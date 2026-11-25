# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from types import TracebackType
from typing import Any, Protocol

import anyio
import anyio.abc
from anyio import to_thread
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED, CallToolResult, TextContent
from sensai.util import logging

from serena.router.call_target import CallTarget
from serena.router.project_facts import ProjectFacts, ProjectInspector
from serena.router.status_file import ChildState, ChildStatus, StatusFile
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
    untrusted: bool = False
    """whether every child runs untrusted, whatever its project's trust (the router's own ``--untrusted``)"""

    _BOOTSTRAP = "from serena.cli import top_level; top_level()"

    def command(self, root: Path, trusted: bool = True) -> list[str]:
        """
        :param root: the real path of the project root the child is launched on
        :param trusted: whether the project may run project-defined programs; if not, the child is launched untrusted
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
        if self.untrusted or not trusted:
            command.append("--untrusted")
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


class Clock(Protocol):
    """
    The source of time for the pool's idle stop.
    """

    def now(self) -> float:
        """
        :return: seconds, on a clock that never goes back
        """
        ...


class MonotonicClock:
    def now(self) -> float:
        return monotonic()


@dataclass(frozen=True)
class ChildReply:
    """
    A child's answer to a call, with what the router knows about the child's project.
    """

    result: CallToolResult
    project_name: str
    activation_text: str


class _ChildProcess:
    """
    One child Serena and its MCP client session. The process and the session live in a task of their own,
    since the transport's context must be exited by the task that entered it.
    """

    INITIAL_INSTRUCTIONS_TOOL_NAME = "initial_instructions"

    def __init__(
        self,
        root: Path,
        options: ChildOptions,
        trusted: bool,
        inspector: ProjectInspector | None,
        last_used: float,
        on_change: Callable[[], None],
    ):
        self.root = root
        self.trusted = trusted
        self.last_used = last_used
        self.active_calls = 0
        self.activation_text = ""
        self.facts = ProjectFacts(name=root.name)
        self._options = options
        self._inspector = inspector
        self._on_change = on_change
        self._session: ClientSession | None = None
        self._ready = anyio.Event()
        self._stop = anyio.Event()
        self._failure: BaseException | None = None

    async def run(self) -> None:
        argv = self._options.command(self.root, self.trusted)
        parameters = StdioServerParameters(command=argv[0], args=argv[1:], env=dict(os.environ), cwd=self.root)
        log.info("Starting child Serena for %s", self.root)
        try:
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self.activation_text = await self._fetch_activation_text(session)
                    if self._inspector is not None:
                        self.facts = await to_thread.run_sync(self._inspector.inspect, self.root)
                    self._session = session
                    self._ready.set()
                    self._on_change()
                    await self._stop.wait()
        except Exception as e:
            log.error("Child Serena for %s failed: %s", self.root, e)
            self._failure = e
        finally:
            self._session = None
            self._stop.set()
            self._ready.set()
            self._on_change()
            log.info("Child Serena for %s stopped", self.root)

    @classmethod
    async def _fetch_activation_text(cls, session: ClientSession) -> str:
        result = await session.call_tool(cls.INITIAL_INSTRUCTIONS_TOOL_NAME, {})
        return "".join(block.text for block in result.content if isinstance(block, TextContent))

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

    @property
    def state(self) -> ChildState | None:
        """
        :return: the child's state for the status file, or None once it has stopped
        """
        if self._stop.is_set():
            return None
        if self._session is None:
            return ChildState.STARTING
        return ChildState.READY if self.active_calls > 0 else ChildState.IDLE

    def stop(self) -> None:
        self._stop.set()
        self._on_change()


class ChildPool:
    """
    The owner of all children: one per project root (by real path), started on first use, restarted on the
    call after it died or after the project's trust changed, and stopped together when the pool is left.
    A child idle for ``idle_timeout`` seconds stops, and at most ``max_children`` run: the least recently used
    stops first.

    Use as an async context manager, entered and left by the same task.
    """

    DEFAULT_IDLE_TIMEOUT = 60 * 60.0
    DEFAULT_MAX_CHILDREN = 8
    DEFAULT_SWEEP_INTERVAL = 60.0

    def __init__(
        self,
        options: ChildOptions,
        inspector: ProjectInspector | None = None,
        clock: Clock | None = None,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        max_children: int = DEFAULT_MAX_CHILDREN,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL,
        status_file: StatusFile | None = None,
    ):
        """
        :param options: the launch options of the children
        :param inspector: reads each project's configuration when its child has started
        :param clock: the source of time for the idle stop
        :param idle_timeout: seconds after which an unused child stops
        :param max_children: the number of children above which the least recently used one stops
        :param sweep_interval: seconds between looks for idle children
        :param status_file: where the children and their states are published on every start, ready, stop and idle change
        """
        self._options = options
        self._inspector = inspector
        self._clock = clock or MonotonicClock()
        self._idle_timeout = idle_timeout
        self._max_children = max_children
        self._sweep_interval = sweep_interval
        self._status_file = status_file
        self._children: dict[Path, _ChildProcess] = {}
        self._start_locks: dict[Path, anyio.Lock] = {}
        self._task_group: anyio.abc.TaskGroup | None = None
        self._closing = anyio.Event()

    async def __aenter__(self) -> "ChildPool":
        self._task_group = anyio.create_task_group()
        await self._task_group.__aenter__()
        self._task_group.start_soon(self._sweep_periodically)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> bool | None:
        assert self._task_group is not None
        self._closing.set()
        for child in self._children.values():
            child.stop()
        try:
            return await self._task_group.__aexit__(exc_type, exc_value, traceback)
        finally:
            if self._status_file is not None:
                self._status_file.remove()

    async def forward(self, target: CallTarget, tool_name: str, arguments: dict[str, Any]) -> ChildReply:
        """
        :param target: the project, caller folder and trust of the call
        :param tool_name: the tool to call on the project's child
        :param arguments: the arguments of the tool, without ``cwd``
        :return: the child's result, unchanged, with what is known about its project
        :raises ChildFailedError: if the child cannot be started or its connection closes
        """
        child = await self._running_child(target)
        try:
            result = await child.call_tool(tool_name, arguments, target.caller_dir)
        finally:
            self._release(child)
        return ChildReply(result, child.facts.name, child.activation_text)

    async def activation(self, target: CallTarget) -> ChildReply:
        """
        :return: the activation text of the target's project, without a call to the child (the result is empty)
        :raises ChildFailedError: if the child cannot be started
        """
        child = await self._running_child(target)
        self._release(child)
        return ChildReply(CallToolResult(content=[]), child.facts.name, child.activation_text)

    def _release(self, child: _ChildProcess) -> None:
        child.active_calls -= 1
        child.last_used = self._clock.now()
        self._publish()

    def _publish(self) -> None:
        if self._status_file is None:
            return
        statuses = [
            ChildStatus(root, child.facts.name, state) for root, child in self._children.items() if (state := child.state) is not None
        ]
        try:
            self._status_file.write(statuses)
        except OSError as e:
            log.warning("Cannot write the status file %s: %s", self._status_file.path, e)

    def stop_idle_children(self) -> None:
        """
        Stops the children that no call has used for the idle timeout.
        """
        now = self._clock.now()
        for root, child in list(self._children.items()):
            if child.is_running and child.active_calls == 0 and now - child.last_used >= self._idle_timeout:
                log.info("Stopping the idle child Serena for %s", root)
                child.stop()
                del self._children[root]

    async def _sweep_periodically(self) -> None:
        while not self._closing.is_set():
            with anyio.move_on_after(self._sweep_interval):
                await self._closing.wait()
            self.stop_idle_children()

    def _make_room_for_another_child(self) -> None:
        running = {root: child for root, child in self._children.items() if child.is_running}
        while len(running) >= self._max_children:
            idle = [(child.last_used, root) for root, child in running.items() if child.active_calls == 0]
            if not idle:
                return
            _, root = min(idle)
            log.info("Stopping the least recently used child Serena for %s", root)
            running.pop(root).stop()
            del self._children[root]

    async def _running_child(self, target: CallTarget) -> _ChildProcess:
        root = target.root
        lock = self._start_locks.setdefault(root, anyio.Lock())
        async with lock:
            child = self._children.get(root)
            if child is not None and child.is_running and child.trusted != target.trusted:
                log.info("Restarting the child Serena for %s, as its trust changed to %s", root, target.trusted)
                child.stop()
                child = None
            if child is None or not child.is_running:
                assert self._task_group is not None
                self._children.pop(root, None)
                self._make_room_for_another_child()
                child = _ChildProcess(root, self._options, target.trusted, self._inspector, self._clock.now(), self._publish)
                self._children[root] = child
                self._publish()
                self._task_group.start_soon(child.run)
            child.last_used = self._clock.now()
            # claimed before the wait, so that a child which comes up for a call is ready and never idle in between
            child.active_calls += 1
            try:
                await child.wait_until_ready()
            except BaseException:
                self._release(child)
                raise
            self._publish()
            return child
