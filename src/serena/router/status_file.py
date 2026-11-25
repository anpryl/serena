# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from serena.config.serena_paths import SerenaPaths
from serena.router.project_facts import CodeFileList, write_atomically


class ChildState(str, Enum):
    STARTING = "starting"
    """the child process is launched and does not answer yet"""
    READY = "ready"
    """the child runs and a call is in flight"""
    IDLE = "idle"
    """the child runs and no call is in flight"""


@dataclass(frozen=True)
class ChildStatus:
    root: Path
    name: str
    state: ChildState


class StatusFile:
    """
    The children one router runs, kept in ``<SERENA_HOME>/follow_cwd/state/<router pid>.json`` for a status line, since
    Claude Code passes no MCP state to one. Every router has a file of its own, so that sessions on one SERENA_HOME
    do not overwrite each other. A script picks the files whose ``launch_root`` fits its session and whose pid is
    alive, which skips the files of routers that were killed.
    """

    FOLDER_NAME = "state"

    def __init__(self, launch_root: Path, serena_home: Path | None = None, pid: int | None = None):
        """
        :param launch_root: the folder the router was started for: its project root, or its working directory
        :param serena_home: the Serena home folder, by default the user's
        :param pid: the router's process id, by default this process's
        """
        self._launch_root = launch_root
        self._pid = os.getpid() if pid is None else pid
        folder = (serena_home or Path(SerenaPaths().serena_user_home_dir)) / CodeFileList.FOLDER_NAME / self.FOLDER_NAME
        self.path = folder / f"{self._pid}.json"

    def write(self, children: list[ChildStatus]) -> None:
        """
        Replaces the file atomically with ``{"pid", "launch_root", "children": [{"root", "name", "state"}]}``.
        """
        entries = [{"root": str(child.root), "name": child.name, "state": child.state.value} for child in children]
        write_atomically(self.path, json.dumps({"pid": self._pid, "launch_root": str(self._launch_root), "children": entries}))

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)
