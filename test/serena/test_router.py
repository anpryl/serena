"""
Tests of the ``--follow-cwd`` router, driving a real router process (which starts real child Serena processes)
through an MCP client session over stdio.
"""

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import psutil
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp_types import CallToolResult

from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path

pytestmark = pytest.mark.python

T = TypeVar("T")
PYTHON_REPO = get_repo_path(LanguageServerId.PYTHON)
SUBFOLDER = "test_repo"
ALWAYS_LOADED = {"get_symbols_overview", "find_symbol", "find_referencing_symbols", "replace_symbol_body", "replace_content"}
MAX_RESULT_SIZE_KEY = "anthropic/maxResultSizeChars"
ALWAYS_LOAD_KEY = "anthropic/alwaysLoad"
# the source repo accumulates an ignored .serena folder (memories, caches) whenever other tests run in it in place
_COPY_IGNORE = shutil.ignore_patterns(".serena", "__pycache__")


def _git(folder: Path, *arguments: str) -> None:
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *arguments],
        cwd=folder,
        env=environment,
        check=True,
        capture_output=True,
    )


def _define(folder: Path, function_name: str) -> None:
    (folder / SUBFOLDER / f"{function_name}_marker.py").write_text(f"def {function_name}() -> str:\n    return '{function_name}'\n")


class RouterHarness:
    """
    A router started in a parent folder that holds two projects ``a`` and ``b``, a git worktree of ``a`` nested in
    ``a/.claude/worktrees/x`` and another one beside it in ``sib``. Each tree defines a function that names it.
    """

    def __init__(self, tmp_path: Path, config: str | None = None) -> None:
        base = tmp_path.resolve()
        self.home = base / "serena_home"
        self.home.mkdir()
        if config is not None:
            self.config_file().write_text(config)
        self.parent = base / "parent"
        self.main = self.parent / "a"
        self.other = self.parent / "b"
        self.nested = self.main / ".claude" / "worktrees" / "x"
        self.sibling = self.parent / "sib"
        self.plain = base / "plain"
        self.plain.mkdir()

        shutil.copytree(PYTHON_REPO, self.main, ignore=_COPY_IGNORE)
        _define(self.main, "main_only")
        _git(self.main, "init", "-q")
        _git(self.main, "add", "-A")
        _git(self.main, "commit", "-q", "-m", "init")
        _git(self.main, "worktree", "add", "-q", str(self.nested), "-b", "x")
        _git(self.main, "worktree", "add", "-q", str(self.sibling), "-b", "sib")
        _define(self.nested, "x_only")
        _define(self.sibling, "sib_only")

        shutil.copytree(PYTHON_REPO, self.other, ignore=_COPY_IGNORE)
        _define(self.other, "b_only")
        _git(self.other, "init", "-q")

        self.session: ClientSession | None = None
        self.router_pid = 0

    def config_file(self) -> Path:
        return self.home / "serena_config.yml"

    @asynccontextmanager
    async def running(self) -> AsyncIterator["RouterHarness"]:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-P",
                "-c",
                "from serena.cli import top_level; top_level()",
                "start-mcp-server",
                "--context",
                "claude-code",
                "--follow-cwd",
                "--enable-web-dashboard",
                "false",
                "--open-web-dashboard",
                "false",
                "--enable-gui-log-window",
                "false",
            ],
            env={**os.environ, "SERENA_HOME": str(self.home)},
            cwd=self.parent,
        )
        async with stdio_client(parameters, errlog=open(os.devnull, "w")) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            self.session = session
            yield self

    async def call_raw(self, tool: str, **arguments: Any) -> CallToolResult:
        assert self.session is not None
        return await self.session.call_tool(tool, arguments)

    async def call(self, tool: str, cwd: Path | str, **arguments: Any) -> str:
        """
        :return: the text of the successful result of the call
        """
        result = await self.call_raw(tool, cwd=str(cwd), **arguments)
        assert not result.is_error, self.text(result)
        return self.text(result)

    @staticmethod
    def text(result: CallToolResult) -> str:
        return "".join(block.text for block in result.content if hasattr(block, "text"))

    async def found(self, name: str, cwd: Path, **arguments: Any) -> list[dict[str, Any]]:
        text = await self.call("find_symbol", cwd, name_path_pattern=name, **arguments)
        return json.loads(text)


