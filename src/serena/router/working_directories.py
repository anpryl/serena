# SPDX-License-Identifier: GPL-3.0-or-later

import os
import warnings
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from mcp.server.session import ServerSession
from sensai.util import logging

log = logging.getLogger(__name__)


class RootsSource(Protocol):
    """
    Where the client's roots come from.
    """

    async def list_roots(self) -> list[Path] | None:
        """
        :return: the folders of the client's roots, or None if the client does not say
        """
        ...


class ClientRoots:
    """
    The roots of the MCP client behind a server session (``roots/list``).
    """

    def __init__(self, session: ServerSession):
        self._session = session

    async def list_roots(self) -> list[Path] | None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                result = await self._session.list_roots()
        except Exception as e:
            log.info("The client does not list its roots: %s", e)
            return None
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
