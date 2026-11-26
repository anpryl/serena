# SPDX-License-Identifier: GPL-3.0-or-later

import os
import warnings
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from mcp.server.mcpserver.server import Context
from mcp_types import InputRequiredResult, ListRootsRequest, ListRootsResult
from mcp_types.version import is_version_at_least
from sensai.util import logging

log = logging.getLogger(__name__)


class RootsSource(Protocol):
    """
    Where the client's roots come from.
    """

    async def list_roots(self) -> list[Path] | None:
        """
        :return: the folders of the client's roots, or None if the client does not say
        :raises RootsRequired: if the client can give its roots only in a retry of the current call
        """
        ...


class RootsRequired(Exception):
    """
    Raised when the client's roots are not known yet but the client can give them: the call must be answered with a
    request for them (:meth:`ClientRoots.input_required`) and is then retried with them.
    """


class ClientRoots:
    """
    The roots of the MCP client behind a tool call. Up to protocol 2025-11-25 they are asked of the client in a
    ``roots/list`` request. From 2026-07-28 on the server cannot ask by itself: the call is answered with an input
    request, and the client retries it with the roots.
    """

    INPUT_KEY = "serena:roots"
    INPUT_REQUIRED_PROTOCOL_VERSION = "2026-07-28"

    def __init__(self, context: Context):
        self._context = context

    @classmethod
    def input_required(cls) -> InputRequiredResult:
        """
        :return: the answer to a call that must be retried with the client's roots
        """
        return InputRequiredResult(input_requests={cls.INPUT_KEY: ListRootsRequest()})

    async def list_roots(self) -> list[Path] | None:
        protocol_version = self._context.protocol_version
        if protocol_version is None or not is_version_at_least(protocol_version, self.INPUT_REQUIRED_PROTOCOL_VERSION):
            return await self._ask_client()

        responses = self._context.input_responses
        response = responses.get(self.INPUT_KEY) if responses else None
        if response is not None:
            if isinstance(response, ListRootsResult):
                return self._folders_of(response)
            log.info("The client answered the request for its roots with something else: %r", response)
            return None

        capabilities = self._context.client_capabilities
        if capabilities is not None and capabilities.roots is not None:
            raise RootsRequired
        return None

    async def _ask_client(self) -> list[Path] | None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = await self._context.session.list_roots()
        except Exception as e:
            log.info("The client does not list its roots: %s", e)
            return None
        return self._folders_of(result)

    @staticmethod
    def _folders_of(result: ListRootsResult) -> list[Path]:
        folders = []
        for root in result.roots:
            parsed = urlparse(str(root.uri))
            if parsed.scheme == "file":
                folders.append(Path(os.path.realpath(unquote(parsed.path))))
        return folders


class WorkingDirectories:
    """
    The folders the user consented to work in, from the client's roots (cached, until the client says they changed);
    without roots, only the folder the router was launched in.
    """

    def __init__(self, launch_dir: Path):
        self._launch_dir = Path(os.path.realpath(launch_dir))
        self._roots: list[Path] | None = None

    def invalidate(self) -> None:
        """
        Forgets the cached roots; the next judgement asks the client again.
        """
        self._roots = None

    async def covers(self, project_root: Path, source: RootsSource) -> bool:
        """
        :param project_root: the real path of a project root
        :param source: where to ask for the client's roots
        :return: whether the project is one the user consented to: it lies inside a working directory, or a
            working directory lies inside it (as a fresh launch in a subfolder of a project works on that project).
            A project judged outside is judged again after asking the client once more.
        :raises RootsRequired: if the client's roots are needed and the client can give them only in a retry of the call
        """
        if self._roots is None:
            await self._refresh(source)
        if self._covers(project_root):
            return True
        await self._refresh(source)
        return self._covers(project_root)

    async def _refresh(self, source: RootsSource) -> None:
        self._roots = await source.list_roots() or []

    def _covers(self, project_root: Path) -> bool:
        assert self._roots is not None
        for directory in self._roots or [self._launch_dir]:
            if project_root.is_relative_to(directory) or directory.is_relative_to(project_root):
                return True
        return False
