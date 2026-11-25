# SPDX-License-Identifier: GPL-3.0-or-later

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any, ClassVar, overload

if TYPE_CHECKING:
    from mcp.server.mcpserver import Context


@dataclass(frozen=True)
class PathRenderer:
    """
    Decides how file paths appear in the output of one tool call and parses paths in the input of that call
    back to paths relative to the project root.

    The output style is a property of the call: if the caller's folder lies inside the project root, paths are
    rendered relative to the root (as in a fresh launch on that folder); otherwise they are rendered absolute,
    since only an absolute path can be passed back from outside the project. Without a caller folder, paths stay
    relative.

    The renderer of the running call is the one made current via :meth:`active`; every producer of paths in
    tool output asks :meth:`current` and never decides the style itself.
    """

    CALLER_DIR_META_KEY: ClassVar[str] = "serena/callerDir"
    """the key of the request's ``_meta`` entry that carries the caller's folder"""

    PATH_PARAMETER_NAMES: ClassVar[tuple[str, ...]] = ("relative_path", "reference_file")
    """the names of tool parameters whose values are file paths"""

    OCCURRENCE_IDS_PARAMETER_NAME: ClassVar[str] = "occurrence_ids"
    """the name of the tool parameter whose values are occurrence ids (``<path>:<index>@<digest>``)"""

    _EXTERNAL_PATH_PREFIX: ClassVar[str] = "<ext:"
    _current: ClassVar[ContextVar["PathRenderer | None"]] = ContextVar("serena_path_renderer", default=None)

    project_root: str | None
    """the root of the project the call runs on; None if the call is not bound to a project (paths pass through)"""
    caller_dir: str | None = None
    """the absolute folder the caller works in, if known"""

    @classmethod
    def current(cls) -> "PathRenderer":
        """
        :return: the renderer of the running call, or one that leaves all paths unchanged if no call is running
        """
        return cls._current.get() or cls(project_root=None)

    @classmethod
    def for_call(cls, project_root: str | None, mcp_ctx: "Context | None") -> "PathRenderer":
        """
        :param project_root: the root of the project the call runs on, if any
        :param mcp_ctx: the MCP context of the call, whose request ``_meta`` may carry the caller's folder
        :return: the renderer for the call
        """
        return cls(project_root=project_root, caller_dir=cls._read_caller_dir(mcp_ctx))

    @classmethod
    def _read_caller_dir(cls, mcp_ctx: "Context | None") -> str | None:
        if mcp_ctx is None:
            return None
        try:
            meta: Mapping[str, Any] | None = mcp_ctx.request_context.meta
        except ValueError:  # the context is not bound to a request
            return None
        caller_dir = (meta or {}).get(cls.CALLER_DIR_META_KEY)
        if isinstance(caller_dir, str) and os.path.isabs(caller_dir):
            return caller_dir
        return None

    @contextmanager
    def active(self) -> Iterator["PathRenderer"]:
        """
        Makes this renderer the current one (for the calling thread) for the duration of the context.
        """
        token = self._current.set(self)
        try:
            yield self
        finally:
            self._current.reset(token)

    @cached_property
    def renders_absolute(self) -> bool:
        """
        :return: whether paths are rendered absolute, which is the case if and only if the caller's folder is known
            and lies outside the project root
        """
        if self.project_root is None or self.caller_dir is None:
            return False
        root = self._comparable(os.path.realpath(self.project_root))
        caller = self._comparable(os.path.realpath(self.caller_dir))
        try:
            return os.path.commonpath([root, caller]) != root
        except ValueError:  # e.g. different drives on Windows
            return True

    @staticmethod
    def _comparable(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    def _is_passed_through(self, path: str) -> bool:
        return self.project_root is None or os.path.isabs(path) or path.startswith(self._EXTERNAL_PATH_PREFIX)

    @overload
    def render(self, relative_path: str) -> str: ...

    @overload
    def render(self, relative_path: None) -> None: ...

    def render(self, relative_path: str | None) -> str | None:
        """
        :param relative_path: a path relative to the project root (as produced by Serena); paths that are already
            absolute or that encode an external file are not changed
        :return: the path in the style of the call
        """
        if relative_path is None or not self.renders_absolute or self._is_passed_through(relative_path):
            return relative_path
        assert self.project_root is not None
        return os.path.normpath(os.path.join(self.project_root, relative_path))

    def parse(self, path: str) -> str:
        """
        :param path: a path as passed by the caller, either relative to the project root or absolute
        :return: the path relative to the project root if the given path is absolute and inside the root;
            otherwise the path unchanged
        """
        if self.project_root is None or not os.path.isabs(path):
            return path
        root = os.path.normpath(self.project_root)
        absolute = os.path.normpath(path)
        try:
            if self._comparable(os.path.commonpath([root, absolute])) != self._comparable(root):
                return path
        except ValueError:
            return path
        return os.path.relpath(absolute, root)

    def render_occurrence_id(self, occurrence_id: str) -> str:
        """
        :param occurrence_id: an occurrence id of the form ``<relative_path>:<index>@<digest>``
        :return: the id with its path rendered in the style of the call
        """
        path, separator, rest = occurrence_id.rpartition(":")
        return self.render(path) + separator + rest if separator else occurrence_id

    def parse_occurrence_id(self, occurrence_id: str) -> str:
        """
        :param occurrence_id: an occurrence id whose path is relative to the project root or absolute
        :return: the id with a path inside the project root made relative to it
        """
        path, separator, rest = occurrence_id.rpartition(":")
        return self.parse(path) + separator + rest if separator else occurrence_id

    def parse_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """
        :param arguments: the arguments of a tool call
        :return: a copy in which every path and occurrence id inside the project root is relative to it
        """
        parsed = dict(arguments)
        for name in self.PATH_PARAMETER_NAMES:
            if isinstance(parsed.get(name), str):
                parsed[name] = self.parse(parsed[name])
        occurrence_ids = parsed.get(self.OCCURRENCE_IDS_PARAMETER_NAME)
        if isinstance(occurrence_ids, list):
            parsed[self.OCCURRENCE_IDS_PARAMETER_NAME] = [self.parse_occurrence_id(i) if isinstance(i, str) else i for i in occurrence_ids]
        return parsed

    @classmethod
    def absolute_paths_in(cls, arguments: Mapping[str, Any]) -> list[str]:
        """
        :param arguments: the arguments of a tool call
        :return: every absolute path among the path arguments and the occurrence ids, in argument order
        """
        paths: list[str] = []
        for name in cls.PATH_PARAMETER_NAMES:
            value = arguments.get(name)
            if isinstance(value, str):
                paths.append(value)
        occurrence_ids = arguments.get(cls.OCCURRENCE_IDS_PARAMETER_NAME)
        if isinstance(occurrence_ids, list):
            paths.extend(i.rpartition(":")[0] for i in occurrence_ids if isinstance(i, str))
        return [path for path in paths if os.path.isabs(path)]
