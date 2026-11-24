import logging
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from serena import code_editor as code_editor_module
from serena.agent import SerenaAgent
from serena.config.serena_config import ProjectConfig, RegisteredProject, SerenaConfig
from serena.constants import PROJECT_TEMPLATE_FILE
from serena.language_backend import BuiltinLanguageBackend
from serena.lsp.lsp_diagnostics import DiagnosticsContext
from serena.project import Project
from serena.tools import (
    CreateTextFileTool,
    FindDeclarationTool,
    FindFileTool,
    FindReferencingSymbolsTool,
    FindSymbolTool,
    GetDiagnosticsForFileTool,
    GetSymbolsOverviewTool,
    ListDirTool,
    ReadFileTool,
    RenameSymbolTool,
    ReplaceContentTool,
    ReplaceInFilesTool,
    ReplaceSymbolBodyTool,
    SafeDeleteSymbol,
    SearchForPatternTool,
    Tool,
)
from serena.tools.tools_base import EditingToolWithDiagnostics
from serena.worktree_routing import WorktreeRouter
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from test.conftest import get_pytest_markers, get_repo_path

pytestmark = get_pytest_markers(LanguageServerId.PYTHON)

MODELS_FILE = os.path.join("test_repo", "models.py")
MAIN_ONLY_IGNORED_FILE = "test_repo/variables.py"
"""a file ignored by the main project's configuration only"""
PROJECT_NAME = "worktree_routing_repo"


def make_project_config(project_name: str, **overrides: Any) -> ProjectConfig:
    settings: dict[str, Any] = {
        "language_servers": [LanguageServerId.PYTHON],
        "ignored_paths": [],
        "excluded_tools": [],
        "read_only": False,
        **overrides,
    }
    return ProjectConfig(
        project_name=project_name,
        ignore_all_files_in_gitignore=True,
        initial_prompt="",
        encoding="utf-8",
        **settings,
    )


def commit_worktree_config(root: Path, **overrides: Any) -> None:
    """
    Commits a project configuration of the worktree's own to its branch.
    """
    config_path = root / ".serena" / "project.yml"
    config_path.parent.mkdir(exist_ok=True)
    shutil.copyfile(PROJECT_TEMPLATE_FILE, config_path)
    make_project_config(root.name, **overrides).save(str(config_path))
    git(root, "add", "-f", str(config_path))
    git(root, "commit", "-qm", "add project configuration")


