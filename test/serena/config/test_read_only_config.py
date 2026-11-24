import hashlib
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from serena.agent import SerenaAgent
from serena.cli import top_level
from serena.config.serena_config import ConfigFileReadOnlyError, ProjectConfig, ProjectConfigAutoGenerationMode, SerenaConfig
from serena.dashboard.dashboard_api import RequestSaveSerenaConfig, SerenaDashboardAPI
from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "serena_home"
    home.mkdir()
    monkeypatch.setenv("SERENA_HOME", str(home))
    path = home / "serena_config.yml"
    monkeypatch.setattr(SerenaConfig, "_determine_config_file_path", classmethod(lambda cls: str(path)))
    return path


@pytest.fixture
def read_only_file(config_path: Path) -> Path:
    """A minimal read-only configuration file, lacking every other key (so loading it migrates)."""
    config_path.write_text("read_only_config: true\nproject_registry: true\nprojects: []\n")
    return config_path


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    project_path = tmp_path / "some_project"
    shutil.copytree(get_repo_path(LanguageServerId.PYTHON), project_path)
    ProjectConfig.load(project_path, serena_config=SerenaConfig(), autogen=ProjectConfigAutoGenerationMode.SYNCHRONOUS)
    return project_path


def _dashboard_save(config: SerenaConfig, content: str) -> None:
    agent = SerenaAgent(serena_config=config)
    try:
        api = SerenaDashboardAPI.__new__(SerenaDashboardAPI)
        api._agent = agent
        api._save_serena_config(RequestSaveSerenaConfig(content=content))
    finally:
        agent.on_shutdown(timeout=5)


class TestReadOnlyConfig:
    def test_loading_a_minimal_file_leaves_it_unchanged(self, read_only_file: Path) -> None:
        digest = _digest(read_only_file)
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        assert config.read_only_config
        assert _digest(read_only_file) == digest

    def test_generated_auth_secret_stays_in_memory(self, config_path: Path) -> None:
        config_path.write_text("read_only_config: true\nauth_secret:\nprojects: []\n")
        digest = _digest(config_path)
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        assert config.auth_secret
        assert _digest(config_path) == digest

    def test_dashboard_toggle_and_activation_by_path_do_not_write(self, read_only_file: Path, project_dir: Path) -> None:
        digest = _digest(read_only_file)
        config = SerenaConfig.from_config_file(generate_if_missing=False)

        config.save_web_dashboard_open_on_launch(False)
        assert config.web_dashboard_open_on_launch is False

        agent = SerenaAgent(serena_config=config)
        try:
            agent.activate_project_from_path_or_name(str(project_dir))
            assert agent.get_active_project() is not None
        finally:
            agent.on_shutdown(timeout=5)
        assert _digest(read_only_file) == digest

    def test_dashboard_config_editor_refuses(self, read_only_file: Path) -> None:
        digest = _digest(read_only_file)
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        with pytest.raises(ConfigFileReadOnlyError, match="read_only_config"):
            _dashboard_save(config, "read_only_config: false\n")
        assert _digest(read_only_file) == digest

    def test_init_fails_naming_the_setting(self, read_only_file: Path) -> None:
        digest = _digest(read_only_file)
        result = CliRunner().invoke(top_level, ["init"])
        assert result.exit_code != 0
        assert "read_only_config" in result.output
        assert _digest(read_only_file) == digest

    def test_file_replaced_by_a_read_only_one_after_loading(self, config_path: Path) -> None:
        config_path.write_text("projects: []\n")
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        assert not config.read_only_config

        config_path.write_text("read_only_config: true\nprojects: []\n")
        digest = _digest(config_path)

        with pytest.raises(ConfigFileReadOnlyError):
            _dashboard_save(config, "projects: []\n")
        config.save_web_dashboard_open_on_launch(False)
        assert _digest(config_path) == digest

    def test_unparsable_file_counts_as_read_only(self, config_path: Path) -> None:
        config_path.write_text("projects: []\n")
        config = SerenaConfig.from_config_file(generate_if_missing=False)

        config_path.write_text("projects: [unclosed\n")
        with pytest.raises(ConfigFileReadOnlyError):
            _dashboard_save(config, "projects: []\n")

    def test_writable_file_is_still_saved(self, config_path: Path) -> None:
        config_path.write_text("projects: []\n")
        config = SerenaConfig.from_config_file(generate_if_missing=False)
        config.save_web_dashboard_open_on_launch(False)
        assert "web_dashboard_open_on_launch: false" in config_path.read_text()
