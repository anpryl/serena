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
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import psutil
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp_types import CallToolResult, ErrorData, ListRootsResult, Root

from serena.router.call_target import CallTarget
from serena.router.child_pool import ChildOptions, ChildPool
from serena.router.router import Router
from serena.router.status_file import StatusFile
from serena.router.working_directories import WorkingDirectories
from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path

pytestmark = pytest.mark.python

T = TypeVar("T")
PYTHON_REPO = get_repo_path(LanguageServerId.PYTHON)
SUBFOLDER = "test_repo"
ALWAYS_LOADED = {"get_symbols_overview", "find_symbol", "find_referencing_symbols", "replace_symbol_body", "replace_content"}
HEADER_PREFIX = "[serena project "
ACTIVATION_PREFIX = "<serena>"
MAX_RESULT_SIZE_KEY = "anthropic/maxResultSizeChars"
ALWAYS_LOAD_KEY = "anthropic/alwaysLoad"
CLAUDE_CODE_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "claude-code", "title": "Claude Code", "version": "2.1.328"},
    "io.modelcontextprotocol/clientCapabilities": {"roots": {"listChanged": True}, "elicitation": {"form": {}, "url": {}}},
}
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

    def __init__(self, tmp_path: Path, config: str | None = None, launch_in: str = "parent", extra_args: Sequence[str] = ()) -> None:
        base = tmp_path.resolve()
        self.launch_in = launch_in
        self.extra_args = list(extra_args)
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
        self.roots: list[Path] | None = None
        self.instructions: str | None = None

    def config_file(self) -> Path:
        return self.home / "serena_config.yml"

    @asynccontextmanager
    async def running(self) -> AsyncIterator["RouterHarness"]:
        async with (
            stdio_client(self._server_parameters(), errlog=open(os.devnull, "w")) as (read, write),
            ClientSession(read, write, list_roots_callback=self._list_roots) as session,
        ):
            self.instructions = (await session.initialize()).instructions
            self.session = session
            yield self

    @asynccontextmanager
    async def opened_by_claude_code(self) -> AsyncIterator["RouterHarness"]:
        """
        Starts the router and opens the connection over raw stdio with the requests Claude Code 2.1.328 sends: protocol
        2026-07-28 named in each request's ``_meta``, with no ``initialize`` and no ``notifications/initialized``.
        Enters once the tool list is answered, without calling a tool.
        """
        parameters = self._server_parameters()
        process = await asyncio.create_subprocess_exec(
            parameters.command,
            *parameters.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=parameters.env,
            cwd=parameters.cwd,
            limit=2**24,
        )
        assert process.stdin is not None and process.stdout is not None
        stdin, stdout = process.stdin, process.stdout

        async def request(request_id: str | int, method: str, **params: Any) -> None:
            message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {"_meta": CLAUDE_CODE_META, **params}}
            stdin.write(json.dumps(message).encode() + b"\n")
            await stdin.drain()

        async def response_to(request_id: str | int) -> dict[str, Any]:
            while True:
                line = await stdout.readline()
                assert line, "the router closed its output"
                message = json.loads(line)
                if message.get("id") == request_id:
                    assert "result" in message, message
                    return message

        try:
            await request("server-discover-probe-1", "server/discover")
            await response_to("server-discover-probe-1")
            await request(
                "listen:0",
                "subscriptions/listen",
                notifications={"toolsListChanged": True, "promptsListChanged": True, "resourcesListChanged": True},
            )
            await request(0, "server/discover")
            await request(1, "prompts/list")
            await request(2, "resources/list")
            await request(3, "tools/list")
            await response_to(3)
            yield self
        finally:
            stdin.close()
            try:
                await asyncio.wait_for(process.wait(), timeout=60)
            except TimeoutError:
                process.kill()
                await process.wait()

    def _server_parameters(self) -> StdioServerParameters:
        return StdioServerParameters(
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
                *self.extra_args,
            ],
            env={**os.environ, "SERENA_HOME": str(self.home)},
            cwd=getattr(self, self.launch_in),
        )

    async def _list_roots(self, _context: Any) -> ListRootsResult | ErrorData:
        if self.roots is None:
            return ErrorData(code=-32601, message="no roots")
        return ListRootsResult(roots=[Root(uri=path.as_uri()) for path in self.roots])

    async def change_roots(self, roots: list[Path]) -> None:
        assert self.session is not None
        self.roots = roots
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            await self.session.send_roots_list_changed()  # ty: ignore[deprecated]

    def make_activation_marker(self, project: Path) -> Path:
        """
        Gives the project an ``activation_command`` that creates the returned file, which exists only if the project was
        activated as a trusted one.
        """
        marker = project.parent / f"{project.name}.activated"
        (project / ".serena").mkdir(exist_ok=True)
        (project / ".serena" / "project.yml").write_text(
            f"project_name: {project.name}\nlanguage_servers:\n- python\nactivation_command: touch {marker}\n"
        )
        return marker

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
    def blocks(result: CallToolResult) -> list[str]:
        return [block.text for block in result.content if hasattr(block, "text")]

    @classmethod
    def text(cls, result: CallToolResult) -> str:
        """
        :return: the text of the child's own result, without the router's header and activation text
        """
        return "".join(block for block in cls.blocks(result) if not block.startswith((HEADER_PREFIX, ACTIVATION_PREFIX)))

    async def found(self, name: str, cwd: Path, **arguments: Any) -> list[dict[str, Any]]:
        text = await self.call("find_symbol", cwd, name_path_pattern=name, **arguments)
        return json.loads(text)


