# SPDX-License-Identifier: GPL-3.0-or-later

import dataclasses
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, ContentBlock, TextContent

from serena.router.call_target import CallRefusedError, CallTarget, CallTargetResolver
from serena.router.child_pool import ChildPool, ChildReply
from serena.router.result_cap import ResultCap
from serena.router.working_directories import RootsSource, WorkingDirectories
from serena.util.project_root import find_project_root


class Router:
    """
    Sends each tool call to the child Serena of the project that the call is about, and caps the size of the result.

    The first result from each project in the session carries the project's activation text, and results from
    projects other than the launch folder's start with a line naming the project.
    """

    INITIAL_INSTRUCTIONS_TOOL_NAME = "initial_instructions"

    def __init__(
        self,
        pool: ChildPool,
        working_directories: WorkingDirectories,
        launch_dir: Path | None = None,
        resolver: CallTargetResolver | None = None,
        result_cap: ResultCap | None = None,
    ):
        """
        :param pool: the owner of the children
        :param working_directories: decides which projects are trusted
        :param launch_dir: the folder the router was launched in; defaults to the current working directory
        """
        self._pool = pool
        self._working_directories = working_directories
        launch_root = find_project_root(start=launch_dir or Path.cwd())
        self._launch_root = Path(os.path.realpath(launch_root)) if launch_root is not None else None
        self._resolver = resolver or CallTargetResolver()
        self._result_cap = result_cap or ResultCap()
        self._projects_with_activation_text: set[Path] = set()

    async def route(self, tool_name: str, arguments: dict[str, Any], client_roots: RootsSource) -> CallToolResult:
        """
        :param tool_name: the tool that was called
        :param arguments: the arguments of the call, including ``cwd``
        :param client_roots: where to ask for the client's roots, which decide the trust of the call's project
        :return: the child's result, with the project's activation text and name where due
        :raises ToolError: if the call has no valid target or the child fails
        """
        try:
            target = self._resolver.resolve(arguments)
        except CallRefusedError as e:
            raise ToolError(str(e)) from e
        trusted = await self._working_directories.covers(target.root, client_roots)
        target = dataclasses.replace(target, trusted=trusted)
        if tool_name == self.INITIAL_INSTRUCTIONS_TOOL_NAME:
            reply = await self._pool.activation(target)
            return self._decorated(target, reply, reply.activation_text)
        forwarded = {name: value for name, value in arguments.items() if name != CallTargetResolver.CWD_PARAMETER_NAME}
        reply = await self._pool.forward(target, tool_name, forwarded)
        return self._decorated(target, reply, None)

    def _decorated(self, target: CallTarget, reply: ChildReply, own_text: str | None) -> CallToolResult:
        """
        :param own_text: the text that replaces the child's result, if any; it is the activation text
        """
        prefix: list[ContentBlock] = []
        if target.root != self._launch_root:
            prefix.append(TextContent(text=f"[serena project {reply.project_name} at {target.root}]"))
        content: list[ContentBlock]
        if own_text is not None:
            self._projects_with_activation_text.add(target.root)
            content = [TextContent(text=own_text)]
        else:
            if target.root not in self._projects_with_activation_text:
                self._projects_with_activation_text.add(target.root)
                prefix.append(TextContent(text=reply.activation_text))
            content = list(reply.result.content)
        result = reply.result.model_copy(update={"content": [*prefix, *content]})
        return self._result_cap.apply(result)
