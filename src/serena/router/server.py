# SPDX-License-Identifier: GPL-3.0-or-later

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer as FastMCP
from sensai.util import logging

from serena.mcp import SerenaMCPFactory
from serena.router.catalog import RouterToolCatalog
from serena.router.child_pool import ChildOptions, ChildPool
from serena.router.router import Router
from serena.util.logging import MemoryLogHandler

log = logging.getLogger(__name__)


class RouterMCPFactory(SerenaMCPFactory):
    """
    Creates the MCP server of the ``--follow-cwd`` mode. It holds no project: its agent only determines the tool
    set, and every call is forwarded to the child Serena of the project that the call is about.
    """

    def __init__(self, context: str, child_options: ChildOptions, memory_log_handler: MemoryLogHandler | None = None):
        """
        :param context: the context name or path of context file
        :param child_options: the launch options of the children
        :param memory_log_handler: the in-memory log handler to use for the agent's logging
        """
        super().__init__(transport="stdio", context=context, project=None, memory_log_handler=memory_log_handler)
        self._child_options = child_options

    @asynccontextmanager
    async def server_lifespan(self, mcp_server: FastMCP) -> AsyncIterator[None]:
        assert self.agent is not None
        try:
            async with ChildPool(self._child_options) as pool:
                catalog = RouterToolCatalog(
                    self.agent,
                    Router(pool),
                    openai_tool_compatible=False,
                    structured_output=self.agent.get_context().structured_tool_output,
                )
                tools = catalog.tools()
                mcp_server._tool_manager._tools = dict(tools)
                log.info("Starting router MCP server with %d tools: %s", len(tools), list(tools))
                yield
        finally:
            self.shutdown_agent()