def paths_outside_worktrees(symbols: list[dict[str, Any]]) -> list[str]:
    """
    :return: the paths of the symbols, without those found in the worktrees nested in the project
    """
    return [s["relative_path"] for s in symbols if not s["relative_path"].startswith(".claude/")]


def run_router(
    tmp_path: Path,
    scenario: Callable[[RouterHarness], Awaitable[T]],
    config: str | None = None,
    launch_in: str = "parent",
    extra_args: Sequence[str] = (),
) -> T:
    harness = RouterHarness(tmp_path, config, launch_in, extra_args)

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


class TestTrust:
    def test_a_project_outside_the_working_directories_runs_untrusted_until_the_roots_include_it(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> tuple[list[bool], list[bool]]:
            main_marker = router.make_activation_marker(router.main)
            sibling_marker = router.make_activation_marker(router.sibling)
            router.roots = [router.main]
            sibling_file = str(router.sibling / SUBFOLDER / "sib_only_marker.py")
            await router.found("main_only", router.main)
            await router.call("find_symbol", router.main, name_path_pattern="sib_only", relative_path=sibling_file)
            before = [main_marker.exists(), sibling_marker.exists()]
            await router.change_roots([router.main, router.sibling])
            await router.call("find_symbol", router.main, name_path_pattern="sib_only", relative_path=sibling_file)
            return before, [main_marker.exists(), sibling_marker.exists()]

        before, after = run_router(
            tmp_path, scenario, config="projects: []\nlanguage_backend: LSP\ntrusted_project_path_patterns:\n- '**'\n"
        )

        assert before == [True, False]
        assert after == [True, True]

    def test_the_routers_untrusted_option_makes_every_child_untrusted(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> bool:
            main_marker = router.make_activation_marker(router.main)
            router.roots = [router.main]
            await router.found("main_only", router.main)
            return main_marker.exists()

        marker_exists = run_router(
            tmp_path,
            scenario,
            config="projects: []\nlanguage_backend: LSP\ntrusted_project_path_patterns:\n- '**'\n",
            extra_args=["--untrusted"],
        )

        assert not marker_exists


class TestActivationText:
    def test_the_first_result_of_each_project_carries_its_activation_text_and_other_projects_are_named(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> dict[str, list[str]]:
            blocks = {}
            blocks["a_first"] = router.blocks(await router.call_raw("list_memories", cwd=str(router.main)))
            blocks["a_second"] = router.blocks(await router.call_raw("list_memories", cwd=str(router.main)))
            blocks["b_first"] = router.blocks(await router.call_raw("list_memories", cwd=str(router.other)))
            blocks["b_second"] = router.blocks(await router.call_raw("list_memories", cwd=str(router.other)))
            return blocks

        blocks = run_router(tmp_path, scenario, launch_in="main")
        base = tmp_path.resolve() / "parent"

        assert blocks["a_first"][0].startswith(ACTIVATION_PREFIX) and "project with name 'a'" in blocks["a_first"][0]
        assert len(blocks["a_first"]) == 2 and not blocks["a_first"][1].startswith(HEADER_PREFIX)
        assert len(blocks["a_second"]) == 1 and not blocks["a_second"][0].startswith((HEADER_PREFIX, ACTIVATION_PREFIX))
        assert blocks["b_first"][0] == f"[serena project b at {base / 'b'}]\n"
        assert blocks["b_first"][1].startswith(ACTIVATION_PREFIX) and "project with name 'b'" in blocks["b_first"][1]
        assert len(blocks["b_second"]) == 2 and blocks["b_second"][0] == blocks["b_first"][0]

    def test_initial_instructions_answers_for_the_project_of_cwd_and_counts_as_its_first_result(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> tuple[list[str], list[str]]:
            asked = router.blocks(await router.call_raw("initial_instructions", cwd=str(router.nested)))
            then = router.blocks(await router.call_raw("list_memories", cwd=str(router.nested)))
            return asked, then

        asked, then = run_router(tmp_path, scenario, launch_in="main")

        assert asked[0].startswith(HEADER_PREFIX) and "worktrees/x]" in asked[0]
        assert len(asked) == 2 and asked[1].startswith(ACTIVATION_PREFIX) and "project with name 'x'" in asked[1]
        assert len(then) == 2 and then[0] == asked[0] and not then[1].startswith(ACTIVATION_PREFIX)

    def test_the_connection_prompt_names_cwd_and_parallel_calls_and_fits_the_limit(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> str | None:
            return router.instructions

        instructions = run_router(tmp_path, scenario)

        assert instructions is not None and len(instructions) < 4096
        assert "required `cwd`" in instructions and "different projects may run in parallel" in instructions


class TestCodeFileList:
    def test_a_project_that_has_started_a_child_lists_the_extensions_of_its_language_servers(self, tmp_path: Path) -> None:
        async def scenario(router: RouterHarness) -> None:
            await router.call("list_memories", router.main)

        run_router(tmp_path, scenario)
        digest = hashlib.sha256(str(tmp_path.resolve() / "parent" / "a").encode()).hexdigest()[:16]
        listing = tmp_path.resolve() / "serena_home" / "follow_cwd" / f"{digest}.code-files"

        assert ".py" in listing.read_text().splitlines()


class FakeClock:
    def __init__(self) -> None:
        self.seconds = 0.0

    def now(self) -> float:
        return self.seconds


def _running_children_of(*roots: Path) -> dict[Path, set[int]]:
    return {root: {pid for pid in _child_pids(str(root)) if psutil.pid_exists(pid)} for root in roots}


async def _gone(root: Path) -> bool:
    for _ in range(100):
        if not _child_pids(str(root)):
            return True
        await asyncio.sleep(0.1)
    return False


class TestLifecycle:
    @staticmethod
    def _target(root: Path) -> CallTarget:
        return CallTarget(root=root, caller_dir=root)

    def test_an_idle_child_stops_and_the_next_call_starts_a_new_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        harness = RouterHarness(tmp_path)
        monkeypatch.setenv("SERENA_HOME", str(harness.home))
        clock = FakeClock()

        async def main() -> tuple[set[int], bool, bool, set[int]]:
            async with ChildPool(ChildOptions(context="claude-code"), clock=clock, idle_timeout=3600) as pool:
                await pool.forward(self._target(harness.main), "list_memories", {})
                first = _child_pids(str(harness.main))
                clock.seconds = 3599
                pool.stop_idle_children()
                still_running = _child_pids(str(harness.main)) == first
                clock.seconds = 3600
                pool.stop_idle_children()
                stopped = await _gone(harness.main)
                await pool.forward(self._target(harness.main), "list_memories", {})
                return first, still_running, stopped, _child_pids(str(harness.main))

        first, still_running, stopped, second = asyncio.run(asyncio.wait_for(main(), timeout=300))

        assert first and still_running and stopped
        assert second and not (second & first)

    def test_beyond_the_cap_the_least_recently_used_child_stops_and_leaving_the_pool_stops_the_rest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness = RouterHarness(tmp_path)
        monkeypatch.setenv("SERENA_HOME", str(harness.home))
        clock = FakeClock()

        async def main() -> tuple[dict[Path, set[int]], dict[Path, set[int]]]:
            async with ChildPool(ChildOptions(context="claude-code"), clock=clock, max_children=2) as pool:
                for root in (harness.main, harness.other, harness.main, harness.nested):
                    clock.seconds += 1
                    await pool.forward(self._target(root), "list_memories", {})
                await _gone(harness.other)
                during = _running_children_of(harness.main, harness.other, harness.nested)
            return during, _running_children_of(harness.main, harness.other, harness.nested)

        during, after = asyncio.run(asyncio.wait_for(main(), timeout=300))

        assert bool(during[harness.main]) and bool(during[harness.nested]) and not during[harness.other]
        assert not any(after.values())


class TestEagerStart:
    def test_the_launch_root_has_a_child_and_a_code_file_list_after_claude_code_connects_and_before_any_call(self, tmp_path: Path) -> None:
        harness = RouterHarness(tmp_path, launch_in="main")
        listing = harness.home / "follow_cwd" / f"{hashlib.sha256(str(harness.main).encode()).hexdigest()[:16]}.code-files"

        def listing_and_children() -> tuple[list[str], list[dict[str, Any]]]:
            deadline = time.monotonic() + 120
            while not listing.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            states = list((harness.home / "follow_cwd" / "state").glob("*.json"))
            return listing.read_text().splitlines() if listing.exists() else [], [
                child for state in states for child in json.loads(state.read_text())["children"]
            ]

        async def main() -> tuple[list[str], list[dict[str, Any]]]:
            async with harness.opened_by_claude_code():
                return await asyncio.to_thread(listing_and_children)

        extensions, children = asyncio.run(asyncio.wait_for(main(), timeout=300))

        assert ".py" in extensions
        assert [child["root"] for child in children] == [str(tmp_path.resolve() / "parent" / "a")]

    def test_a_launch_folder_in_no_project_starts_nothing(self) -> None:
        pool = _RecordingPool()
        router = Router(pool, WorkingDirectories(Path("/")), launch_dir=Path("/"))

        asyncio.run(router.start_launch_child())

        assert pool.targets == []

    def test_a_failed_start_is_not_raised_and_the_next_start_is_tried(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        pool = _RecordingPool(fail_first=True)
        router = Router(pool, WorkingDirectories(tmp_path), launch_dir=tmp_path)

        asyncio.run(router.start_launch_child())
        asyncio.run(router.start_launch_child())

        assert [target.root for target in pool.targets] == [tmp_path.resolve()] * 2
        assert all(target.trusted for target in pool.targets)


class _RecordingPool:
    def __init__(self, fail_first: bool = False) -> None:
        self.targets: list[CallTarget] = []
        self._fail_first = fail_first

    async def activation(self, target: CallTarget) -> None:
        self.targets.append(target)
        if self._fail_first and len(self.targets) == 1:
            raise RuntimeError("no child")


class TestStatusFile:
    def test_the_file_is_named_by_the_pid_and_reflects_a_start_a_ready_and_a_stop(self, tmp_path: Path) -> None:
        harness = RouterHarness(tmp_path)
        status_file = StatusFile(harness.main, harness.home, pid=4242)
        clock = FakeClock()

        def content() -> dict[str, Any]:
            return json.loads(status_file.path.read_text()) if status_file.path.exists() else {}

        async def main() -> tuple[set[str], dict[str, Any], dict[str, Any]]:
            seen: set[str] = set()
            async with ChildPool(ChildOptions(context="claude-code"), clock=clock, idle_timeout=3600, status_file=status_file) as pool:

                async def watch() -> None:
                    while True:
                        seen.update(child["state"] for child in content().get("children", []))
                        await asyncio.sleep(0.005)

                watcher = asyncio.create_task(watch())
                await pool.forward(self._target(harness.main), "list_memories", {})
                watcher.cancel()
                after_call = content()
                clock.seconds = 3600
                pool.stop_idle_children()
                after_stop = content()
            return seen, after_call, after_stop

        seen, after_call, after_stop = asyncio.run(asyncio.wait_for(main(), timeout=300))

        assert status_file.path == harness.home / "follow_cwd" / "state" / "4242.json"
        assert seen == {"starting", "ready"}
        assert after_call == {
            "pid": 4242,
            "launch_root": str(harness.main),
            "children": [{"root": str(harness.main), "name": harness.main.name, "state": "idle"}],
        }
        assert after_stop == {"pid": 4242, "launch_root": str(harness.main), "children": []}
        assert not status_file.path.exists()

    def test_two_pools_keep_separate_files(self, tmp_path: Path) -> None:
        harness = RouterHarness(tmp_path)
        first = StatusFile(harness.main, harness.home, pid=1)
        second = StatusFile(harness.other, harness.home, pid=2)

        async def main() -> tuple[dict[str, Any], dict[str, Any]]:
            options = ChildOptions(context="claude-code")
            async with ChildPool(options, status_file=first) as first_pool, ChildPool(options, status_file=second) as second_pool:
                await first_pool.forward(self._target(harness.main), "list_memories", {})
                await second_pool.forward(self._target(harness.other), "list_memories", {})
                return json.loads(first.path.read_text()), json.loads(second.path.read_text())

        first_content, second_content = asyncio.run(asyncio.wait_for(main(), timeout=300))

        assert first_content["launch_root"] == str(harness.main)
        assert [child["root"] for child in first_content["children"]] == [str(harness.main)]
        assert second_content["launch_root"] == str(harness.other)
        assert [child["root"] for child in second_content["children"]] == [str(harness.other)]
        assert not first.path.exists() and not second.path.exists()

    @staticmethod
    def _target(root: Path) -> CallTarget:
        return CallTarget(root=root, caller_dir=root)
