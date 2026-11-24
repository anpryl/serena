"""
Tests of how a Serena MCP server renders and accepts file paths depending on the caller's folder
(passed in the ``_meta`` of a request), driving a real server process through an MCP client session.
Snapshots use syrupy; recreate them with `pytest --snapshot-update`.
"""

import asyncio
import json
import os
import re
import shutil
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, TypeVar

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from syrupy import SnapshotAssertion

from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path

pytestmark = [pytest.mark.python, pytest.mark.snapshot]

T = TypeVar("T")
PYTHON_REPO = get_repo_path(LanguageServerId.PYTHON)

CallerStyle = Literal["inside", "inside_subfolder", "outside", "absent"]
CALLER_STYLES: list[CallerStyle] = ["inside", "inside_subfolder", "outside", "absent"]


class ServerHarness:
    """
    A Serena server (claude-code context) on a copy of the Python test repository, reached through an MCP client session.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "serena_home"
        self.home.mkdir()
        self.root = tmp_path / "project"
        shutil.copytree(PYTHON_REPO, self.root)
        self.outside = tmp_path / "elsewhere"
        self.outside.mkdir()
        self._session: ClientSession | None = None

    def caller_dir(self, style: CallerStyle) -> str | None:
        return {
            "inside": str(self.root),
            "inside_subfolder": str(self.root / "test_repo"),
            "outside": str(self.outside),
            "absent": None,
        }[style]

    @asynccontextmanager
    async def running(self) -> AsyncIterator["ServerHarness"]:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-c",
                "from serena.cli import top_level; top_level()",
                "start-mcp-server",
                "--project",
                str(self.root),
                "--context",
                "claude-code",
                "--agent-interface",
                "tools",
                "--enable-web-dashboard",
                "false",
                "--enable-gui-log-window",
                "false",
                "--open-web-dashboard",
                "false",
            ],
            env={**os.environ, "SERENA_HOME": str(self.home)},
        )
        async with stdio_client(params, errlog=open(os.devnull, "w")) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            self._session = session
            yield self

    async def call(self, style: CallerStyle, tool: str, **arguments: Any) -> str:
        """
        :return: the text of the tool's result (or error), with the test's temporary folders replaced by placeholders
        """
        assert self._session is not None
        caller_dir = self.caller_dir(style)
        meta = {"serena/callerDir": caller_dir} if caller_dir is not None else None
        result = await self._session.call_tool(tool, arguments, meta=meta)
        text = "\n".join(block.text for block in result.content if hasattr(block, "text"))
        return self._mask(text)

    def _mask(self, text: str) -> str:
        for folder, placeholder in ((self.root, "<ROOT>"), (self.outside, "<OUTSIDE>")):
            for variant in {str(folder), os.path.realpath(folder)}:
                text = text.replace(variant, placeholder)
        return text

    def unmask(self, text: str) -> str:
        return text.replace("<ROOT>", str(self.root)).replace("<OUTSIDE>", str(self.outside))

    def read(self, relative_path: str) -> str:
        return (self.root / relative_path).read_text()


def run_against_server(tmp_path: Path, scenario: Callable[[ServerHarness], Awaitable[T]]) -> T:
    harness = ServerHarness(tmp_path)

    async def main() -> T:
        async with harness.running():
            return await scenario(harness)

    return asyncio.run(asyncio.wait_for(main(), timeout=300))


MODELS = os.path.join("test_repo", "models.py")
SERVICES = os.path.join("test_repo", "services.py")
DIAGNOSTICS_SAMPLE = os.path.join("test_repo", "diagnostics_sample.py")


class TestPathRendering:
    @pytest.mark.parametrize("style", CALLER_STYLES)
    def test_every_tool_renders_paths_in_the_style_of_the_caller(
        self, tmp_path: Path, style: CallerStyle, snapshot: SnapshotAssertion
    ) -> None:
        async def scenario(server: ServerHarness) -> dict[str, str]:
            call = lambda tool, **arguments: server.call(style, tool, **arguments)
            return {
                "get_symbols_overview": await call("get_symbols_overview", relative_path=MODELS),
                "find_symbol": await call("find_symbol", name_path_pattern="User", relative_path="test_repo", depth=1),
                "find_symbol_max_matches": await call("find_symbol", name_path_pattern="to_dict", max_matches=1),
                "find_referencing_symbols": await call(
                    "find_referencing_symbols", name_path="UserService/get_user", relative_path=SERVICES
                ),
                "find_declaration": await call(
                    "find_declaration",
                    relative_path=SERVICES,
                    regex=r"-> (User) \| None:",
                    containing_symbol_name_path="UserService/get_user",
                ),
                "get_diagnostics_for_file": await call("get_diagnostics_for_file", relative_path=DIAGNOSTICS_SAMPLE, min_severity=1),
                "safe_delete_symbol": await call("safe_delete_symbol", name_path_pattern="User", relative_path=MODELS),
                "replace_in_files_dry_run": await call(
                    "replace_in_files", needle="Loggable", repl="Traceable", mode="literal", dry_run=True
                ),
                "replace_in_files_applied": await call("replace_in_files", needle="Loggable", repl="Traceable", mode="literal"),
            }

        assert run_against_server(tmp_path, scenario) == snapshot

    @pytest.mark.parametrize("style", ["inside", "outside"])
    def test_a_symbol_path_from_find_symbol_can_be_passed_to_an_editing_tool(self, tmp_path: Path, style: CallerStyle) -> None:
        async def scenario(server: ServerHarness) -> str:
            found = json.loads(
                await server.call(style, "find_symbol", name_path_pattern="UserService/list_users", relative_path="test_repo")
            )
            location = found[0]
            await server.call(
                style,
                "replace_symbol_body",
                name_path=location["name_path"],
                relative_path=server.unmask(location["relative_path"]),
                body="    def list_users(self) -> list[User]:\n        return []  # path round trip",
            )
            return location["relative_path"]

        rendered_path = run_against_server(tmp_path, scenario)

        assert rendered_path.startswith("<ROOT>") == (style == "outside")
        edited = (tmp_path / "project" / "test_repo" / "services.py").read_text()
        assert "# path round trip" in edited

    @pytest.mark.parametrize("style", ["inside", "outside"])
    def test_occurrence_ids_of_a_preview_apply_exactly_the_previewed_occurrences(self, tmp_path: Path, style: CallerStyle) -> None:
        async def scenario(server: ServerHarness) -> None:
            preview = await server.call(style, "replace_in_files", needle="Loggable", repl="Traceable", mode="literal", dry_run=True)
            ids = [server.unmask(i) for i in re.findall(r"\[([^\]]+:\d+@[0-9a-f]{16})\]", preview)]
            assert len(ids) >= 2
            await server.call(style, "replace_in_files", needle="Loggable", repl="Traceable", mode="literal", occurrence_ids=ids[:1])

        run_against_server(tmp_path, scenario)

        remaining = (tmp_path / "project" / "test_repo" / "models.py").read_text().count("Loggable")
        total_before = (PYTHON_REPO / "test_repo" / "models.py").read_text().count("Loggable")
        assert remaining == total_before - 1

    @pytest.mark.parametrize("style", CALLER_STYLES)
    def test_an_absolute_path_inside_the_project_is_accepted_like_its_relative_form(self, tmp_path: Path, style: CallerStyle) -> None:
        async def scenario(server: ServerHarness) -> tuple[str, str]:
            relative = await server.call(style, "get_diagnostics_for_file", relative_path=DIAGNOSTICS_SAMPLE, min_severity=1)
            absolute = await server.call(
                style, "get_diagnostics_for_file", relative_path=server.unmask(f"<ROOT>/{DIAGNOSTICS_SAMPLE}"), min_severity=1
            )
            return relative, absolute

        relative, absolute = run_against_server(tmp_path, scenario)

        assert "broken_factory" in relative
        assert absolute == relative
