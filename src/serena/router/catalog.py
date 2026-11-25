# SPDX-License-Identifier: GPL-3.0-or-later

from typing import Any, ClassVar

from mcp.server.mcpserver.server import Context
from sensai.util import logging

from serena.agent import SerenaAgent
from serena.mcp import SerenaFastMCPTool
from serena.router.call_target import CallTargetResolver
from serena.router.result_cap import ResultCap
from serena.router.router import Router
from serena.router.working_directories import ClientRoots
from serena.tools import Tool

log = logging.getLogger(__name__)


class RouterTool(SerenaFastMCPTool):
    """
    A Serena tool as the router offers it: the tool's own schema plus a required ``cwd``, and the metadata that
    Claude Code reads from the tool list. A call is forwarded to the child of the project that it is about.
    """

    CWD_DESCRIPTION: ClassVar[str] = (
        "Your current working directory, absolute. Serena works as if launched there. After `cd`, pass the new folder. "
        "Relative paths in other arguments stay relative to the project root, not to `cwd`."
    )

    def __init__(self, tool: Tool, router: Router, meta: dict[str, Any], openai_tool_compatible: bool, structured_output: bool | None):
        super().__init__(tool, openai_tool_compatible=openai_tool_compatible, structured_output=structured_output)
        cwd = CallTargetResolver.CWD_PARAMETER_NAME
        properties: dict[str, Any] = self.parameters.setdefault("properties", {})
        if cwd in properties:
            raise ValueError(f"Tool '{self.name}' has a parameter named '{cwd}' of its own, which the router needs for the caller's folder")
        self.parameters["properties"] = {cwd: {"type": "string", "title": "Cwd", "description": self.CWD_DESCRIPTION}, **properties}
        self.parameters["required"] = [cwd, *self.parameters.get("required", [])]
        self.meta = meta
        self._router = router

    async def run(self, arguments: dict[str, Any], context: Context, convert_result: bool = False) -> Any:
        return await self._router.route(self.name, arguments, ClientRoots(context.request_context.session))


class RouterToolCatalog:
    """
    The tools of the router: those the context exposes with no project, in the shape of :class:`RouterTool`.
    """

    ALWAYS_LOAD_META_KEY = "anthropic/alwaysLoad"
    MAX_RESULT_SIZE_META_KEY = "anthropic/maxResultSizeChars"
    ALWAYS_LOADED_TOOL_NAMES = ("get_symbols_overview", "find_symbol", "find_referencing_symbols", "replace_symbol_body", "replace_content")

    def __init__(self, agent: SerenaAgent, router: Router, openai_tool_compatible: bool, structured_output: bool | None):
        """
        :param agent: an agent without a project, which determines the tool set
        :param router: the router to which the tools forward their calls
        """
        self._agent = agent
        self._router = router
        self._openai_tool_compatible = openai_tool_compatible
        self._structured_output = structured_output

    def tools(self) -> dict[str, RouterTool]:
        """
        :return: the router's tools by name; children are single-project sessions in a single-project context,
            so the tools such a session does not expose are left out
        """
        exposed = self._agent.get_exposed_tool_instances()
        if self._agent.get_context().single_project:
            exposed = [tool for tool in exposed if tool.get_name() not in SerenaAgent.SINGLE_PROJECT_EXCLUDED_TOOL_NAMES]
        return {
            tool.get_name(): RouterTool(
                tool,
                self._router,
                meta=self._meta_of(tool.get_name()),
                openai_tool_compatible=self._openai_tool_compatible,
                structured_output=self._structured_output,
            )
            for tool in exposed
        }

    def _meta_of(self, tool_name: str) -> dict[str, Any]:
        meta: dict[str, Any] = {self.MAX_RESULT_SIZE_META_KEY: ResultCap.MAX_CHARS}
        if tool_name in self.ALWAYS_LOADED_TOOL_NAMES:
            meta[self.ALWAYS_LOAD_META_KEY] = True
        return meta
