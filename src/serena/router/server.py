# SPDX-License-Identifier: GPL-3.0-or-later

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer as FastMCP
from mcp_types import NotificationParams
from sensai.util import logging

from serena.mcp import SerenaMCPFactory
from serena.router.catalog import RouterToolCatalog
from serena.router.child_pool import ChildOptions, ChildPool
from serena.router.project_facts import ProjectInspector
from serena.router.router import Router
from serena.router.status_file import StatusFile
from serena.router.working_directories import ClientRoots, WorkingDirectories
from serena.util.logging import MemoryLogHandler
from serena.util.project_root import find_project_root

log = logging.getLogger(__name__)


class RouterMCPFactory(SerenaMCPFactory):
    """
    Creates the MCP server of the ``--follow-cwd`` mode. It holds no project: its agent only determines the tool
    set, and every call is forwarded to the child Serena of the project that the call is about.
    """

    ROUTER_PROMPT = (
        "This server follows your working directory. Every tool takes a required `cwd`: your current working directory, "
        "absolute; after `cd`, pass the new folder. Serena then works as if launched in that folder, on the project "
        "that contains it (a git worktree, even one nested in another project, is a project of its own). An absolute "
        "path argument selects the project it lies in instead. Relative paths in other arguments stay relative to that "
        "project's root, not to `cwd`.\n\n"
        "Calls for different projects may run in parallel.\n\n"
        "The first result from each project carries that project's instructions. Results from a project other than the "
        "one Serena was started in begin with a `[serena project <name> at <root>]` line. `initial_instructions` with "
        "`cwd` returns the instructions of that folder's project."
    )
    MAX_CONNECTION_PROMPT_CHARS = 4096

    def __init__(self, context: str, child_options: ChildOptions, memory_log_handler: MemoryLogHandler | None = None):
        """
        :param context: the context name or path of context file
        :param child_options: the launch options of the children
        :param memory_log_handler: the in-memory log handler to use for the agent's logging
        """
        super().__init__(transport="stdio", context=context, project=None, memory_log_handler=memory_log_handler)
        self._child_options = child_options

    def _get_initial_instructions(self) -> str:
        return f"{super()._get_initial_instructions()}\n\n{self.ROUTER_PROMPT}"

    @asynccontextmanager
    async def server_lifespan(self, mcp_server: FastMCP) -> AsyncIterator[None]:
        assert self.agent is not None
        try:
            launch_dir = Path.cwd()
            working_directories = WorkingDirectories(launch_dir)
            status_file = StatusFile(Path(os.path.realpath(find_project_root(start=launch_dir) or launch_dir)))

            async def on_roots_changed(*_: object) -> None:
                working_directories.invalidate()

            mcp_server._lowlevel_server.add_notification_handler("notifications/roots/list_changed", NotificationParams, on_roots_changed)
            async with (
                ChildPool(self._child_options, inspector=ProjectInspector(self.agent.serena_config), status_file=status_file) as pool,
                anyio.create_task_group() as eager_starts,
            ):
                router = Router(pool, working_directories)

                async def on_initialized(context: Any, _: object) -> None:
                    # in a task of its own: asking the client for its roots needs the receive loop that runs this handler
                    eager_starts.start_soon(router.start_launch_child, ClientRoots(context.session))

                mcp_server._lowlevel_server.add_notification_handler("notifications/initialized", NotificationParams, on_initialized)
                catalog = RouterToolCatalog(
                    self.agent,
                    router,
                    openai_tool_compatible=False,
                    structured_output=self.agent.get_context().structured_tool_output,
                )
                tools = catalog.tools()
                mcp_server._tool_manager._tools = dict(tools)
                log.info("Starting router MCP server with %d tools: %s", len(tools), list(tools))
                yield
                eager_starts.cancel_scope.cancel()
        finally:
            self.shutdown_agent()
