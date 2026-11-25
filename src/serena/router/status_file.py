# SPDX-License-Identifier: GPL-3.0-or-later

import json
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
    The children the router runs, kept in ``<SERENA_HOME>/follow_cwd/state.json`` for a status line, since Claude Code
    passes no MCP state to one.
    """

    FILE_NAME = "state.json"

    def __init__(self, serena_home: Path | None = None):
        self.path = (serena_home or Path(SerenaPaths().serena_user_home_dir)) / CodeFileList.FOLDER_NAME / self.FILE_NAME

    def write(self, children: list[ChildStatus]) -> None:
        """
        Replaces the file atomically with ``{"children": [{"root", "name", "state"}]}``.
        """
        entries = [{"root": str(child.root), "name": child.name, "state": child.state.value} for child in children]
        write_atomically(self.path, json.dumps({"children": entries}))
