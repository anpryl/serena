"""
The write guard at language servers' launch writes, without starting a process.

No language marker: these build server shells with ``object.__new__`` (or mock the dependencies) and call the
launch-time write paths directly, so they run in the default suite.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from solidlsp.language_servers.clangd_language_server import ClangdLanguageServer
from solidlsp.language_servers.pyrefly_server import PyreflyLanguageServer
from solidlsp.language_servers.scala_language_server import ScalaLanguageServer
from solidlsp.ls_config import LanguageServerId
from solidlsp.settings import SolidLSPSettings, WriteGuard, WriteRefusal
from solidlsp.util.metals_db_utils import MetalsDbStatus, MetalsLockInfo


class _RefusingWriteGuard(WriteGuard):
    """
    A write guard refusing every write, recording which rule each check was made by.
    """

    def __init__(self) -> None:
        self.checks: list[tuple[str, str]] = []

    def check_write(self, path: str) -> None:
        self.checks.append(("repository", path))
        raise WriteRefusal(f"Refusing to write {path}")

    def check_data_write(self, path: str) -> None:
        self.checks.append(("data", path))
        raise WriteRefusal(f"Refusing to write {path}")


def _settings(tmp_path: Path, guard: WriteGuard, project_data_path: Path, ls_specific_settings: dict | None = None) -> SolidLSPSettings:
    return SolidLSPSettings(
        solidlsp_dir=str(tmp_path / "solidlsp"),
        project_data_path=str(project_data_path),
        ls_specific_settings=ls_specific_settings or {},
        write_guard=guard,
    )


def _make_clangd(root: Path, settings: SolidLSPSettings, custom_settings: dict) -> ClangdLanguageServer:
    server = object.__new__(ClangdLanguageServer)
    server.repository_root_path = str(root)
    server._solidlsp_settings = settings
    server._custom_settings = custom_settings
    return server


def _write_relative_compile_db(root: Path) -> None:
    (root / "compile_commands.json").write_text(
        json.dumps([{"directory": "build", "command": "cc -c main.c", "file": "main.c"}]), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "compile_commands_dir,expected_rule",
    [(None, "data"), ("build-db", "repository")],
    ids=["in_data_folder", "elsewhere_in_repository"],
)
def test_clangd_compile_commands_write_is_refused_by_the_rule_of_its_location(
    tmp_path: Path, compile_commands_dir: str | None, expected_rule: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _write_relative_compile_db(root)
    guard = _RefusingWriteGuard()
    custom_settings = {} if compile_commands_dir is None else {"compile_commands_dir": compile_commands_dir}
    server = _make_clangd(root, _settings(tmp_path, guard, root / ".serena"), custom_settings)

    with pytest.raises(WriteRefusal):
        server._prepare_compile_commands()

    target_dir = root / (compile_commands_dir or ".serena")
    assert guard.checks == [(expected_rule, str(target_dir))]
    assert not target_dir.exists()


def test_pyrefly_workspace_config_write_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    guard = _RefusingWriteGuard()
    server = object.__new__(PyreflyLanguageServer)
    server.repository_root_path = str(root)
    server._solidlsp_settings = _settings(tmp_path, guard, root / ".serena")

    with pytest.raises(WriteRefusal):
        server._ensure_workspace_pyrefly_config()

    assert guard.checks == [("repository", str(root / "pyrefly.toml"))]
    assert not (root / "pyrefly.toml").exists()


def test_metals_stale_lock_removal_is_refused(tmp_path: Path) -> None:
    lock_path = tmp_path / ".metals" / "metals.mv.db.lock.db"
    lock_info = MetalsLockInfo(pid=12345, port=9092, lock_path=lock_path, is_stale=True, raw_content="SERVER:localhost:9092:12345")
    guard = _RefusingWriteGuard()
    settings = _settings(tmp_path, guard, tmp_path / ".serena", {LanguageServerId.SCALA: {"on_stale_lock": "auto-clean"}})
    cleanup = MagicMock(return_value=True)

    with (
        patch("solidlsp.util.metals_db_utils.check_metals_db_status", return_value=(MetalsDbStatus.STALE_LOCK, lock_info)),
        patch("solidlsp.util.metals_db_utils.cleanup_stale_lock", cleanup),
    ):
        server = object.__new__(ScalaLanguageServer)
        with pytest.raises(WriteRefusal):
            server._check_metals_db_status(str(tmp_path), settings)

    assert guard.checks == [("repository", str(lock_path))]
    cleanup.assert_not_called()
