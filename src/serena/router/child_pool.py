# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import anyio
import anyio.abc
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, CallToolResult
from sensai.util import logging

from serena.router.call_target import CallTarget
from serena.util.path_renderer import PathRenderer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChildOptions:
    """
    The launch options that every child Serena inherits from the router.
    """

    context: str
    """the built-in context name or the path of the context file"""
    modes: tuple[str, ...] = ()
    added_modes: tuple[str, ...] = ()
    language_backend: str | None = None
    tool_timeout: float | None = None
    log_level: str | None = None

    _BOOTSTRAP = "from serena.cli import top_level; top_level()"

    def command(self, root: Path) -> list[str]:
        """
        :param root: the real path of the project root the child is launched on
        :return: the command line that starts a child Serena on the root, running on the router's own interpreter
            without the working directory on its module path
        """
        command = [
            sys.executable,
            "-P",
            "-c",
            self._BOOTSTRAP,
            "start-mcp-server",
            "--context",
            self.context,
            "--project",
            str(root),
            "--project-registry",
            "false",
            "--read-only-config",
            "true",
            "--agent-interface",
            "tools",
            "--enable-web-dashboard",
            "false",
            "--open-web-dashboard",
            "false",
            "--enable-gui-log-window",
            "false",
        ]
        for mode in self.modes:
            command += ["--mode", mode]
        for mode in self.added_modes:
            command += ["--add-mode", mode]
        if self.language_backend is not None:
            command += ["--language-backend", self.language_backend]
        if self.tool_timeout is not None:
            command += ["--tool-timeout", str(self.tool_timeout)]
        if self.log_level is not None:
            command += ["--log-level", self.log_level]
        return command


class ChildFailedError(ToolError):
    """
    Raised when a child cannot be started or stops answering; the next call to its root starts a new child.
    """


class _ChildProcess:
    """
    One child Serena and its MCP client session. The process and the session live in a task of their own,
    since the transport's context must be exited by the task that entered it.
    """

    def __init__(self, root: Path, options: ChildOptions):
        self.root = root
        self._options = options
        self._session: ClientSession | None = None
        self._ready = anyio.Event()
        self._stop = anyio.Event()
        self._failure: BaseException | None = None

    async def run(self) -> None:
        argv = self._options.command(self.root)
        parameters = StdioServerParameters(command=argv[0], args=argv[1:], env=dict(os.environ), cwd=self.root)
        log.info("Starting child Serena for %s", self.root)
        try:
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except Exception as e:
            log.error("Child Serena for %s failed: %s", self.root, e)
            self._failure = e
        finally:
            self._session = None
            self._stop.set()
            self._ready.set()
            log.info("Child Serena for %s stopped", self.root)

    async def wait_until_ready(self) -> None:
        """
        :raises ChildFailedError: if the child did not come up
        """
        await self._ready.wait()
        if self._session is None:
            raise ChildFailedError(f"The Serena child for {self.root} could not be started: {self._failure or 'it exited'}")

    @property
    def is_running(self) -> bool:
        return self._session is not None and not self._stop.is_set()

    async def call_tool(self, name: str, arguments: dict[str, Any], caller_dir: Path) -> CallToolResult:
        """
        :raises ChildFailedError: if the child's connection is closed, which also marks the child as stopped
        """
        session = self._session
        if session is None:
            raise ChildFailedError(f"The Serena child for {self.root} is not running")
        try:
            return await session.call_tool(name, arguments, meta={PathRenderer.CALLER_DIR_META_KEY: str(caller_dir)})
        except (anyio.ClosedResourceError, anyio.BrokenResourceError) as e:
            self.stop()
            raise ChildFailedError(f"The Serena child for {self.root} stopped answering: {e!r}") from e
        except MCPError as e:
            if e.code != CONNECTION_CLOSED:
                raise
            self.stop()
            raise ChildFailedError(f"The Serena child for {self.root} stopped answering: {e}") from e

    def stop(self) -> None:
        self._stop.set()


class ChildPool:
    """
    The owner of all children: one per project root (by real path), started on first use, restarted on the
    call after it died, and stopped together when the pool is left.

    Use as an async context manager, entered and left by the same task.
    """

    def __init__(self, options: ChildOptions):
        self._options = options
        self._children: dict[Path, _ChildProcess] = {}
        self._start_locks: dict[Path, anyio.Lock] = {}
        self._task_group: anyio.abc.TaskGroup | None = None

    async def __aenter__(self) -> "ChildPool":
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> bool | None:
        assert self._task_group is not None
        for child in self._children.values():
            child.stop()
        return await self._task_group.__aexit__(exc_type, exc_value, traceback)

    async def forward(self, target: CallTarget, tool_name: str, arguments: dict[str, Any]) -> CallToolResult:
        """
        :param target: the project and caller folder of the call
        :param tool_name: the tool to call on the project's child
        :param arguments: the arguments of the tool, without ``cwd``
        :return: the child's result, unchanged
        :raises ChildFailedError: if the child cannot be started or its connection closes
        """
        child = await self._running_child(target.root)
        return await child.call_tool(tool_name, arguments, target.caller_dir)

    async def _running_child(self, root: Path) -> _ChildProcess:
        lock = self._start_locks.setdefault(root, anyio.Lock())
        async with lock:
            child = self._children.get(root)
            if child is None or not child.is_running:
                assert self._task_group is not None
                child = _ChildProcess(root, self._options)
                self._children[root] = child
                self._task_group.start_soon(child.run)
            await child.wait_until_ready()
            return child
