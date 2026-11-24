"""Tests for the per-process overrides of ``start-mcp-server`` (``--project-registry``, ``--read-only-config``, ``--untrusted``)."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from serena import mcp as mcp_module
from serena.agent import SerenaAgent
from serena.config.serena_config import SerenaConfig
from serena.mcp import SerenaMCPFactory
from serena.project import Project


class _ConfigCaptured(Exception):
    pass


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "serena_config.yml"
    monkeypatch.setattr(SerenaConfig, "_determine_config_file_path", classmethod(lambda cls: str(path)))
    return path


def _write_config(path: Path, **settings: object) -> None:
    lines = ["projects:"] + [f"{key}: {str(value).lower()}" for key, value in settings.items()]
    path.write_text("\n".join(lines) + "\n")


def _config_after_overrides(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> SerenaConfig:
    """Runs the factory's override step on the configuration file and returns the configuration it would hand to the agent."""
    captured: list[SerenaConfig] = []

    def capture(self: SerenaMCPFactory, serena_config: SerenaConfig, *args: object, **kwargs: object) -> SerenaAgent:
        captured.append(serena_config)
        raise _ConfigCaptured

    monkeypatch.setattr(SerenaMCPFactory, "_create_serena_agent", capture)
    monkeypatch.setattr(mcp_module, "show_fatal_exception_safe", lambda e: None)
    with pytest.raises(_ConfigCaptured):
        SerenaMCPFactory(transport="stdio").create_mcp_server(**overrides)
    return captured[0]


class TestRegistryAndReadOnlyOverrides:
    @pytest.mark.parametrize("file_value", [True, False])
    def test_project_registry_override_beats_file(self, config_file: Path, monkeypatch: pytest.MonkeyPatch, file_value: bool) -> None:
        _write_config(config_file, project_registry=file_value)
        config = _config_after_overrides(monkeypatch, project_registry=not file_value)
        assert config.project_registry is (not file_value)

    def test_registry_off_override_forgets_registered_projects(
        self, config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = tmp_path / "registered"
        (project_dir / ".serena").mkdir(parents=True)
        (project_dir / ".serena" / "project.yml").write_text("project_name: registered\nlanguages: [python]\n")
        config_file.write_text(f"projects:\n  - {project_dir}\n")
        assert [p.project_name for p in SerenaConfig.from_config_file().projects] == ["registered"]
        config = _config_after_overrides(monkeypatch, project_registry=False)
        assert config.projects == []

    def test_read_only_override_true_beats_file(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_config(config_file, read_only_config=False)
        config = _config_after_overrides(monkeypatch, read_only_config=True)
        assert config.read_only_config is True
        assert config.is_config_file_writable() is False

    def test_read_only_override_false_cannot_unlock_file_that_says_true(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_config(config_file, read_only_config=True)
        config = _config_after_overrides(monkeypatch, read_only_config=False)
        assert config.read_only_config is False
        assert config.is_config_file_writable() is False

    def test_read_only_override_false_keeps_writable_file_writable(self, config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_config(config_file, read_only_config=False)
        config = _config_after_overrides(monkeypatch, read_only_config=False)
        assert config.is_config_file_writable() is True


class TestCliFlags:
    def test_flags_are_forwarded_to_the_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from serena.cli import TopLevelCommands

        received: dict[str, object] = {}

        def record(self: SerenaMCPFactory, **kwargs: object) -> None:
            received.update(kwargs)
            raise _ConfigCaptured

        monkeypatch.setattr(SerenaMCPFactory, "create_mcp_server", record)
        result = CliRunner().invoke(
            TopLevelCommands.start_mcp_server,
            ["--project-registry", "false", "--read-only-config", "true", "--untrusted"],
        )
        assert isinstance(result.exception, _ConfigCaptured), result.output
        assert received["project_registry"] is False
        assert received["read_only_config"] is True
        assert received["untrusted"] is True


class TestUntrusted:
    @staticmethod
    def _project_with_activation_command(root: Path, marker: Path, config: SerenaConfig) -> Project:
        (root / ".serena").mkdir(parents=True)
        (root / ".serena" / "project.yml").write_text(
            "\n".join(
                [
                    "project_name: untrusted-test",
                    "languages: [python]",
                    f'activation_command: "touch {marker}"',
                    "ls_specific_settings:",
                    "  python:",
                    "    ls_path: /project/level/path",
                ]
            )
            + "\n"
        )
        return Project.load(root, serena_config=config)

    @pytest.mark.parametrize("untrusted", [True, False])
    def test_activation_command_runs_only_when_trusted(
        self, config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, untrusted: bool
    ) -> None:
        config_file.write_text("projects:\ntrusted_project_path_patterns:\n  - '**'\n")
        config = _config_after_overrides(monkeypatch, untrusted=untrusted)
        marker = tmp_path / "marker"
        project = self._project_with_activation_command(tmp_path / "proj", marker, config)

        SerenaAgent._run_project_activation_command(project)

        assert marker.exists() is (not untrusted)

    @pytest.mark.parametrize("untrusted", [True, False])
    def test_project_level_ls_settings_applied_only_when_trusted(
        self, config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, untrusted: bool
    ) -> None:
        config_file.write_text("projects:\ntrusted_project_path_patterns:\n  - '**'\n")
        config = _config_after_overrides(monkeypatch, untrusted=untrusted)
        project = self._project_with_activation_command(tmp_path / "proj", tmp_path / "marker", config)
        captured: dict[str, object] = {}

        def record_factory(*args: object, **kwargs: object) -> object:
            captured.update(kwargs)
            return object()

        from serena import project as project_module

        monkeypatch.setattr(project_module, "LanguageServerFactory", record_factory)
        monkeypatch.setattr(project_module.LanguageServerManager, "from_languages", classmethod(lambda cls, *a, **k: object()))
        project.create_language_server_manager()

        applied = "ls_path" in (captured["ls_specific_settings"].get("python") or {})  # type: ignore[attr-defined]
        assert applied is (not untrusted)
        assert captured["is_project_trusted"] is (not untrusted)
