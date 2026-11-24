import hashlib
import logging
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from serena.agent import ProjectNotFoundError, SerenaAgent
from serena.cli import top_level
from serena.config.serena_config import ProjectConfig, ProjectConfigAutoGenerationMode, SerenaConfig
from serena.project_server import ProjectServer
from serena.tools import ListQueryableProjectsTool, QueryProjectTool
from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def serena_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "serena_home"
    home.mkdir()
    monkeypatch.setenv("SERENA_HOME", str(home))
    monkeypatch.setattr(SerenaConfig, "_determine_config_file_path", classmethod(lambda cls: str(home / "serena_config.yml")))
    return home


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A project holding a project configuration file."""
    project_path = tmp_path / "some_project"
    shutil.copytree(get_repo_path(LanguageServerId.PYTHON), project_path)
    ProjectConfig.load(project_path, serena_config=SerenaConfig(), autogen=ProjectConfigAutoGenerationMode.SYNCHRONOUS)
    return project_path


def _write_config(serena_home: Path, registry: bool | None, projects: list[Path | str] | None) -> Path:
    config_path = serena_home / "serena_config.yml"
    lines = []
    if registry is not None:
        lines.append(f"project_registry: {str(registry).lower()}")
    if projects is not None:
        lines.append("projects:" + ("" if projects else " []"))
        lines.extend(f"  - {p}" for p in projects)
    config_path.write_text("\n".join(lines) + "\n")
    return config_path


class TestRegistryOff:
    def test_listed_projects_are_ignored(
        self, serena_home: Path, project_dir: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        # a project listed with its configuration, one without any, and one missing
        _write_config(serena_home, False, [project_dir, tmp_path, tmp_path / "missing"])

        with caplog.at_level(logging.WARNING):
            config = SerenaConfig.from_config_file(generate_if_missing=False)

        assert config.projects == []
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_projects_key_may_be_absent(self, serena_home: Path) -> None:
        _write_config(serena_home, False, None)
        assert SerenaConfig.from_config_file(generate_if_missing=False).projects == []

    def test_projects_key_is_still_required_with_registry_on(self, serena_home: Path) -> None:
        _write_config(serena_home, True, None)
        with pytest.raises(Exception, match="projects"):
            SerenaConfig.from_config_file(generate_if_missing=False)

    def test_activation_by_unknown_name_fails_and_by_path_works_without_writing(self, serena_home: Path, project_dir: Path) -> None:
        config_path = _write_config(serena_home, False, [])
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        digest_before = _digest(config_path)
        agent = SerenaAgent(serena_config=config)
        try:
            with pytest.raises(ProjectNotFoundError, match="project_registry: false"):
                agent.activate_project_from_path_or_name("no_such_project")

            agent.activate_project_from_path_or_name(str(project_dir))
            assert agent.get_active_project() is not None

            # a name activated in this process resolves again
            name = agent.get_active_project_or_raise().project_name
            assert config.get_registered_project(name) is not None
        finally:
            agent.on_shutdown(timeout=5)
        assert _digest(config_path) == digest_before

    def test_saving_keeps_the_listed_projects_of_the_file(self, serena_home: Path, project_dir: Path) -> None:
        config_path = _write_config(serena_home, False, [project_dir])
        SerenaConfig.from_config_file(generate_if_missing=False).save_web_dashboard_open_on_launch(False)
        assert str(project_dir) in config_path.read_text()

    def test_cross_project_queries_are_refused(self, serena_home: Path, project_dir: Path) -> None:
        _write_config(serena_home, False, [])
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        agent = SerenaAgent(serena_config=config)
        config.add_project_from_path(project_dir)
        try:
            assert ListQueryableProjectsTool(agent).apply() == "{}"
            with pytest.raises(ValueError, match="project_registry"):
                QueryProjectTool(agent).apply("any", "list_dir", "{}")
        finally:
            agent.on_shutdown(timeout=5)

    def test_project_server_refuses_to_start(self, serena_home: Path) -> None:
        _write_config(serena_home, False, [])
        with pytest.raises(ValueError, match="project_registry"):
            ProjectServer()

    def test_memories_cli_resolves_a_path_in_a_fresh_process(self, serena_home: Path, project_dir: Path) -> None:
        config_path = _write_config(serena_home, False, [])

        result = CliRunner().invoke(top_level, ["memories", "list", str(project_dir)])

        assert result.exit_code == 0, result.output
        assert str(project_dir) not in config_path.read_text()


class TestRegistryOn:
    def test_projects_are_persisted_and_names_resolve_after_reload(self, serena_home: Path, project_dir: Path) -> None:
        _write_config(serena_home, True, [])
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        project = config.add_project_from_path(project_dir)

        reloaded = SerenaConfig.from_config_file(generate_if_missing=False)
        assert [str(p.project_root) for p in reloaded.projects] == [str(project.project_root)]
        assert reloaded.get_registered_project(project.project_name) is not None

    def test_unknown_name_message_lists_existing_names(self, serena_home: Path) -> None:
        _write_config(serena_home, True, [])
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        assert "Existing project names" in config.unknown_project_message("x")

    def test_cross_project_queries_are_available(self, serena_home: Path, project_dir: Path) -> None:
        _write_config(serena_home, True, [])
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        config.add_project_from_path(project_dir)
        config.ensure_project_queries_available()
        assert len(config.queryable_projects) == 1
