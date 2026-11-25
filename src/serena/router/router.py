# SPDX-License-Identifier: GPL-3.0-or-later

from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult

from serena.router.call_target import CallRefusedError, CallTargetResolver
from serena.router.child_pool import ChildPool
from serena.router.result_cap import ResultCap


class Router:
    """
    Sends each tool call to the child Serena of the project that the call is about, and caps the size of the result.
    """

    def __init__(self, pool: ChildPool, resolver: CallTargetResolver | None = None, result_cap: ResultCap | None = None):
        self._pool = pool
        self._resolver = resolver or CallTargetResolver()
        self._result_cap = result_cap or ResultCap()

    async def route(self, tool_name: str, arguments: dict[str, Any]) -> CallToolResult:
        """
        :param tool_name: the tool that was called
        :param arguments: the arguments of the call, including ``cwd``
        :return: the child's result
        :raises ToolError: if the call has no valid target or the child fails
        """
        try:
            target = self._resolver.resolve(arguments)
        except CallRefusedError as e:
            raise ToolError(str(e)) from e
        forwarded = {name: value for name, value in arguments.items() if name != CallTargetResolver.CWD_PARAMETER_NAME}
        result = await self._pool.forward(target, tool_name, forwarded)
        return self._result_cap.apply(result)