def paths_outside_worktrees(symbols: list[dict[str, Any]]) -> list[str]:
    """
    :return: the paths of the symbols, without those found in the worktrees nested in the project
    """
    return [s["relative_path"] for s in symbols if not s["relative_path"].startswith(".claude/")]


def run_router(tmp_path: Path, scenario: Callable[[RouterHarness], Awaitable[T]], config: str | None = None) -> T:
    harness = RouterHarness(tmp_path, config)

    async def main() -> T:
        async with harness.running():
            return await scenario(harness)

    return asyncio.run(asyncio.wait_for(main(), timeout=300))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestRouting:
    def test_a_subfolder_of_a_project_gives_root_relative_paths(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> list[dict[str, Any]]:
            return await router.found("main_only", router.main / SUBFOLDER)

        symbols = run_router(tmp_path, scenario)

        assert paths_outside_worktrees(symbols) == [f"{SUBFOLDER}/main_only_marker.py"]

    def test_folders_in_different_projects_are_served_from_their_own_project(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> tuple[list, list, list, list]:
            return (
                await router.found("main_only", router.main),
                await router.found("b_only", router.main),
                await router.found("b_only", router.other),
                await router.found("main_only", router.other),
            )

        main_in_a, b_in_a, b_in_b, main_in_b = run_router(tmp_path, scenario)

        assert (len(paths_outside_worktrees(main_in_a)), len(b_in_a), len(b_in_b), len(main_in_b)) == (1, 0, 1, 0)

    def test_a_nested_worktree_is_its_own_project_and_an_edit_there_leaves_the_main_checkout_unchanged(self, tmp_path: Path) -> None:
        main_file = f"{SUBFOLDER}/main_only_marker.py"

        async def scenario(router: RouterHarness) -> tuple[list, list, str, str]:
            in_x = await router.found("x_only", router.nested)
            in_main = await router.found("x_only", router.main)
            before = _hash(router.main / main_file)
            await router.call(
                "replace_symbol_body",
                router.nested,
                name_path="main_only",
                relative_path=main_file,
                body="def main_only() -> str:\n    return 'edited in x'",
            )
            return in_x, in_main, before, _hash(router.main / main_file)

        in_x, in_main, before, after = run_router(tmp_path, scenario)
        nested_file = tmp_path.resolve() / "parent" / "a" / ".claude" / "worktrees" / "x" / main_file

        assert [s["relative_path"] for s in in_x] == [f"{SUBFOLDER}/x_only_marker.py"]
        assert paths_outside_worktrees(in_main) == []
        assert "edited in x" in nested_file.read_text()
        assert before == after

    def test_an_absolute_path_into_a_sibling_worktree_is_served_from_it_with_absolute_result_paths(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> tuple[list[dict[str, Any]], Path]:
            symbols = await router.found("sib_only", router.main, relative_path=str(router.sibling / SUBFOLDER / "sib_only_marker.py"))
            return symbols, router.sibling

        symbols, sibling = run_router(tmp_path, scenario)

        assert [s["relative_path"] for s in symbols] == [str(sibling / SUBFOLDER / "sib_only_marker.py")]

    def test_a_cold_first_call_to_another_project_does_not_hold_up_a_call_to_a_warm_one(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> tuple[float, float]:
            await router.call("list_memories", router.main)
            started = time.monotonic()
            cold = asyncio.create_task(router.call("list_memories", router.other))
            await asyncio.sleep(0.05)
            await router.call("list_memories", router.main)
            warm_done = time.monotonic() - started
            await cold
            return warm_done, time.monotonic() - started

        warm_done, cold_done = run_router(tmp_path, scenario)

        assert warm_done < cold_done / 2


class TestRefusals:
    def test_calls_without_a_valid_target_are_refused_naming_the_cause(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> dict[str, CallToolResult]:
            sibling_file = router.sibling / SUBFOLDER / "sib_only_marker.py"
            main_file = router.main / SUBFOLDER / "main_only_marker.py"
            return {
                "missing": await router.call_raw("list_memories"),
                "relative": await router.call_raw("list_memories", cwd="a/test_repo"),
                "file": await router.call_raw("list_memories", cwd=str(main_file)),
                "no_project": await router.call_raw("list_memories", cwd=str(router.plain)),
                "two_roots": await router.call_raw(
                    "replace_in_files",
                    cwd=str(router.main),
                    needle="x",
                    repl="y",
                    mode="literal",
                    occurrence_ids=[f"{main_file}:0@0123456789abcdef", f"{sibling_file}:0@0123456789abcdef"],
                ),
            }

        results = run_router(tmp_path, scenario)
        texts = {name: RouterHarness.text(result) for name, result in results.items()}
        base = tmp_path.resolve()

        assert all(result.is_error for result in results.values())
        assert "`cwd`" in texts["missing"]
        assert "a/test_repo" in texts["relative"]
        assert "main_only_marker.py" in texts["file"]
        assert f"No project root found from cwd={base / 'plain'}" in texts["no_project"]
        assert str(base / "parent" / "a") in texts["two_roots"] and str(base / "parent" / "sib") in texts["two_roots"]


class TestLaunchOptions:
    @pytest.mark.parametrize("project_option", [["--project", "."], ["--project-from-cwd"], ["."]])
    def test_follow_cwd_cannot_be_combined_with_a_project(self, tmp_path: Path, project_option: list[str]) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-P",
                "-c",
                "from serena.cli import top_level; top_level()",
                "start-mcp-server",
                "--follow-cwd",
                *project_option,
            ],
            check=False,
            env={**os.environ, "SERENA_HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert completed.returncode == 2
        assert "--follow-cwd cannot be used with" in completed.stderr


class TestMemories:
    def test_memories_belong_to_the_project_of_the_folder_and_global_memories_are_shared(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> dict[str, str]:
            await router.call("write_memory", router.main, memory_name="in_a", content="written from a")
            await router.call("write_memory", router.nested, memory_name="in_x", content="written from x")
            await router.call("write_memory", router.main, memory_name="global/shared", content="shared text")
            return {
                "list_a": await router.call("list_memories", router.main),
                "list_x": await router.call("list_memories", router.nested),
                "list_b": await router.call("list_memories", router.other),
                "read_a": await router.call("read_memory", router.main, memory_name="in_a"),
                "read_x_of_a": RouterHarness.text(await router.call_raw("read_memory", cwd=str(router.main), memory_name="in_x")),
                "global_from_b": await router.call("read_memory", router.other, memory_name="global/shared"),
            }

        texts = run_router(tmp_path, scenario)
        parent = tmp_path.resolve() / "parent"

        assert (parent / "a" / ".serena" / "memories" / "in_a.md").read_text() == "written from a"
        assert not (parent / "a" / ".serena" / "memories" / "in_x.md").exists()
        assert (parent / "a" / ".claude" / "worktrees" / "x" / ".serena" / "memories" / "in_x.md").read_text() == "written from x"
        assert not (parent / "b" / ".serena" / "memories" / "in_a.md").exists()
        assert "in_a" in texts["list_a"] and "in_x" not in texts["list_a"]
        assert "in_x" in texts["list_x"] and "in_a" not in texts["list_x"]
        assert "in_a" not in texts["list_b"] and "in_x" not in texts["list_b"]
        assert "written from a" in texts["read_a"]
        assert "not found" in texts["read_x_of_a"]
        assert "shared text" in texts["global_from_b"]


class TestToolList:
    def test_tools_take_a_required_cwd_and_carry_the_metadata_for_claude_code(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> Any:
            assert router.session is not None
            return (await router.session.list_tools()).tools

        tools = run_router(tmp_path, scenario)
        by_name = {tool.name: tool for tool in tools}

        assert by_name.keys() >= ALWAYS_LOADED
        assert all(tool.input_schema["required"][0] == "cwd" for tool in tools)
        assert all((tool.meta or {}).get(MAX_RESULT_SIZE_KEY) == 200000 for tool in tools)
        assert {tool.name for tool in tools if (tool.meta or {}).get(ALWAYS_LOAD_KEY) is True} == ALWAYS_LOADED
        assert not any(set(tool.meta or {}) - {MAX_RESULT_SIZE_KEY, ALWAYS_LOAD_KEY} for tool in tools)

    def test_a_fresh_serena_home_lists_the_core_tools_and_not_the_repl(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> set[str]:
            assert router.session is not None
            return {tool.name for tool in (await router.session.list_tools()).tools}

        names = run_router(tmp_path, scenario)

        assert names >= ALWAYS_LOADED
        assert "serena_repl" not in names
        assert "activate_project" not in names


class TestResultSize:
    def test_a_result_over_the_limit_is_cut_with_a_notice_that_says_how_to_narrow_the_call(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> tuple[str, str]:
            memories = router.main / ".serena" / "memories"
            memories.mkdir(parents=True)
            (memories / "big.md").write_text("x" * 250_000)
            (memories / "small.md").write_text("small")
            return await router.call("read_memory", router.main, memory_name="big"), await router.call(
                "read_memory", router.main, memory_name="small"
            )

        big, small = run_router(tmp_path, scenario)

        assert len(big) <= 200_000
        assert all(word in big for word in ("`relative_path`", "`depth`", "`max_matches`"))
        assert small == "small"


class TestChildren:
    def test_a_session_with_three_children_leaves_the_config_file_unchanged_and_starts_no_dashboard(self, tmp_path: Path) -> None:
        config = "projects: []\nlanguage_backend: LSP\n"

        async def scenario(router: RouterHarness) -> tuple[str, str, set[int]]:
            before = _hash(router.config_file())
            for folder in (router.main, router.nested, router.other):
                await router.call("list_memories", folder)
            await asyncio.sleep(2)
            descendants = psutil.Process().children(recursive=True)
            listening = {
                connection.laddr.port
                for process in descendants
                for connection in _tcp_connections(process)
                if connection.status == psutil.CONN_LISTEN
            }
            commands = " ".join(" ".join(_cmdline(process)) for process in descendants)
            return before, _hash(router.config_file()) + "|" + commands, listening

        before, after_and_commands, listening = run_router(tmp_path, scenario, config)
        after, _, commands = after_and_commands.partition("|")

        assert before == after
        assert "dashboard_tray_manager" not in commands and "dashboard-viewer" not in commands
        assert listening == set()

    def test_a_dead_child_fails_the_call_that_finds_it_dead_and_is_restarted_by_the_next_call(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> tuple[CallToolResult, CallToolResult, set[int]]:
            await router.call("write_memory", router.main, memory_name="kept", content="kept text")
            old_pids = _child_pids(str(router.main))
            for pid in old_pids:
                psutil.Process(pid).kill()
            await asyncio.sleep(1)
            failed = await router.call_raw("read_memory", cwd=str(router.main), memory_name="kept")
            recovered = await router.call_raw("read_memory", cwd=str(router.main), memory_name="kept")
            return failed, recovered, old_pids

        failed, recovered, old_pids = run_router(tmp_path, scenario)

        assert old_pids
        assert failed.is_error and "Serena child" in RouterHarness.text(failed)
        assert not recovered.is_error and "kept text" in RouterHarness.text(recovered)


def _child_pids(root: str) -> set[int]:
    return {p.pid for p in psutil.Process().children(recursive=True) if "--project" in _cmdline(p) and root in _cmdline(p)}


def _tcp_connections(process: psutil.Process) -> list[Any]:
    try:
        return process.net_connections(kind="tcp")
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return []


def _cmdline(process: psutil.Process) -> list[str]:
    try:
        return process.cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return []
