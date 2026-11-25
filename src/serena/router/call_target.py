# SPDX-License-Identifier: GPL-3.0-or-later

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from serena.util.path_renderer import PathRenderer
from serena.util.project_root import find_project_root, no_project_root_message


@dataclass(frozen=True)
class CallTarget:
    """
    The project and the caller's folder that one tool call is about.
    """

    root: Path
    """the real path of the project root the call runs on"""
    caller_dir: Path
    """the real path of the folder the caller works in"""
    trusted: bool = True
    """whether the project may run project-defined programs"""


class CallRefusedError(Exception):
    """
    Raised when a call cannot be given a target; the message is meant for the caller.
    """


class CallTargetResolver:
    """
    Decides which project a call is about: the folder of an absolute path argument if there is one, else the
    caller's folder (``cwd``), and the project root is the nearest project boundary above that folder.
    """

    CWD_PARAMETER_NAME = "cwd"

    def resolve(self, arguments: Mapping[str, Any]) -> CallTarget:
        """
        :param arguments: the arguments of a tool call, including ``cwd``
        :return: the target of the call
        :raises CallRefusedError: if ``cwd`` is missing, relative or not a directory, if the absolute paths lie in
            different projects, or if no project root is found
        """
        caller_dir = self._parse_cwd(arguments)

        # the folders named by absolute path arguments
        path_folders = [self._folder_of(path) for path in PathRenderer.absolute_paths_in(arguments)]
        folders = path_folders or [caller_dir]

        # their project roots, which must agree
        roots: dict[Path, Path] = {}
        for folder in folders:
            root = find_project_root(start=folder)
            if root is None:
                raise CallRefusedError(no_project_root_message(folder))
            roots.setdefault(Path(root), folder)
        if len(roots) > 1:
            described = "; ".join(f"{root} (from {folder})" for root, folder in roots.items())
            raise CallRefusedError(f"The absolute paths of this call lie in different projects: {described}. Make one call per project.")
        return CallTarget(root=next(iter(roots)), caller_dir=caller_dir)

    @classmethod
    def _parse_cwd(cls, arguments: Mapping[str, Any]) -> Path:
        cwd = arguments.get(cls.CWD_PARAMETER_NAME)
        if not isinstance(cwd, str) or not cwd:
            raise CallRefusedError(
                f"The required argument `{cls.CWD_PARAMETER_NAME}` is missing. Pass your current working directory, absolute."
            )
        if not os.path.isabs(cwd):
            raise CallRefusedError(f"`{cls.CWD_PARAMETER_NAME}` must be an absolute path, but is relative: {cwd}")
        if not os.path.isdir(cwd):
            raise CallRefusedError(f"`{cls.CWD_PARAMETER_NAME}` is not a directory: {cwd}")
        return Path(os.path.realpath(cwd))

    @staticmethod
    def _folder_of(path: str) -> Path:
        real = Path(os.path.realpath(path))
        return real if real.is_dir() else real.parent