def git(cwd: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def add_worktree(main: Path, path: str, branch: str, marker: str) -> Path:
    """
    Adds a worktree on a new branch, committing a function named `marker` there, so that the trees differ.

    :return: the worktree's root
    """
    git(main, "worktree", "add", path, "-b", branch)
    root = (main / path).resolve()
    with open(root / MODELS_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n\ndef {marker}() -> None:\n    pass\n")
    git(root, "commit", "-am", f"add {marker}")
    return root


@dataclass
class Worktree:
    root: Path
    marker: str
    """the name of a function that exists only in this worktree"""


@dataclass
class WorktreeRepo:
    main: Path
    worktrees: dict[str, Worktree]

    def read_models(self, root: Path) -> str:
        return (root / MODELS_FILE).read_text(encoding="utf-8")


def create_worktree_repo(base: Path) -> WorktreeRepo:
    """
    Creates the test repository under `base`, with a sibling and a nested worktree.
    """
    main = base / "main"
    shutil.copytree(get_repo_path(LanguageServerId.PYTHON), main, ignore=shutil.ignore_patterns("__pycache__", ".serena"))
    with open(main / ".gitignore", "a", encoding="utf-8") as f:
        f.write("\n.claude/\n.serena/\n")
    git(main, "init", "-q", "-b", "main")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "initial")
    sibling = add_worktree(main, "../wt", "feature", "feature_only_function")
    nested = add_worktree(main, ".claude/worktrees/x", "nested", "nested_only_function")
    return WorktreeRepo(
        main=main.resolve(),
        worktrees={
            "sibling": Worktree(root=sibling, marker="feature_only_function"),
            "nested": Worktree(root=nested, marker="nested_only_function"),
        },
    )


@contextmanager
def routing_agent(
    project_root: Path,
    trusted_project_path_patterns: list[str] | None = None,
    project_serena_folder_location: str | None = None,
    **project_settings: Any,
) -> Iterator[SerenaAgent]:
    """
    Provides an agent with routing on, whose active project has the given root.

    :param trusted_project_path_patterns: the trusted project path patterns, if not the default (all paths)
    :param project_serena_folder_location: the location template of projects' Serena data folders, if not the default
    :param project_settings: settings of the active project's configuration
    """
    config = SerenaConfig(log_level=logging.ERROR, tool_timeout=600, route_worktree_paths=True).with_headless_mode_overrides()
    if trusted_project_path_patterns is not None:
        config.trusted_project_path_patterns = trusted_project_path_patterns
    if project_serena_folder_location is not None:
        config.project_serena_folder_location = project_serena_folder_location
    project = Project(
        project_root=str(project_root),
        project_config=make_project_config(PROJECT_NAME, ignored_paths=[MAIN_ONLY_IGNORED_FILE], **project_settings),
        serena_config=config,
    )
    config.projects = [RegisteredProject.from_project_instance(project)]
    agent = SerenaAgent(project=PROJECT_NAME, serena_config=config)
    try:
        agent.execute_task(lambda: None)
        yield agent
    finally:
        agent.on_shutdown(timeout=5)


@pytest.fixture
def worktree_repo(tmp_path: Path) -> WorktreeRepo:
    return create_worktree_repo(tmp_path)


@pytest.fixture
def agent(worktree_repo: WorktreeRepo) -> Iterator[SerenaAgent]:
    with routing_agent(worktree_repo.main) as agent:
        yield agent


def call(agent: SerenaAgent, tool_class: type[Tool], **kwargs: Any) -> str:
    return agent.get_tool(tool_class).apply_ex(**kwargs)


def call_unrouted(agent: SerenaAgent, tool_class: type[Tool], **kwargs: Any) -> str:
    """
    Calls a tool with routing switched off, i.e. as Serena behaves without it.
    """
    agent.serena_config.route_worktree_paths = False
    try:
        return call(agent, tool_class, **kwargs)
    finally:
        agent.serena_config.route_worktree_paths = True


def use_git_without_nul_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Puts a git wrapper first on the path which rejects `-z`, as git versions before 2.36 do for `worktree list`.
    """
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper_dir = tmp_path / "old-git"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(f'#!/bin/sh\nfor arg in "$@"; do [ "$arg" = "-z" ] && exit 129; done\nexec "{real_git}" "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}")


def use_git_echoing_path_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Puts a git wrapper first on the path which, as git versions before 2.31 do, echoes `--path-format=...` back from
    `rev-parse` instead of applying it.
    """
    real_git = shutil.which("git")
    assert real_git is not None
    wrapper_dir = tmp_path / "git-without-path-format"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "git"
    wrapper.write_text(
        f'#!/bin/sh\nfor arg in "$@"; do case "$arg" in --path-format=*) echo "$arg"; exit 0;; esac; done\nexec "{real_git}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}")


def record_project_shutdowns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    :return: a list which receives the root of every project shut down from now on
    """
    shut_down_roots: list[str] = []
    original_shutdown = Project.shutdown

    def recording_shutdown(project: Project, timeout: float = 2.0) -> None:
        shut_down_roots.append(project.project_root)
        original_shutdown(project, timeout=timeout)

    monkeypatch.setattr(Project, "shutdown", recording_shutdown)
    return shut_down_roots


@dataclass
class ShutDownProject:
    project: Project
    language_servers: list[SolidLanguageServer]
    """the project's language servers when its shutdown began"""

    def are_servers_stopped(self) -> bool:
        return all(not ls.is_running() for ls in self.language_servers)


def record_shut_down_projects(monkeypatch: pytest.MonkeyPatch) -> list[ShutDownProject]:
    """
    :return: a list which receives every project shut down from now on, once its shutdown has completed
    """
    shut_down_projects: list[ShutDownProject] = []
    original_shutdown = Project.shutdown

    def recording_shutdown(project: Project, timeout: float = 2.0) -> None:
        manager = project.language_server_manager
        language_servers = list(manager.get_current_language_servers()) if manager is not None else []
        original_shutdown(project, timeout=timeout)
        shut_down_projects.append(ShutDownProject(project=project, language_servers=language_servers))

    monkeypatch.setattr(Project, "shutdown", recording_shutdown)
    return shut_down_projects


def wait_for_length(items: list, length: int, timeout: float = 60) -> None:
    """
    Waits until a list, filled by another thread, has reached the given length, or the timeout has passed.
    """
    deadline = time.monotonic() + timeout
    while len(items) < length and time.monotonic() < deadline:
        time.sleep(0.05)


def is_running(project: Project) -> bool:
    """
    :return: whether all of the project's language servers are running
    """
    manager = project.language_server_manager
    return manager is not None and all(ls.is_running() for ls in manager.get_current_language_servers())


def recreate_worktree_in_place(main: Path, root: Path, branch: str, kept_dir: Path) -> None:
    """
    Removes a worktree and adds it again in the same, emptied directory, so that its root keeps its identity while its
    git directory is a new one.

    :param kept_dir: a directory to keep the old git directory in, so that the new one cannot take over its identity
    """
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").strip())
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    shutil.move(git_dir, kept_dir)
    git(main, "worktree", "add", "-q", str(root), branch)


def recreate_worktree_at_same_path(main: Path, root: Path, branch: str, kept_dir: Path) -> None:
    """
    Removes a worktree and adds it again at the same path, keeping the old root and git directory (moved into
    `kept_dir`), so that the new ones cannot take over their identities.
    """
    git_dir = Path(git(root, "rev-parse", "--absolute-git-dir").strip())
    kept_dir.mkdir()
    shutil.move(root, kept_dir / "root")
    shutil.move(git_dir, kept_dir / "git-dir")
    git(main, "worktree", "add", "-q", str(root), branch)


def paths_under(result: str, root: Path) -> list[str]:
    """
    :return: the paths under the given root that a tool result names, in order of appearance
    """
    return [path.removesuffix(".") for path in re.findall(re.escape(str(root)) + r"[^\"\s:,\]\}\)]*", result)]


def snapshot_files(root: Path) -> dict[str, bytes]:
    """
    :return: the contents of the files of a checkout, excluding git's and Serena's data and nested worktrees
    """
    snapshot = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", ".serena", ".claude")]
        for filename in filenames:
            path = Path(dirpath) / filename
            snapshot[str(path.relative_to(root))] = path.read_bytes()
    return snapshot


def assert_path_routes_back(agent: SerenaAgent, worktree_repo: WorktreeRepo, path: str) -> None:
    """
    Asserts that an edit through the given path, as taken from a routed result, lands in the sibling worktree only.
    """
    sibling_root = worktree_repo.worktrees["sibling"].root
    assert path.startswith(str(sibling_root)), path
    main_before = snapshot_files(worktree_repo.main)
    result = call(agent, ReplaceContentTool, relative_path=path, needle=r"\A", repl="# routed back\n", mode="regex")
    assert Path(path).read_text(encoding="utf-8").startswith("# routed back\n"), result
    assert snapshot_files(worktree_repo.main) == main_before


def shortened_result(call_with_limit: Callable[[int], str], header: str) -> str:
    """
    :param call_with_limit: calls a tool with the given `max_answer_chars`
    :param header: the header of the shortened form sought
    :return: the result of the first of a series of decreasing limits whose result is in the sought form
    """
    full_length = len(call_with_limit(-1))
    for fraction in (0.95, 0.85, 0.7, 0.55, 0.45, 0.35, 0.25, 0.18, 0.12, 0.08, 0.05, 0.03, 0.02):
        result = call_with_limit(max(1, int(full_length * fraction)))
        if header in result:
            return result
    raise AssertionError(f"no limit produced the form '{header}'")


WORKTREE_KINDS = ["sibling", "nested"]


class TestWorktreeRouting:
    @pytest.mark.parametrize("kind", WORKTREE_KINDS)
    def test_reads_follow_the_worktree(self, agent: SerenaAgent, worktree_repo: WorktreeRepo, kind: str) -> None:
        worktree = worktree_repo.worktrees[kind]
        path = str(worktree.root / MODELS_FILE)

        overview = call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert worktree.marker in overview
        assert worktree.marker not in call(agent, GetSymbolsOverviewTool, relative_path=MODELS_FILE)

        found = call(agent, FindSymbolTool, name_path_pattern=worktree.marker, relative_path=path)
        assert worktree.marker in found

    @pytest.mark.parametrize("kind", WORKTREE_KINDS)
    def test_path_that_is_the_worktree_root_routes(self, agent: SerenaAgent, worktree_repo: WorktreeRepo, kind: str) -> None:
        worktree = worktree_repo.worktrees[kind]
        found = call(agent, FindSymbolTool, name_path_pattern=worktree.marker, relative_path=str(worktree.root))
        assert worktree.marker in found

    def test_worktree_created_after_first_routed_call_routes(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

        late_root = add_worktree(worktree_repo.main, ".claude/worktrees/late", "late", "late_only_function")
        assert "late_only_function" in call(agent, GetSymbolsOverviewTool, relative_path=str(late_root / MODELS_FILE))

    @pytest.mark.parametrize("replacement", ["plain_directory", "unlisted_plain_directory", "other_repository"])
    def test_replaced_worktree_is_refused(self, agent: SerenaAgent, worktree_repo: WorktreeRepo, replacement: str) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        main_models_before = worktree_repo.read_models(worktree_repo.main)

        # replace the worktree with a directory holding a file of its own
        if replacement == "unlisted_plain_directory":
            git(worktree_repo.main, "worktree", "remove", "--force", str(sibling.root))
        else:
            shutil.rmtree(sibling.root)
        (sibling.root / "test_repo").mkdir(parents=True)
        (sibling.root / MODELS_FILE).write_text("def replacement_function() -> None:\n    pass\n", encoding="utf-8")
        if replacement == "other_repository":
            git(sibling.root, "init", "-q")

        path = str(sibling.root / MODELS_FILE)
        assert "replacement_function" not in call(agent, GetSymbolsOverviewTool, relative_path=path)
        result = call(agent, ReplaceContentTool, relative_path=path, needle="replacement_function", repl="edited", mode="literal")
        assert "replacement_function" in (sibling.root / MODELS_FILE).read_text(encoding="utf-8"), result
        if replacement == "other_repository":
            assert str(sibling.root) in result
        assert worktree_repo.read_models(worktree_repo.main) == main_models_before

    def test_path_spellings_route_alike(self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        spellings = [str(sibling.root)]

        # a spelling through a symlinked directory
        alias = tmp_path / "alias"
        alias.symlink_to(sibling.root.parent, target_is_directory=True)
        spellings.append(str(alias / sibling.root.name))

        # the spelling without the /private prefix (macOS: /tmp and /var are symlinks into /private)
        if str(sibling.root).startswith("/private/") and os.path.samefile(str(sibling.root).removeprefix("/private"), sibling.root):
            spellings.append(str(sibling.root).removeprefix("/private"))

        for spelling in spellings:
            overview = call(agent, GetSymbolsOverviewTool, relative_path=os.path.join(spelling, MODELS_FILE))
            assert sibling.marker in overview, spelling

    @pytest.mark.parametrize(
        "case",
        ["relative", "omitted", "absolute_in_main_root", "other_repository", "plain_directory"],
    )
    def test_unrouted_paths_behave_as_today(self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, case: str) -> None:
        # prepare a file outside any worktree of the active project's repository
        outside_dir = tmp_path / case
        outside_file = outside_dir / "module.py"
        if case in ("other_repository", "plain_directory"):
            outside_dir.mkdir()
            outside_file.write_text("def outside_function() -> None:\n    pass\n", encoding="utf-8")
            if case == "other_repository":
                git(outside_dir, "init", "-q")
                git(outside_dir, "add", "-A")
                git(outside_dir, "commit", "-qm", "initial")

        tool_class: type[Tool]
        kwargs: dict[str, Any]
        match case:
            case "relative":
                tool_class, kwargs = GetSymbolsOverviewTool, {"relative_path": MODELS_FILE}
            case "omitted":
                tool_class, kwargs = FindSymbolTool, {"name_path_pattern": "feature_only_function"}
            case "absolute_in_main_root":
                tool_class, kwargs = GetSymbolsOverviewTool, {"relative_path": str(worktree_repo.main / MODELS_FILE)}
            case _:
                tool_class, kwargs = GetSymbolsOverviewTool, {"relative_path": str(outside_file)}

        assert call(agent, tool_class, **kwargs) == call_unrouted(agent, tool_class, **kwargs)

    def test_timed_out_routed_call_does_not_affect_next_call(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        sibling_path = str(sibling.root / MODELS_FILE)
        sibling_models_before = worktree_repo.read_models(sibling.root)

        # start the worktree's language server, then make its tool exceed a small timeout
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=sibling_path)
        overview_tool = agent.get_tool(GetSymbolsOverviewTool)
        original_apply = overview_tool.apply
        late_finish = threading.Event()

        def sleeping_apply(**kwargs: Any) -> str:
            time.sleep(3)
            try:
                return original_apply(**kwargs)
            finally:
                late_finish.set()

        monkeypatch.setattr(overview_tool, "apply", sleeping_apply)
        agent.serena_config.tool_timeout = 1

        timed_out = call(agent, GetSymbolsOverviewTool, relative_path=sibling_path)
        assert "timed out" in timed_out
        assert f"git worktree {sibling.root}" in timed_out
        result = call(
            agent,
            ReplaceContentTool,
            relative_path=MODELS_FILE,
            needle="class User(BaseModel):",
            repl="class User(BaseModel):  # one",
            mode="literal",
        )
        assert "class User(BaseModel):  # one" in worktree_repo.read_models(worktree_repo.main), result
        assert worktree_repo.read_models(sibling.root) == sibling_models_before

        # after the routed call's late finish, unrouted calls still reach the main checkout
        assert late_finish.wait(timeout=60)
        agent.serena_config.tool_timeout = 600
        call(agent, ReplaceContentTool, relative_path=MODELS_FILE, needle="  # one", repl="  # two", mode="literal")
        assert "class User(BaseModel):  # two" in worktree_repo.read_models(worktree_repo.main)
        assert worktree_repo.read_models(sibling.root) == sibling_models_before

    def test_prunable_entry_is_not_routed(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        # replace the nested worktree with a plain directory, which git then lists as prunable; inside the main
        # checkout, git still resolves the directory to the active project's repository
        nested = worktree_repo.worktrees["nested"]
        shutil.rmtree(nested.root)
        (nested.root / "test_repo").mkdir(parents=True)
        (nested.root / MODELS_FILE).write_text("def replacement_function() -> None:\n    pass\n", encoding="utf-8")
        assert "prunable" in git(worktree_repo.main, "worktree", "list", "--porcelain")

        path = str(nested.root / MODELS_FILE)
        result = call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert "replacement_function" not in result
        assert result == call_unrouted(agent, GetSymbolsOverviewTool, relative_path=path)

    def test_parent_segment_after_symlink_is_not_routed_to_another_file(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path
    ) -> None:
        # a symlink in the worktree whose parent is not the worktree's directory, so that `link/..` leaves the worktree
        sibling = worktree_repo.worktrees["sibling"]
        elsewhere = tmp_path / "elsewhere"
        (elsewhere / "inner").mkdir(parents=True)
        (elsewhere / "models.py").write_text("def elsewhere_function() -> None:\n    pass\n", encoding="utf-8")
        (sibling.root / "test_repo" / "link").symlink_to(elsewhere / "inner", target_is_directory=True)
        sibling_models_before = worktree_repo.read_models(sibling.root)

        path = str(sibling.root / "test_repo" / "link" / ".." / "models.py")
        result = call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert sibling.marker not in result
        assert result == call_unrouted(agent, GetSymbolsOverviewTool, relative_path=path)
        call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert worktree_repo.read_models(sibling.root) == sibling_models_before

    def test_moved_worktree_routes_to_its_new_path(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

        # move the worktree, and put another checkout at its old path
        moved_root = sibling.root.parent / "wt-moved"
        git(worktree_repo.main, "worktree", "move", str(sibling.root), str(moved_root))
        git(worktree_repo.main, "worktree", "add", str(sibling.root), "-b", "at-old-path")
        old_path_models_before = worktree_repo.read_models(sibling.root)

        result = call(
            agent,
            ReplaceContentTool,
            relative_path=str(moved_root / MODELS_FILE),
            needle="class User(BaseModel):",
            repl="class User(BaseModel):  # moved",
            mode="literal",
        )
        assert "class User(BaseModel):  # moved" in worktree_repo.read_models(moved_root), result
        assert worktree_repo.read_models(sibling.root) == old_path_models_before

    def test_worktree_read_only_configuration_refuses_edits(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        commit_worktree_config(sibling.root, read_only=True)
        sibling_models_before = worktree_repo.read_models(sibling.root)
        path = str(sibling.root / MODELS_FILE)

        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=path)
        result = call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert "read_only" in result
        assert worktree_repo.read_models(sibling.root) == sibling_models_before

    def test_incomplete_worktree_configuration_is_not_rewritten(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        config_path = sibling.root / ".serena" / "project.yml"
        config_path.parent.mkdir(exist_ok=True)
        config_path.write_text("project_name: wt\nlanguage_servers: [python]\nread_only: true\n", encoding="utf-8")
        git(sibling.root, "add", "-f", str(config_path))
        git(sibling.root, "commit", "-qm", "add incomplete project configuration")
        config_before = config_path.read_bytes()
        path = str(sibling.root / MODELS_FILE)

        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert "read_only" in call(agent, ReplaceContentTool, relative_path=path, needle="class User", repl="class Edited", mode="literal")
        assert config_path.read_bytes() == config_before

    def test_worktree_excluded_tool_is_refused(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        commit_worktree_config(sibling.root, excluded_tools=["find_symbol"])

        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))
        result = call(agent, FindSymbolTool, name_path_pattern=sibling.marker, relative_path=str(sibling.root))
        assert "excluded_tools" in result
        assert "User" in call(agent, FindSymbolTool, name_path_pattern="User", relative_path=MODELS_FILE)

    @pytest.mark.parametrize("has_own_config", [False, True], ids=["without_own_config", "with_own_config"])
    def test_worktree_configuration_falls_back_to_active_project(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, has_own_config: bool
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        if has_own_config:
            commit_worktree_config(sibling.root)

        result = call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MAIN_ONLY_IGNORED_FILE))
        if has_own_config:
            assert "VariableContainer" in result
        else:
            assert "VariableContainer" not in result
            main_result = call(agent, GetSymbolsOverviewTool, relative_path=MAIN_ONLY_IGNORED_FILE)
            assert result.startswith(main_result.replace(MAIN_ONLY_IGNORED_FILE, str(sibling.root / MAIN_ONLY_IGNORED_FILE)))

    def test_paths_behave_as_today_without_git(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setenv("PATH", str(empty_dir))

        path = str(worktree_repo.main / MODELS_FILE)
        result = call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert "User" in result
        assert result == call_unrouted(agent, GetSymbolsOverviewTool, relative_path=path)

    def test_paths_behave_as_today_with_undecodable_worktree_path(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        undecodable_root = os.fsencode(worktree_repo.main.parent) + b"/wt-\xff"
        try:
            os.mkdir(undecodable_root)
            os.rmdir(undecodable_root)
        except OSError:
            pytest.skip("the file system rejects paths that are not valid UTF-8")
        subprocess.run([b"git", b"worktree", b"add", undecodable_root, b"-b", b"undecodable"], cwd=worktree_repo.main, check=True)

        path = str(worktree_repo.main / MODELS_FILE)
        result = call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert "User" in result
        assert result == call_unrouted(agent, GetSymbolsOverviewTool, relative_path=path)

    @pytest.mark.parametrize("link_kind", ["file", "directory"])
    @pytest.mark.parametrize("link_from,link_to", [("nested", "main"), ("sibling", "nested")], ids=["nested_to_main", "sibling_to_nested"])
    def test_symlink_out_of_worktree_is_not_written_through(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, link_from: str, link_to: str, link_kind: str
    ) -> None:
        source_root = worktree_repo.worktrees[link_from].root
        target_root = worktree_repo.main if link_to == "main" else worktree_repo.worktrees[link_to].root
        target_models_before = worktree_repo.read_models(target_root)

        # link into the target tree, either to its models file or to its root directory
        if link_kind == "file":
            link = source_root / "test_repo" / "linked_models.py"
            link.symlink_to(target_root / MODELS_FILE)
            path = str(link)
        else:
            link = source_root / "linked_root"
            link.symlink_to(target_root, target_is_directory=True)
            path = str(link / MODELS_FILE)

        call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert worktree_repo.read_models(target_root) == target_models_before

    def test_symlink_in_untrusted_active_project_is_rejected(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        agent.serena_config.trusted_project_path_patterns = []
        (worktree_repo.main / "test_repo" / "linked_worktree").symlink_to(sibling.root, target_is_directory=True)
        sibling_models_before = worktree_repo.read_models(sibling.root)

        path = str(worktree_repo.main / "test_repo" / "linked_worktree" / MODELS_FILE)
        assert sibling.marker not in call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert sibling.marker not in call_unrouted(agent, GetSymbolsOverviewTool, relative_path=path)
        call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert worktree_repo.read_models(sibling.root) == sibling_models_before

    @pytest.mark.parametrize("spelling", ["parent_segment", "symlink_to_root"])
    def test_path_resolving_into_read_only_nested_worktree_is_refused(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, spelling: str
    ) -> None:
        nested = worktree_repo.worktrees["nested"]
        commit_worktree_config(nested.root, read_only=True)
        nested_models_before = worktree_repo.read_models(nested.root)

        # spell a path in the main checkout that resolves into the nested worktree
        if spelling == "parent_segment":
            path = str(nested.root / "test_repo" / ".." / MODELS_FILE)
        else:
            alias = worktree_repo.main / "nested_alias"
            alias.symlink_to(nested.root, target_is_directory=True)
            path = str(alias / MODELS_FILE)

        result = call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert str(nested.root) in result
        assert worktree_repo.read_models(nested.root) == nested_models_before

    def test_worktree_checkout_to_read_only_branch_refuses_next_edit(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        path = str(sibling.root / MODELS_FILE)

        # prepare a read-only branch, leaving the worktree on its branch without a configuration of its own
        git(sibling.root, "checkout", "-q", "-b", "read-only")
        commit_worktree_config(sibling.root, read_only=True)
        git(sibling.root, "checkout", "-q", "feature")
        call(
            agent,
            ReplaceContentTool,
            relative_path=path,
            needle="class User(BaseModel):",
            repl="class User(BaseModel):  # one",
            mode="literal",
        )
        assert "class User(BaseModel):  # one" in worktree_repo.read_models(sibling.root)

        git(sibling.root, "checkout", "-q", "-f", "read-only")
        models_before = worktree_repo.read_models(sibling.root)
        result = call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert "read_only" in result
        assert worktree_repo.read_models(sibling.root) == models_before

    def test_worktree_checkout_adding_ignored_path_replaces_project(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]

        # prepare a branch whose configuration ignores the models file, leaving the worktree on its branch
        git(sibling.root, "checkout", "-q", "-b", "ignoring")
        commit_worktree_config(sibling.root, ignored_paths=[MODELS_FILE.replace(os.sep, "/")])
        git(sibling.root, "checkout", "-q", "feature")
        assert "models.py" in call(agent, SearchForPatternTool, substring_pattern=sibling.marker, relative_path=str(sibling.root))

        # record every project shutdown across the checkout
        shut_down_roots: list[str] = []
        original_shutdown = Project.shutdown

        def recording_shutdown(project: Project, timeout: float = 2.0) -> None:
            shut_down_roots.append(project.project_root)
            original_shutdown(project, timeout=timeout)

        monkeypatch.setattr(Project, "shutdown", recording_shutdown)
        git(sibling.root, "checkout", "-q", "ignoring")
        result = call(agent, SearchForPatternTool, substring_pattern=sibling.marker, relative_path=str(sibling.root))
        wait_for_length(shut_down_roots, 1)
        monkeypatch.undo()
        assert "models.py" not in result
        assert shut_down_roots == [str(sibling.root)]

    def test_worktree_mode_removing_edit_tools_refuses_edits(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        commit_worktree_config(sibling.root, added_modes=["planning"])
        sibling_models_before = worktree_repo.read_models(sibling.root)
        path = str(sibling.root / MODELS_FILE)

        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=path)
        result = call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert "added_modes" in result
        assert worktree_repo.read_models(sibling.root) == sibling_models_before
        call(
            agent,
            ReplaceContentTool,
            relative_path=MODELS_FILE,
            needle="class User(BaseModel):",
            repl="class User(BaseModel):  # main",
            mode="literal",
        )
        assert "class User(BaseModel):  # main" in worktree_repo.read_models(worktree_repo.main)

    def test_spellings_of_one_worktree_do_not_restart_its_project(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        alias = tmp_path / "alias"
        alias.symlink_to(sibling.root.parent, target_is_directory=True)
        spellings = [sibling.root, alias / sibling.root.name]
        upper_case_root = sibling.root.parent / sibling.root.name.upper()
        if os.path.exists(upper_case_root) and os.path.samefile(upper_case_root, sibling.root):
            spellings.append(upper_case_root)
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

        # record every project shutdown while the spellings take turns
        shut_down_roots: list[str] = []
        original_shutdown = Project.shutdown

        def recording_shutdown(project: Project, timeout: float = 2.0) -> None:
            shut_down_roots.append(project.project_root)
            original_shutdown(project, timeout=timeout)

        monkeypatch.setattr(Project, "shutdown", recording_shutdown)
        for spelling in spellings * 2:
            assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(spelling / MODELS_FILE)), spelling
        monkeypatch.undo()
        assert shut_down_roots == []

    def test_read_only_nested_worktree_is_refused_without_git(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = worktree_repo.worktrees["nested"]
        commit_worktree_config(nested.root, read_only=True)
        nested_models_before = worktree_repo.read_models(nested.root)
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.setenv("PATH", str(empty_dir))

        path = str(nested.root / MODELS_FILE)
        result = call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert str(nested.root) in result
        assert worktree_repo.read_models(nested.root) == nested_models_before

    def test_fallback_configuration_follows_changes_to_the_active_project(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        assert "models.py" in call(agent, SearchForPatternTool, substring_pattern=sibling.marker, relative_path=str(sibling.root))

        # change the active project's configuration in place, as the dashboard's language server edits do
        agent.get_active_project_or_raise().project_config.ignored_paths.append(MODELS_FILE.replace(os.sep, "/"))

        assert "models.py" not in call(agent, SearchForPatternTool, substring_pattern=sibling.marker, relative_path=str(sibling.root))

    def test_active_project_in_subdirectory_of_its_worktree_does_not_route(self, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        sibling_models_before = worktree_repo.read_models(sibling.root)
        with routing_agent(worktree_repo.main / "test_repo") as agent:
            path = str(sibling.root / MODELS_FILE)
            assert sibling.marker not in call(agent, GetSymbolsOverviewTool, relative_path=path)
            result = call(
                agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal"
            )
            assert str(sibling.root) in result
            assert worktree_repo.read_models(sibling.root) == sibling_models_before
            assert "User" in call(agent, GetSymbolsOverviewTool, relative_path="models.py")

    def test_absolute_path_into_nested_clone_of_non_git_project_behaves_as_today(self, tmp_path: Path) -> None:
        plain_root = tmp_path / "plain"
        shutil.copytree(get_repo_path(LanguageServerId.PYTHON), plain_root, ignore=shutil.ignore_patterns("__pycache__", ".serena"))
        clone = plain_root / "vendored"
        clone.mkdir()
        (clone / "module.py").write_text("def vendored_function() -> None:\n    pass\n", encoding="utf-8")
        git(clone, "init", "-q")
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", "initial")

        with routing_agent(plain_root.resolve()) as agent:
            path = str(clone.resolve() / "module.py")
            result = call(agent, GetSymbolsOverviewTool, relative_path=path)
            assert "vendored_function" in result
            assert result == call_unrouted(agent, GetSymbolsOverviewTool, relative_path=path)

    def test_git_without_nul_terminated_listing_routes(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = worktree_repo.worktrees["nested"]
        commit_worktree_config(nested.root, read_only=True)
        nested_models_before = worktree_repo.read_models(nested.root)
        use_git_without_nul_listing(tmp_path, monkeypatch)

        path = str(nested.root / MODELS_FILE)
        assert nested.marker in call(agent, GetSymbolsOverviewTool, relative_path=path)
        result = call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert "read_only" in result
        assert worktree_repo.read_models(nested.root) == nested_models_before

    def test_untrusted_active_project_language_server_settings_do_not_apply_in_worktree(self, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        with routing_agent(
            worktree_repo.main,
            trusted_project_path_patterns=[str(sibling.root)],
            ls_specific_settings={"python": {"ls_path": str(worktree_repo.main / "no-such-language-server")}},
        ) as agent:
            assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

    def test_nested_worktree_with_newline_in_path_is_refused_without_nul_listing(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        newline_root = add_worktree(worktree_repo.main, ".claude/worktrees/new\nline", "newline", "newline_only_function")
        commit_worktree_config(newline_root, read_only=True)
        models_before = worktree_repo.read_models(newline_root)
        use_git_without_nul_listing(tmp_path, monkeypatch)

        path = str(newline_root / MODELS_FILE)
        result = call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert str(newline_root) in result
        assert worktree_repo.read_models(newline_root) == models_before

    def test_unlisted_nested_checkout_is_refused_only_without_nul_listing(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = worktree_repo.main / "vendored"
        clone.mkdir()
        (clone / "module.py").write_text("def vendored_function() -> None:\n    pass\n", encoding="utf-8")
        git(clone, "init", "-q")
        path = str(clone / "module.py")

        result = call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert "vendored_function" in result
        assert result == call_unrouted(agent, GetSymbolsOverviewTool, relative_path=path)

        use_git_without_nul_listing(tmp_path, monkeypatch)
        result = call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert "vendored_function" not in result
        assert str(clone) in result

    def test_path_from_routed_symbol_lookup_edits_the_worktree(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        main_before = snapshot_files(worktree_repo.main)
        found = call(agent, FindSymbolTool, name_path_pattern="User", relative_path=str(sibling.root / MODELS_FILE), include_body=True)
        path = paths_under(found, sibling.root)[0]

        result = call(agent, ReplaceSymbolBodyTool, name_path="User", relative_path=path, body="class User:\n    pass")
        assert "class User:\n    pass" in worktree_repo.read_models(sibling.root), result
        assert snapshot_files(worktree_repo.main) == main_before

    @pytest.mark.parametrize(
        "output_form",
        [
            "find_symbol",
            "find_symbol_shortened",
            "find_referencing_symbols",
            "search_for_pattern",
            "find_file",
            "list_dir",
            "diagnostics",
            "replace_in_files_preview",
            "replace_in_files_summary",
            "create_text_file",
            "find_symbol_max_matches",
            "edit_diagnostics",
            "find_declaration",
            pytest.param(
                "find_implementations",
                marks=pytest.mark.skip(reason="pyright does not support textDocument/implementation, so Python yields none"),
            ),
            "safe_delete_refusal",
            "ambiguous_symbol_error",
            "references_without_context",
            "reference_counts_per_file",
            "matched_lines",
            "matched_lines_truncated",
            "match_line_numbers",
            "match_counts_per_file",
        ],
    )
    def test_path_from_routed_result_routes_back(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, output_form: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = worktree_repo.worktrees["sibling"].root
        models = str(root / MODELS_FILE)
        match output_form:
            case "find_symbol":
                result = call(agent, FindSymbolTool, name_path_pattern="User", relative_path=str(root))
            case "find_symbol_shortened":
                result = call(
                    agent, FindSymbolTool, name_path_pattern="User", relative_path=str(root), include_body=True, max_answer_chars=400
                )
                assert "Shortened result" in result, result
            case "find_referencing_symbols":
                result = call(agent, FindReferencingSymbolsTool, name_path="User", relative_path=models)
            case "search_for_pattern":
                result = call(agent, SearchForPatternTool, substring_pattern="class User", relative_path=str(root))
            case "find_file":
                result = call(agent, FindFileTool, file_mask="models.py", relative_path=str(root))
            case "list_dir":
                result = call(agent, ListDirTool, relative_path=str(root / "test_repo"), recursive=False)
            case "diagnostics":
                result = call(agent, GetDiagnosticsForFileTool, relative_path=str(root / "test_repo" / "diagnostics_sample.py"))
            case "replace_in_files_preview":
                result = call(
                    agent, ReplaceInFilesTool, needle="class User", repl="class User", mode="literal", relative_path=str(root), dry_run=True
                )
            case "replace_in_files_summary":
                result = call(agent, ReplaceInFilesTool, needle="class User(", repl="class User(", mode="literal", relative_path=str(root))
                assert result.startswith("Replaced"), result
            case "find_symbol_max_matches":
                result = call(
                    agent, FindSymbolTool, name_path_pattern="User", relative_path=str(root), substring_matching=True, max_matches=1
                )
                assert "max_matches" in result, result
            case "edit_diagnostics":
                monkeypatch.setattr(EditingToolWithDiagnostics, "ENABLE_DIAGNOSTICS", True)
                result = call(agent, ReplaceContentTool, relative_path=models, needle=r"\Z", repl="\nundefined_name_xyz()\n", mode="regex")
                assert DiagnosticsContext.DIAGNOSTICS_KEY in result, result
            case "find_declaration":
                result = call(
                    agent, FindDeclarationTool, relative_path=str(root / "test_repo" / "services.py"), regex=r"import Item, (User)"
                )
            case "safe_delete_refusal":
                result = call(agent, SafeDeleteSymbol, name_path_pattern="User", relative_path=models)
                assert "Cannot delete" in result, result
            case "ambiguous_symbol_error":
                result = call(agent, ReplaceSymbolBodyTool, name_path="__init__", relative_path=models, body="pass")
                assert "multiple" in result, result
            case "references_without_context" | "reference_counts_per_file":
                header = (
                    "References without surrounding lines" if output_form == "references_without_context" else "Reference counts per file"
                )
                result = shortened_result(
                    lambda limit: call(agent, FindReferencingSymbolsTool, name_path="User", relative_path=models, max_answer_chars=limit),
                    header,
                )
            case "matched_lines" | "matched_lines_truncated" | "match_line_numbers" | "match_counts_per_file":
                (root / "test_repo" / "long_lines.py").write_text(
                    "".join(f'value_{i} = "{"x" * 100}"\n' for i in range(30)), encoding="utf-8"
                )
                header = {
                    "matched_lines": "Matched lines per file",
                    "matched_lines_truncated": "Matched lines (text over",
                    "match_line_numbers": "Match lines per file",
                    "match_counts_per_file": "Match counts per file",
                }[output_form]
                result = shortened_result(
                    lambda limit: call(
                        agent,
                        SearchForPatternTool,
                        substring_pattern="value_",
                        relative_path=str(root / "test_repo" / "long_lines.py"),
                        context_lines_after=2,
                        max_answer_chars=limit,
                    ),
                    header,
                )
            case _:
                result = call(agent, CreateTextFileTool, relative_path=str(root / "created.py"), content="x = 1\n")
        python_paths = [p for p in paths_under(result, root) if p.endswith(".py")]
        assert python_paths, result
        assert_path_routes_back(agent, worktree_repo, python_paths[0])

    def test_occurrence_ids_apply_only_under_their_root(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        root = worktree_repo.worktrees["sibling"].root
        sibling_before = worktree_repo.read_models(root)
        main_before = snapshot_files(worktree_repo.main)
        preview = call(
            agent, ReplaceInFilesTool, needle="class User(", repl="class Person(", mode="literal", relative_path=str(root), dry_run=True
        )
        occurrence_ids = re.findall(r"\[([^\]]+)\]", preview)
        assert occurrence_ids, preview

        # applied with no relative_path, i.e. to the main checkout, the worktree's ids are refused
        refused = call(agent, ReplaceInFilesTool, needle="class User(", repl="class Person(", mode="literal", occurrence_ids=occurrence_ids)
        assert "another project root" in refused
        assert snapshot_files(worktree_repo.main) == main_before
        assert worktree_repo.read_models(root) == sibling_before

        # applied in the worktree, they apply there only
        call(
            agent,
            ReplaceInFilesTool,
            needle="class User(",
            repl="class Person(",
            mode="literal",
            relative_path=str(root),
            occurrence_ids=occurrence_ids,
        )
        assert "class Person(" in worktree_repo.read_models(root)
        assert snapshot_files(worktree_repo.main) == main_before

    def test_unrouted_results_name_files_by_relative_path(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        for result in (
            call(agent, FindSymbolTool, name_path_pattern="User"),
            call(agent, SearchForPatternTool, substring_pattern="class User"),
            call(agent, FindFileTool, file_mask="models.py", relative_path="."),
            call(agent, ReplaceInFilesTool, needle="class User(", repl="class Person(", mode="literal", dry_run=True),
        ):
            assert MODELS_FILE in result
            assert str(worktree_repo.main) not in result

    def test_worktree_requiring_another_backend_is_refused(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        commit_worktree_config(sibling.root, language_backend=BuiltinLanguageBackend.JETBRAINS.get_instance())
        result = call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))
        assert sibling.marker not in result
        assert "language_backend" in result

    def test_unlisted_checkout_inside_routed_worktree_is_refused_without_nul_listing(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clone = worktree_repo.worktrees["sibling"].root / "vendored"
        clone.mkdir()
        (clone / "module.py").write_text("def vendored_function() -> None:\n    pass\n", encoding="utf-8")
        git(clone, "init", "-q")
        use_git_without_nul_listing(tmp_path, monkeypatch)

        result = call(agent, GetSymbolsOverviewTool, relative_path=str(clone / "module.py"))
        assert "vendored_function" not in result
        assert str(clone) in result

    def test_truncated_root_from_newline_listing_is_not_routed(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # a worktree whose path continues with a line the newline-terminated listing accepts, beside a plain directory at
        # the truncated path
        add_worktree(worktree_repo.main, ".claude/worktrees/a\nlocked", "truncated", "truncated_only_function")
        plain_dir = worktree_repo.main / ".claude" / "worktrees" / "a"
        (plain_dir / "test_repo").mkdir(parents=True)
        (plain_dir / MODELS_FILE).write_text("def plain_function() -> None:\n    pass\n", encoding="utf-8")
        use_git_without_nul_listing(tmp_path, monkeypatch)

        path = str(plain_dir / MODELS_FILE)
        result = call(agent, GetSymbolsOverviewTool, relative_path=path)
        assert "plain_function" not in result
        assert result == call_unrouted(agent, GetSymbolsOverviewTool, relative_path=path)

    def test_path_from_routed_error_routes_back(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        root = worktree_repo.worktrees["sibling"].root
        main_before = snapshot_files(worktree_repo.main)
        result = call(agent, GetSymbolsOverviewTool, relative_path=str(root / "test_repo" / "missing.py"))
        paths = paths_under(result, root)
        assert paths, result

        call(agent, CreateTextFileTool, relative_path=paths[0], content="x = 1\n")
        assert (root / "test_repo" / "missing.py").exists()
        assert snapshot_files(worktree_repo.main) == main_before

    def test_routed_error_from_language_server_names_the_worktree(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        root = worktree_repo.worktrees["sibling"].root
        result = call(agent, FindSymbolTool, name_path_pattern="VariableContainer", relative_path=str(root / MAIN_ONLY_IGNORED_FILE))
        assert "ignored" in result
        assert f"git worktree {root}" in result

    def test_failed_worktree_project_setup_names_the_worktree(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        commit_worktree_config(sibling.root, ls_specific_settings={"python": {"ls_path": str(sibling.root / "no-such-language-server")}})
        result = call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))
        assert sibling.marker not in result
        assert f"git worktree {sibling.root}" in result

    def search_files(self, agent: SerenaAgent, root: Path, pattern: str) -> str:
        return call(agent, SearchForPatternTool, substring_pattern=pattern, relative_path=str(root))

    def test_gitignore_only_checkout_takes_effect(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        root = worktree_repo.worktrees["sibling"].root
        git(root, "checkout", "-q", "-b", "ignoring-models")
        with open(root / ".gitignore", "a", encoding="utf-8") as f:
            f.write("test_repo/models.py\n")
        git(root, "commit", "-qam", "ignore the models")
        git(root, "checkout", "-q", "feature")
        assert "models.py" in self.search_files(agent, root, "class User")

        git(root, "checkout", "-q", "ignoring-models")
        assert "models.py" not in self.search_files(agent, root, "class User")

    def test_repeated_edits_of_modified_gitignore_take_effect(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        root = worktree_repo.worktrees["sibling"].root
        assert "services.py" in self.search_files(agent, root, "class UserService")

        with open(root / ".gitignore", "a", encoding="utf-8") as f:
            f.write("test_repo/services.py\n")
        assert "services.py" not in self.search_files(agent, root, "class UserService")
        assert "models.py" in self.search_files(agent, root, "class User\\(")

        with open(root / ".gitignore", "a", encoding="utf-8") as f:
            f.write("test_repo/models.py\n")
        assert "models.py" not in self.search_files(agent, root, "class User\\(")

    def test_edit_and_deletion_of_loaded_untracked_gitignore_take_effect(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        root = worktree_repo.worktrees["sibling"].root
        untracked_gitignore = root / "test_repo" / ".gitignore"
        untracked_gitignore.write_text("nothing_here.txt\n", encoding="utf-8")
        assert "models.py" in self.search_files(agent, root, "class User\\(")

        untracked_gitignore.write_text("models.py\n", encoding="utf-8")
        assert "models.py" not in self.search_files(agent, root, "class User\\(")

        untracked_gitignore.unlink()
        assert "models.py" in self.search_files(agent, root, "class User\\(")

    def test_routed_call_cost_does_not_grow_with_ignored_tree(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        root = worktree_repo.worktrees["sibling"].root
        with open(root / ".gitignore", "a", encoding="utf-8") as f:
            f.write("build/\n")
        path = str(root / MODELS_FILE)

        def median_call_seconds() -> float:
            durations = []
            for _ in range(15):
                start = time.perf_counter()
                assert "class User" in call(agent, ReadFileTool, relative_path=path)
                durations.append(time.perf_counter() - start)
            return statistics.median(durations)

        call(agent, ReadFileTool, relative_path=path)
        without_tree = median_call_seconds()
        for i in range(5000):
            directory = root / "build" / f"d{i // 100}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"f{i}.txt").write_text("x\n", encoding="utf-8")
        with_tree = median_call_seconds()

        print(f"routed read_file median: {without_tree * 1000:.1f} ms without, {with_tree * 1000:.1f} ms with 5,000 ignored files")
        assert with_tree < without_tree * 2 + 0.05

    def test_checkout_adding_tracked_gitignore_takes_effect(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        root = worktree_repo.worktrees["sibling"].root
        git(root, "checkout", "-q", "-b", "nested-gitignore")
        (root / "test_repo" / ".gitignore").write_text("models.py\n", encoding="utf-8")
        git(root, "add", str(root / "test_repo" / ".gitignore"))
        git(root, "commit", "-qm", "ignore the models in a nested .gitignore")
        git(root, "checkout", "-q", "feature")
        assert "models.py" in self.search_files(agent, root, "class User\\(")

        git(root, "checkout", "-q", "nested-gitignore")
        assert "models.py" not in self.search_files(agent, root, "class User\\(")

    def test_symlinked_gitignore_is_not_read(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # a branch with a tracked .gitignore that links to a FIFO, which blocks any reader
        root = worktree_repo.worktrees["sibling"].root
        path = str(root / MODELS_FILE)
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        git(root, "checkout", "-q", "-b", "symlinked-gitignore")
        (root / "test_repo" / ".gitignore").symlink_to(fifo)
        git(root, "add", str(root / "test_repo" / ".gitignore"))
        git(root, "commit", "-qm", "link a .gitignore to a FIFO")
        git(root, "checkout", "-q", "feature")
        assert "class User" in call(agent, ReadFileTool, relative_path=path)

        git(root, "checkout", "-q", "symlinked-gitignore")
        agent.serena_config.tool_timeout = 30
        try:
            assert "class User" in call(agent, ReadFileTool, relative_path=path)
        finally:
            # release any reader blocked on the FIFO
            try:
                os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

    def test_info_exclude_edit_replaces_the_project(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = worktree_repo.worktrees["sibling"].root
        assert "models.py" in self.search_files(agent, root, "class User\\(")
        shut_down_roots = record_project_shutdowns(monkeypatch)

        exclude = worktree_repo.main / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as f:
            f.write("unrelated-pattern\n")
        self.search_files(agent, root, "class User\\(")
        wait_for_length(shut_down_roots, 1)
        monkeypatch.undo()
        assert shut_down_roots == [str(root)]

    def test_git_echoing_path_format_routes(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        use_git_echoing_path_format(tmp_path, monkeypatch)
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

    def test_worktree_languages_follow_its_configuration(self, agent: SerenaAgent, worktree_repo: WorktreeRepo) -> None:
        root = worktree_repo.worktrees["sibling"].root
        commit_worktree_config(root, language_servers=[LanguageServerId.PYTHON_TY])
        sample = os.path.join("test_repo", "diagnostics_sample.py")

        assert '"source": "ty"' in call(agent, GetDiagnosticsForFileTool, relative_path=str(root / sample))
        assert '"source": "Pyright"' in call(agent, GetDiagnosticsForFileTool, relative_path=sample)

    @pytest.mark.parametrize("directory", ["new_directory", "same_directory"])
    def test_worktree_recreated_from_read_only_branch_refuses_next_edit(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, directory: str
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        path = str(sibling.root / MODELS_FILE)

        # prepare a read-only branch, leaving the worktree on its branch without a configuration of its own
        git(sibling.root, "checkout", "-q", "-b", "read-only")
        commit_worktree_config(sibling.root, read_only=True)
        git(sibling.root, "checkout", "-q", "feature")
        call(
            agent,
            ReplaceContentTool,
            relative_path=path,
            needle="class User(BaseModel):",
            repl="class User(BaseModel):  # one",
            mode="literal",
        )
        assert "class User(BaseModel):  # one" in worktree_repo.read_models(sibling.root)

        # recreate the worktree at the same path from the read-only branch
        if directory == "new_directory":
            git(worktree_repo.main, "worktree", "remove", "--force", str(sibling.root))
            git(worktree_repo.main, "worktree", "add", "-q", str(sibling.root), "read-only")
        else:
            recreate_worktree_in_place(worktree_repo.main, sibling.root, "read-only", tmp_path / "old-git-dir")
        models_before = worktree_repo.read_models(sibling.root)

        result = call(agent, ReplaceContentTool, relative_path=path, needle="class User(BaseModel):", repl="class Edited:", mode="literal")
        assert "read_only" in result
        assert worktree_repo.read_models(sibling.root) == models_before

    def test_replace_content_works_as_first_call_and_after_replacement(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        path = str(sibling.root / MODELS_FILE)
        main_before = snapshot_files(worktree_repo.main)
        git(sibling.root, "checkout", "-q", "-b", "configured")
        commit_worktree_config(sibling.root)
        git(sibling.root, "checkout", "-q", "feature")

        result = call(
            agent,
            ReplaceContentTool,
            relative_path=path,
            needle="class User(BaseModel):",
            repl="class User(BaseModel):  # one",
            mode="literal",
        )
        assert "class User(BaseModel):  # one" in worktree_repo.read_models(sibling.root), result

        # change the worktree's configuration, which replaces its project
        shut_down_roots = record_project_shutdowns(monkeypatch)
        git(sibling.root, "checkout", "-q", "-f", "configured")
        result = call(
            agent,
            ReplaceContentTool,
            relative_path=path,
            needle="class User(BaseModel):",
            repl="class User(BaseModel):  # two",
            mode="literal",
        )
        wait_for_length(shut_down_roots, 1)
        monkeypatch.undo()
        assert "class User(BaseModel):  # two" in worktree_repo.read_models(sibling.root), result
        assert shut_down_roots == [str(sibling.root)]
        assert snapshot_files(worktree_repo.main) == main_before

    def test_timed_out_rename_keeps_its_project_until_its_thread_ends(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        path = str(sibling.root / MODELS_FILE)
        services = sibling.root / "test_repo" / "services.py"
        main_before = snapshot_files(worktree_repo.main)

        # prepare a branch with a configuration of its own (which does not ignore the main-only ignored file), leaving
        # the worktree on its branch, and start the worktree's project
        git(sibling.root, "checkout", "-q", "-b", "configured")
        commit_worktree_config(sibling.root)
        git(sibling.root, "checkout", "-q", "feature")
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=path)

        # make the rename wait, once its project is pinned, until released
        rename_tool = agent.get_tool(RenameSymbolTool)
        original_apply = rename_tool.apply
        rename_projects: list[Project] = []
        may_proceed = threading.Event()
        rename_finished = threading.Event()

        def waiting_apply(**kwargs: Any) -> str:
            rename_projects.append(agent.get_active_project_or_raise())
            may_proceed.wait(timeout=120)
            try:
                return original_apply(**kwargs)
            finally:
                rename_finished.set()

        monkeypatch.setattr(rename_tool, "apply", waiting_apply)
        shut_down_projects = record_shut_down_projects(monkeypatch)

        # let the rename time out, then change the configuration and call into the worktree again
        agent.serena_config.tool_timeout = 1
        try:
            timed_out = call(agent, RenameSymbolTool, name_path="User", relative_path=path, new_name="Account")
        finally:
            agent.serena_config.tool_timeout = 600
        assert "timed out" in timed_out
        git(sibling.root, "checkout", "-q", "configured")
        assert "VariableContainer" in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MAIN_ONLY_IGNORED_FILE))
        [rename_project] = rename_projects
        assert all(shut_down.project is not rename_project for shut_down in shut_down_projects)
        assert is_running(rename_project)

        # the rename completes in full, and its project is shut down only after the rename's task has ended
        may_proceed.set()
        assert rename_finished.wait(timeout=120)
        deadline = time.monotonic() + 60
        while not any(shut_down.project is rename_project for shut_down in shut_down_projects) and time.monotonic() < deadline:
            time.sleep(0.1)
        [rename_shutdown] = [shut_down for shut_down in shut_down_projects if shut_down.project is rename_project]
        assert rename_shutdown.are_servers_stopped()
        models = worktree_repo.read_models(sibling.root)
        for reference in ("class Account(BaseModel):", "-> Account:", "return Account(", "class LoggableUser(Account, Loggable):"):
            assert reference in models
        services_text = services.read_text(encoding="utf-8")
        for reference in ("from .models import Item, Account", "dict[str, Account]", "user = Account(", "-> list[Account]:"):
            assert reference in services_text
        assert snapshot_files(worktree_repo.main) == main_before

        # later calls run against the fresh project
        assert "VariableContainer" in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MAIN_ONLY_IGNORED_FILE))

    def test_worktree_projects_live_until_their_worktree_is_removed_or_the_agent_shuts_down(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktrees = [
            worktree_repo.worktrees["sibling"],
            worktree_repo.worktrees["nested"],
            Worktree(root=add_worktree(worktree_repo.main, "../wt3", "third", "third_only_function"), marker="third_only_function"),
        ]
        shut_down_projects = record_shut_down_projects(monkeypatch)

        # use the worktrees in turn, twice
        for worktree in worktrees * 2:
            assert worktree.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(worktree.root / MODELS_FILE))
        assert shut_down_projects == []

        # removing a worktree stops its servers at the next routed call into another worktree
        removed = worktrees.pop()
        git(worktree_repo.main, "worktree", "remove", "--force", str(removed.root))
        assert worktrees[0].marker in call(agent, GetSymbolsOverviewTool, relative_path=str(worktrees[0].root / MODELS_FILE))
        wait_for_length(shut_down_projects, 1)
        assert [shut_down.project.project_root for shut_down in shut_down_projects] == [str(removed.root)]
        assert shut_down_projects[0].are_servers_stopped()

        # shutting down the agent stops the servers of the remaining worktrees
        agent.on_shutdown(timeout=5)
        worktree_shutdowns = [
            shut_down for shut_down in shut_down_projects[1:] if shut_down.project.project_root != str(worktree_repo.main)
        ]
        assert sorted(shut_down.project.project_root for shut_down in worktree_shutdowns) == sorted(str(w.root) for w in worktrees)
        assert all(shut_down.are_servers_stopped() for shut_down in worktree_shutdowns)

    def test_worktree_recreated_in_place_stops_its_servers_at_next_routed_call(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        nested = worktree_repo.worktrees["nested"]
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))
        shut_down_projects = record_shut_down_projects(monkeypatch)

        recreate_worktree_in_place(worktree_repo.main, sibling.root, "feature", tmp_path / "old-git-dir")
        assert nested.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(nested.root / MODELS_FILE))
        wait_for_length(shut_down_projects, 1)
        assert [shut_down.project.project_root for shut_down in shut_down_projects] == [str(sibling.root)]
        assert shut_down_projects[0].are_servers_stopped()
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

    @pytest.mark.parametrize("tool_name", ["rename_symbol", "get_symbols_overview"])
    def test_timed_out_call_does_not_write_into_worktree_recreated_meanwhile(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        nested = worktree_repo.worktrees["nested"]
        path = str(sibling.root / MODELS_FILE)
        main_before = snapshot_files(worktree_repo.main)
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=path)

        # make the call wait, once its project is pinned, until released
        tool_class: type[Tool]
        kwargs: dict[str, Any]
        if tool_name == "rename_symbol":
            tool_class, kwargs = RenameSymbolTool, {"name_path": "User", "relative_path": path, "new_name": "Account"}
        else:
            # a file whose symbols the project has not cached yet, so that the call's cache save has something to write
            tool_class, kwargs = GetSymbolsOverviewTool, {"relative_path": str(sibling.root / "test_repo" / "services.py")}
        waiting_tool = agent.get_tool(tool_class)
        original_apply = waiting_tool.apply
        apply_errors: list[Exception] = []
        may_proceed = threading.Event()
        apply_finished = threading.Event()

        def waiting_apply(**apply_kwargs: Any) -> str:
            may_proceed.wait(timeout=120)
            try:
                return original_apply(**apply_kwargs)
            except Exception as e:
                apply_errors.append(e)
                raise
            finally:
                apply_finished.set()

        monkeypatch.setattr(waiting_tool, "apply", waiting_apply)
        shut_down_projects = record_shut_down_projects(monkeypatch)

        # let the call time out, then recreate the worktree at the same path while the call still waits
        agent.serena_config.tool_timeout = 1
        try:
            timed_out = call(agent, tool_class, **kwargs)
        finally:
            agent.serena_config.tool_timeout = 600
        assert "timed out" in timed_out
        recreate_worktree_at_same_path(worktree_repo.main, sibling.root, "feature", tmp_path / "old-worktree")
        recreated_before = snapshot_files(sibling.root)

        # none of the call's writes land in the new checkout (or the main checkout); a rename fails at its first write
        may_proceed.set()
        assert apply_finished.wait(timeout=120)
        assert snapshot_files(sibling.root) == recreated_before
        assert snapshot_files(worktree_repo.main) == main_before
        if tool_name == "rename_symbol":
            assert len(apply_errors) == 1
            assert "removed or replaced" in str(apply_errors[0])

        # neither the call's cache save nor the old project's shutdown writes to the new checkout's Serena data folder
        assert nested.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(nested.root / MODELS_FILE))
        wait_for_length(shut_down_projects, 1)
        assert [shut_down.project.project_root for shut_down in shut_down_projects] == [str(sibling.root)]
        assert shut_down_projects[0].are_servers_stopped()
        assert not (sibling.root / ".serena").exists()

    def test_edit_and_deletion_of_loaded_gitignore_in_submodule_take_effect(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path
    ) -> None:
        root = worktree_repo.worktrees["sibling"].root

        # a submodule of the worktree, whose own .gitignore Serena loads
        source = tmp_path / "submodule-source"
        source.mkdir()
        (source / "vendored_module.py").write_text("class VendoredThing:\n    pass\n", encoding="utf-8")
        (source / ".gitignore").write_text("nothing_here.txt\n", encoding="utf-8")
        git(source, "init", "-q", "-b", "main")
        git(source, "add", "-A")
        git(source, "commit", "-qm", "initial")
        git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "vendored")
        git(root, "commit", "-qm", "add a submodule")
        submodule_gitignore = root / "vendored" / ".gitignore"
        assert "vendored_module.py" in self.search_files(agent, root, "class VendoredThing")

        submodule_gitignore.write_text("vendored_module.py\n", encoding="utf-8")
        assert "vendored_module.py" not in self.search_files(agent, root, "class VendoredThing")

        submodule_gitignore.unlink()
        assert "vendored_module.py" in self.search_files(agent, root, "class VendoredThing")

    def test_worktree_data_folder_configured_outside_the_worktree_holds_its_caches(
        self, worktree_repo: WorktreeRepo, tmp_path: Path
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        data_root = tmp_path / "serena-data"
        path = str(sibling.root / MODELS_FILE)

        with routing_agent(worktree_repo.main, project_serena_folder_location=str(data_root / "$projectFolderName")) as agent:
            assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=path)
            result = call(
                agent,
                ReplaceContentTool,
                relative_path=path,
                needle="class User(BaseModel):",
                repl="class User(BaseModel):  # one",
                mode="literal",
            )
            assert "class User(BaseModel):  # one" in worktree_repo.read_models(sibling.root), result
            assert "UserService" in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / "test_repo" / "services.py"))

        assert list((data_root / sibling.root.name / "cache").rglob("*.pkl"))
        assert not (sibling.root / ".serena").exists()

    def test_cache_folder_swapped_for_a_link_out_of_the_worktree_is_not_written_through(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

        # replace the cache folders of the worktree's project with a link to a directory outside the worktree
        outside = tmp_path / "outside"
        outside.mkdir()
        cache_folder = sibling.root / ".serena" / "cache"
        shutil.rmtree(cache_folder)
        cache_folder.symlink_to(outside, target_is_directory=True)

        # calls whose cache saves have something to write succeed without the caches being written through the link,
        # and so do later calls, whether or not they write anything
        assert "UserService" in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / "test_repo" / "services.py"))
        assert "class User" in call(agent, ReadFileTool, relative_path=str(sibling.root / MODELS_FILE))
        assert "Item" in call(agent, FindSymbolTool, name_path_pattern="Item", relative_path=str(sibling.root / MODELS_FILE))
        assert list(outside.iterdir()) == []

    def test_timed_out_edit_is_refused_after_checkout_to_read_only_branch(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        path = str(sibling.root / MODELS_FILE)
        main_before = snapshot_files(worktree_repo.main)

        # prepare a read-only branch, leaving the worktree on its branch without a configuration of its own
        git(sibling.root, "checkout", "-q", "-b", "read-only")
        commit_worktree_config(sibling.root, read_only=True)
        git(sibling.root, "checkout", "-q", "feature")
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=path)

        # make the rename wait, once its project is pinned, until released
        rename_tool = agent.get_tool(RenameSymbolTool)
        original_apply = rename_tool.apply
        rename_errors: list[Exception] = []
        may_proceed = threading.Event()
        rename_finished = threading.Event()

        def waiting_apply(**kwargs: Any) -> str:
            may_proceed.wait(timeout=120)
            try:
                return original_apply(**kwargs)
            except Exception as e:
                rename_errors.append(e)
                raise
            finally:
                rename_finished.set()

        monkeypatch.setattr(rename_tool, "apply", waiting_apply)

        # let the rename time out, then check the read-only branch out in the worktree while the rename still waits
        agent.serena_config.tool_timeout = 1
        try:
            timed_out = call(agent, RenameSymbolTool, name_path="User", relative_path=path, new_name="Account")
        finally:
            agent.serena_config.tool_timeout = 600
        assert "timed out" in timed_out
        git(sibling.root, "checkout", "-q", "-f", "read-only")
        checked_out = snapshot_files(sibling.root)

        # the rename's writes are refused, naming the setting, and neither checkout changes
        may_proceed.set()
        assert rename_finished.wait(timeout=120)
        assert len(rename_errors) == 1
        assert "read_only" in str(rename_errors[0])
        assert snapshot_files(sibling.root) == checked_out
        assert snapshot_files(worktree_repo.main) == main_before

    def test_worktree_projects_survive_a_call_while_a_project_of_another_repository_is_active(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

        # a project of another repository, made active as on activating it
        other = create_worktree_repo(tmp_path / "other")
        other_project = Project(
            project_root=str(other.main), project_config=make_project_config("other_repo"), serena_config=agent.serena_config
        )
        other_project.create_language_server_manager()
        shut_down_projects = record_shut_down_projects(monkeypatch)
        try:
            with agent.active_project_context(other_project):
                assert "User" in call(agent, GetSymbolsOverviewTool, relative_path=str(other.main / MODELS_FILE))

            # the worktree's project was neither removed nor replaced, so its servers keep running
            wait_for_length(shut_down_projects, 1, timeout=3)
            assert shut_down_projects == []
            assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))
            wait_for_length(shut_down_projects, 1, timeout=3)
            assert shut_down_projects == []
        finally:
            monkeypatch.undo()
            other_project.shutdown()

    def test_rename_edit_with_a_target_outside_the_worktree_leaves_the_worktree_unchanged(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        path = str(sibling.root / MODELS_FILE)
        outside_file = tmp_path / "outside.py"
        outside_file.write_text("def outside_function() -> None:\n    pass\n", encoding="utf-8")
        sibling_before = snapshot_files(sibling.root)

        # a language server whose rename edit changes a file of the worktree first, then one outside it
        def outside_reaching_rename_edit(self: SolidLanguageServer, **kwargs: Any) -> dict:
            insertion = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "newText": "# renamed\n"}
            return {"changes": {Path(path).as_uri(): [insertion], outside_file.as_uri(): [insertion]}}

        monkeypatch.setattr(SolidLanguageServer, "request_rename_symbol_edit", outside_reaching_rename_edit)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=path, new_name="Account")
        assert "Refusing to write" in result
        assert snapshot_files(sibling.root) == sibling_before
        assert outside_file.read_text(encoding="utf-8") == "def outside_function() -> None:\n    pass\n"

    def test_data_folder_linked_out_of_the_worktree_is_refused(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        outside = tmp_path / "outside-data"
        outside.mkdir()
        link = sibling.root / ".serena"
        link.symlink_to(outside, target_is_directory=True)

        result = call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))
        assert sibling.marker not in result
        assert str(link) in result
        assert list(outside.iterdir()) == []

    def test_timed_out_replace_in_files_is_refused_after_checkout_to_read_only_branch(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        main_before = snapshot_files(worktree_repo.main)

        # prepare a read-only branch, leaving the worktree on its branch without a configuration of its own
        git(sibling.root, "checkout", "-q", "-b", "read-only")
        commit_worktree_config(sibling.root, read_only=True)
        git(sibling.root, "checkout", "-q", "feature")
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

        # make the replacement wait, once its project is pinned, until released
        replace_tool = agent.get_tool(ReplaceInFilesTool)
        original_apply = replace_tool.apply
        replace_errors: list[Exception] = []
        may_proceed = threading.Event()
        replace_finished = threading.Event()

        def waiting_apply(**kwargs: Any) -> str:
            may_proceed.wait(timeout=120)
            try:
                return original_apply(**kwargs)
            except Exception as e:
                replace_errors.append(e)
                raise
            finally:
                replace_finished.set()

        monkeypatch.setattr(replace_tool, "apply", waiting_apply)

        # let the replacement time out, then check the read-only branch out in the worktree while it still waits
        agent.serena_config.tool_timeout = 1
        try:
            timed_out = call(agent, ReplaceInFilesTool, needle="User", repl="Account", mode="literal", relative_path=str(sibling.root))
        finally:
            agent.serena_config.tool_timeout = 600
        assert "timed out" in timed_out
        git(sibling.root, "checkout", "-q", "-f", "read-only")
        checked_out = snapshot_files(sibling.root)

        # the replacement's writes are refused before any is made, naming the setting
        may_proceed.set()
        assert replace_finished.wait(timeout=120)
        assert len(replace_errors) == 1
        assert "read_only" in str(replace_errors[0])
        assert snapshot_files(sibling.root) == checked_out
        assert snapshot_files(worktree_repo.main) == main_before

    def _make_read_only_after_first_write(
        self,
        worktree_root: Path,
        monkeypatch: pytest.MonkeyPatch,
        after_first_write: Callable[[], None] | None = None,
        write_number: int = 1,
    ) -> list[str]:
        """
        Makes the editors' writes call `after_first_write` (by default, commit a read_only configuration to the worktree)
        right after their write numbered `write_number` (by default, the first).

        :return: a list which receives the path of each write the editors make
        """
        original_write = code_editor_module.write_file_atomic
        written_paths: list[str] = []

        def write_then_change(path: str, content: str, **kwargs: Any) -> None:
            original_write(path, content, **kwargs)
            written_paths.append(path)
            if len(written_paths) == write_number:
                if after_first_write is None:
                    commit_worktree_config(worktree_root, read_only=True)
                else:
                    after_first_write()

        monkeypatch.setattr(code_editor_module, "write_file_atomic", write_then_change)
        return written_paths

    @staticmethod
    def _fake_two_file_rename_edit(monkeypatch: pytest.MonkeyPatch, first: Path, second: Path) -> None:
        """
        Makes the language server's rename edit insert a line into two files, in this order.
        """

        def two_file_rename_edit(self: SolidLanguageServer, **kwargs: Any) -> dict:
            insertion = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "newText": "# renamed\n"}
            return {"changes": {first.as_uri(): [insertion], second.as_uri(): [insertion]}}

        monkeypatch.setattr(SolidLanguageServer, "request_rename_symbol_edit", two_file_rename_edit)

    def test_configuration_change_during_a_rename_batch_rolls_it_back(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A rename's writes are checked against the configuration as now on disk before the first and again at each write;
        a write refused mid-batch for the configuration (here, read_only, committed after the first file is written)
        rolls the batch's earlier writes back, so the worktree is left as before the batch.
        """
        sibling = worktree_repo.worktrees["sibling"]
        models = sibling.root / MODELS_FILE
        services = sibling.root / "test_repo" / "services.py"
        self._fake_two_file_rename_edit(monkeypatch, models, services)
        before = snapshot_files(sibling.root)
        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(models), new_name="Account")
        assert len(written_paths) == 1, result
        assert "read_only" in result
        assert "rolled back" in result
        assert snapshot_files(sibling.root) == before

    def test_configuration_change_during_a_replace_in_files_batch_rolls_it_back(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        As for a rename: a replace_in_files write refused mid-batch for the configuration rolls the batch's earlier
        writes back, so the worktree is left as before the batch.
        """
        sibling = worktree_repo.worktrees["sibling"]
        for path in (sibling.root / MODELS_FILE, sibling.root / "test_repo" / "services.py"):
            path.write_text("# batch marker\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
        before = snapshot_files(sibling.root)
        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch)

        result = call(
            agent, ReplaceInFilesTool, needle="# batch marker", repl="# batch done", mode="literal", relative_path=str(sibling.root)
        )
        assert len(written_paths) == 1, result
        assert "read_only" in result
        assert "rolled back" in result
        assert snapshot_files(sibling.root) == before

    def test_worktree_replaced_during_a_rename_batch_rolls_nothing_back(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A rename write refused mid-batch because the worktree was replaced rolls nothing back: the batch's earlier write
        stays in the old checkout, and the new one is not written to.
        """
        sibling = worktree_repo.worktrees["sibling"]
        models = sibling.root / MODELS_FILE
        services = sibling.root / "test_repo" / "services.py"
        self._fake_two_file_rename_edit(monkeypatch, models, services)
        kept_dir = tmp_path / "old-worktree"
        written_paths = self._make_read_only_after_first_write(
            sibling.root,
            monkeypatch,
            after_first_write=lambda: recreate_worktree_at_same_path(worktree_repo.main, sibling.root, "feature", kept_dir),
        )

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(models), new_name="Account")
        assert len(written_paths) == 1, result
        assert "removed or replaced" in result
        assert "rolled back" not in result
        assert "stay in place" in result
        assert (kept_dir / "root" / MODELS_FILE).read_text(encoding="utf-8").startswith("# renamed\n")
        assert not models.read_text(encoding="utf-8").startswith("# renamed\n")
        assert not services.read_text(encoding="utf-8").startswith("# renamed\n")

    @staticmethod
    def _fake_rename_edit(monkeypatch: pytest.MonkeyPatch, document_changes: list[dict]) -> None:
        """
        Makes the language server's rename edit consist of the given document changes.
        """

        def rename_edit(self: SolidLanguageServer, **kwargs: Any) -> dict:
            return {"documentChanges": document_changes}

        monkeypatch.setattr(SolidLanguageServer, "request_rename_symbol_edit", rename_edit)

    @staticmethod
    def _insertion_into(path: Path) -> dict:
        insertion = {"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}, "newText": "# renamed\n"}
        return {"textDocument": {"uri": path.as_uri(), "version": None}, "edits": [insertion]}

    def test_configuration_change_during_a_batch_with_a_rename_rolls_the_rename_back(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        models = sibling.root / MODELS_FILE
        utils = sibling.root / "test_repo" / "utils.py"
        self._fake_rename_edit(
            monkeypatch,
            [
                {"kind": "rename", "oldUri": utils.as_uri(), "newUri": (utils.parent / "renamed_utils.py").as_uri()},
                self._insertion_into(models),
                self._insertion_into(sibling.root / "test_repo" / "services.py"),
            ],
        )
        before = snapshot_files(sibling.root)
        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(models), new_name="Account")
        assert len(written_paths) == 1, result
        assert "read_only" in result
        assert "rolled back" in result
        assert snapshot_files(sibling.root) == before

    def test_path_turned_into_a_symlink_after_its_batch_write_is_not_written_through_on_rollback(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        models = sibling.root / MODELS_FILE
        precious = sibling.root / "precious.txt"
        precious.write_text("untracked and precious\n", encoding="utf-8")
        self._fake_rename_edit(
            monkeypatch, [self._insertion_into(models), self._insertion_into(sibling.root / "test_repo" / "services.py")]
        )

        # after the batch's first write, turn the written file into a link to another file of the worktree, and make
        # the configuration read_only
        def link_and_make_read_only() -> None:
            models.unlink()
            models.symlink_to(precious)
            commit_worktree_config(sibling.root, read_only=True)

        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch, after_first_write=link_and_make_read_only)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(models), new_name="Account")
        assert len(written_paths) == 1, result
        assert "read_only" in result
        assert f"{models}, which are left changed" in result
        assert precious.read_text(encoding="utf-8") == "untracked and precious\n"
        assert models.is_symlink()

    def test_rename_onto_an_existing_file_is_refused_before_any_write(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        models = sibling.root / MODELS_FILE
        services = sibling.root / "test_repo" / "services.py"
        self._fake_rename_edit(
            monkeypatch,
            [
                self._insertion_into(models),
                {"kind": "rename", "oldUri": (sibling.root / "test_repo" / "utils.py").as_uri(), "newUri": services.as_uri()},
            ],
        )
        before = snapshot_files(sibling.root)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(models), new_name="Account")
        assert f"{services} already exists" in result
        assert snapshot_files(sibling.root) == before

    def test_configuration_change_after_editing_a_renamed_file_rolls_back_both(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        utils = sibling.root / "test_repo" / "utils.py"
        renamed_utils = utils.parent / "renamed_utils.py"
        self._fake_rename_edit(
            monkeypatch,
            [
                {"kind": "rename", "oldUri": utils.as_uri(), "newUri": renamed_utils.as_uri()},
                self._insertion_into(renamed_utils),
                self._insertion_into(sibling.root / "test_repo" / "services.py"),
            ],
        )
        before = snapshot_files(sibling.root)
        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(sibling.root / MODELS_FILE), new_name="Account")
        assert len(written_paths) == 1, result
        assert "rolled back" in result
        assert snapshot_files(sibling.root) == before

    def test_rename_target_appearing_mid_batch_rolls_the_batch_back(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        models = sibling.root / MODELS_FILE
        target = sibling.root / "test_repo" / "renamed_utils.py"
        self._fake_rename_edit(
            monkeypatch,
            [
                self._insertion_into(models),
                {"kind": "rename", "oldUri": (sibling.root / "test_repo" / "utils.py").as_uri(), "newUri": target.as_uri()},
            ],
        )

        # the rename's target, free at the pre-check, appears after the batch's first write
        written_paths = self._make_read_only_after_first_write(
            sibling.root, monkeypatch, after_first_write=lambda: target.write_text("# appeared\n", encoding="utf-8")
        )

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(models), new_name="Account")
        assert len(written_paths) == 1, result
        assert f"{target} already exists" in result
        assert "rolled back" in result
        assert not models.read_text(encoding="utf-8").startswith("# renamed\n")

    def test_data_folder_turning_into_a_link_out_of_the_worktree_before_the_build_is_refused(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        outside = tmp_path / "outside-data"
        outside.mkdir()
        (sibling.root / ".serena").symlink_to(outside, target_is_directory=True)

        # the link appears after the check made before the build
        monkeypatch.setattr(WorktreeRouter, "_check_data_folder_location", lambda self, project_root: None)

        result = call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))
        assert sibling.marker not in result
        assert "leads out of the worktree" in result
        assert not (outside / "cache").exists()

    def test_rollback_after_editing_renaming_and_editing_a_file_again_restores_it(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        utils = sibling.root / "test_repo" / "utils.py"
        renamed_utils = utils.parent / "renamed_utils.py"
        self._fake_rename_edit(
            monkeypatch,
            [
                self._insertion_into(utils),
                {"kind": "rename", "oldUri": utils.as_uri(), "newUri": renamed_utils.as_uri()},
                self._insertion_into(renamed_utils),
                self._insertion_into(sibling.root / "test_repo" / "services.py"),
            ],
        )
        before = snapshot_files(sibling.root)
        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch, write_number=2)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(sibling.root / MODELS_FILE), new_name="Account")
        assert len(written_paths) == 2, result
        assert "rolled back" in result
        assert snapshot_files(sibling.root) == before

    def test_rollback_across_a_directory_rename_restores_the_files_beneath_it(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        package = sibling.root / "pkg"
        package.mkdir()
        (package / "module.py").write_text("def packaged_function() -> None:\n    pass\n", encoding="utf-8")
        renamed_package = sibling.root / "renamed_pkg"
        self._fake_rename_edit(
            monkeypatch,
            [
                self._insertion_into(package / "module.py"),
                {"kind": "rename", "oldUri": package.as_uri(), "newUri": renamed_package.as_uri()},
                self._insertion_into(renamed_package / "module.py"),
                self._insertion_into(sibling.root / "test_repo" / "services.py"),
            ],
        )
        before = snapshot_files(sibling.root)
        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch, write_number=2)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(sibling.root / MODELS_FILE), new_name="Account")
        assert len(written_paths) == 2, result
        assert "rolled back" in result
        assert snapshot_files(sibling.root) == before

    @pytest.mark.parametrize("tool_name", ["replace_content", "create_text_file"])
    @pytest.mark.parametrize("change", ["recreated", "read_only"])
    def test_timed_out_single_file_write_is_refused_at_the_sink(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str, change: str
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        main_before = snapshot_files(worktree_repo.main)
        if change == "read_only":
            git(sibling.root, "checkout", "-q", "-b", "read-only")
            commit_worktree_config(sibling.root, read_only=True)
            git(sibling.root, "checkout", "-q", "feature")
        assert sibling.marker in call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))

        tool_class: type[Tool]
        kwargs: dict[str, Any]
        if tool_name == "replace_content":
            tool_class = ReplaceContentTool
            kwargs = {
                "relative_path": str(sibling.root / MODELS_FILE),
                "needle": "class User(BaseModel):",
                "repl": "class Edited:",
                "mode": "literal",
            }
        else:
            tool_class = CreateTextFileTool
            kwargs = {"relative_path": str(sibling.root / "created.txt"), "content": "created\n"}

        # make the call wait, once its project is pinned, until released
        waiting_tool = agent.get_tool(tool_class)
        original_apply = waiting_tool.apply
        apply_errors: list[Exception] = []
        may_proceed = threading.Event()
        apply_finished = threading.Event()

        def waiting_apply(**apply_kwargs: Any) -> str:
            may_proceed.wait(timeout=120)
            try:
                return original_apply(**apply_kwargs)
            except Exception as e:
                apply_errors.append(e)
                raise
            finally:
                apply_finished.set()

        monkeypatch.setattr(waiting_tool, "apply", waiting_apply)

        # let the call time out, then change the worktree while it waits
        agent.serena_config.tool_timeout = 1
        try:
            assert "timed out" in call(agent, tool_class, **kwargs)
        finally:
            agent.serena_config.tool_timeout = 600
        if change == "recreated":
            recreate_worktree_at_same_path(worktree_repo.main, sibling.root, "feature", tmp_path / "old-worktree")
        else:
            git(sibling.root, "checkout", "-q", "-f", "read-only")
        changed_before = snapshot_files(sibling.root)

        # the write is refused at its sink, and neither checkout changes
        may_proceed.set()
        assert apply_finished.wait(timeout=120)
        assert len(apply_errors) == 1
        assert ("removed or replaced" if change == "recreated" else "read_only") in str(apply_errors[0])
        assert snapshot_files(sibling.root) == changed_before
        assert snapshot_files(worktree_repo.main) == main_before

    def test_rename_of_an_entry_behind_a_directory_link_out_of_the_worktree_is_refused(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        utils = sibling.root / "test_repo" / "utils.py"

        # a directory link out of the worktree, holding a link back to a file of the worktree
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "link").symlink_to(utils)
        (sibling.root / "out").symlink_to(outside, target_is_directory=True)
        self._fake_rename_edit(
            monkeypatch,
            [{"kind": "rename", "oldUri": (sibling.root / "out" / "link").as_uri(), "newUri": (sibling.root / "moved").as_uri()}],
        )
        before = snapshot_files(sibling.root)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(sibling.root / MODELS_FILE), new_name="Account")
        assert "does not lie inside the git worktree" in result
        assert (outside / "link").is_symlink()
        assert not os.path.lexists(sibling.root / "moved")
        assert snapshot_files(sibling.root) == before

    def test_case_only_rename_routes_and_rolls_back(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        utils = sibling.root / "test_repo" / "utils.py"
        if not os.path.exists(utils.parent / "UTILS.PY"):
            pytest.skip("the file system is case-sensitive, so a case-only rename is an ordinary rename there")
        self._fake_rename_edit(
            monkeypatch,
            [
                {"kind": "rename", "oldUri": utils.as_uri(), "newUri": (utils.parent / "Utils.py").as_uri()},
                self._insertion_into(sibling.root / "test_repo" / "services.py"),
                self._insertion_into(sibling.root / MODELS_FILE),
            ],
        )
        before = snapshot_files(sibling.root)
        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(sibling.root / MODELS_FILE), new_name="Account")
        assert len(written_paths) == 1, result
        assert "rolled back" in result
        assert snapshot_files(sibling.root) == before

    def test_rollback_after_a_path_is_vacated_and_reoccupied_restores_both_files(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        package = sibling.root / "test_repo"
        b_path, c_path = package / "b_module.py", package / "c_module.py"
        self._fake_rename_edit(
            monkeypatch,
            [
                {"kind": "rename", "oldUri": (package / "utils.py").as_uri(), "newUri": b_path.as_uri()},
                self._insertion_into(b_path),
                {"kind": "rename", "oldUri": b_path.as_uri(), "newUri": c_path.as_uri()},
                {"kind": "rename", "oldUri": (package / "models.py").as_uri(), "newUri": b_path.as_uri()},
                self._insertion_into(b_path),
                self._insertion_into(package / "services.py"),
            ],
        )
        before = snapshot_files(sibling.root)
        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch, write_number=2)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(sibling.root / MODELS_FILE), new_name="Account")
        assert len(written_paths) == 2, result
        assert "rolled back" in result
        assert snapshot_files(sibling.root) == before

    def test_rename_of_the_worktree_root_itself_is_refused(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        self._fake_rename_edit(
            monkeypatch, [{"kind": "rename", "oldUri": sibling.root.as_uri(), "newUri": (sibling.root / "moved").as_uri()}]
        )
        before = snapshot_files(sibling.root)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(sibling.root / MODELS_FILE), new_name="Account")
        assert "does not lie inside the git worktree" in result
        assert snapshot_files(sibling.root) == before

    def test_renames_of_a_batch_are_checked_in_their_order(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        package = sibling.root / "test_repo"
        services_before = (package / "services.py").read_bytes()
        models_before = (package / "models.py").read_bytes()

        # a target that holds an entry is free once an earlier rename of the batch has moved it away
        self._fake_rename_edit(
            monkeypatch,
            [
                {"kind": "rename", "oldUri": (package / "services.py").as_uri(), "newUri": (package / "moved_services.py").as_uri()},
                {"kind": "rename", "oldUri": (package / "models.py").as_uri(), "newUri": (package / "services.py").as_uri()},
            ],
        )
        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(package / "models.py"), new_name="Account")
        assert "already exists" not in result, result
        assert (package / "moved_services.py").read_bytes() == services_before
        assert (package / "services.py").read_bytes() == models_before

        # a target taken by an earlier rename of the batch is not free
        self._fake_rename_edit(
            monkeypatch,
            [
                {"kind": "rename", "oldUri": (package / "utils.py").as_uri(), "newUri": (package / "b_module.py").as_uri()},
                {"kind": "rename", "oldUri": (package / "services.py").as_uri(), "newUri": (package / "b_module.py").as_uri()},
            ],
        )
        before = snapshot_files(sibling.root)
        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(package / "services.py"), new_name="Account")
        assert f"{package / 'b_module.py'} already exists" in result
        assert "rolled back" not in result, "refused at the pre-check, before any write"
        assert snapshot_files(sibling.root) == before

    def test_data_folder_gitignore_linked_out_of_the_worktree_is_never_written_through(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, tmp_path: Path
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        absent_outside = tmp_path / "outside" / "absent.txt"
        absent_outside.parent.mkdir()
        (sibling.root / ".serena").mkdir()
        link = sibling.root / ".serena" / ".gitignore"
        link.symlink_to(absent_outside)

        result = call(agent, GetSymbolsOverviewTool, relative_path=str(sibling.root / MODELS_FILE))
        assert sibling.marker not in result
        assert str(link) in result
        assert not os.path.lexists(absent_outside)

    def test_rollback_restores_a_file_reached_through_a_link_in_a_renamed_directory(
        self, agent: SerenaAgent, worktree_repo: WorktreeRepo, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sibling = worktree_repo.worktrees["sibling"]
        target = sibling.root / "target.py"
        target.write_text("def target_function() -> None:\n    pass\n", encoding="utf-8")
        (sibling.root / "old").mkdir()
        (sibling.root / "old" / "link.py").symlink_to(Path("..") / "target.py")
        self._fake_rename_edit(
            monkeypatch,
            [
                {"kind": "rename", "oldUri": (sibling.root / "old").as_uri(), "newUri": (sibling.root / "new").as_uri()},
                self._insertion_into(sibling.root / "new" / "link.py"),
                self._insertion_into(sibling.root / "test_repo" / "services.py"),
            ],
        )
        target_before = target.read_bytes()
        before = snapshot_files(sibling.root)
        written_paths = self._make_read_only_after_first_write(sibling.root, monkeypatch)

        result = call(agent, RenameSymbolTool, name_path="User", relative_path=str(sibling.root / MODELS_FILE), new_name="Account")
        assert len(written_paths) == 1, result
        assert "rolled back" in result
        assert target.read_bytes() == target_before
        assert (sibling.root / "old" / "link.py").is_symlink()
        assert snapshot_files(sibling.root) == before
