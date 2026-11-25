# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sensai.util import logging

from serena.config.serena_config import ProjectConfig, SerenaConfig
from serena.config.serena_paths import SerenaPaths

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectFacts:
    """
    What the router knows about a project from its configuration.
    """

    name: str


def write_atomically(path: Path, text: str) -> None:
    """
    Replaces the file with the text, so that a reader sees the old or the new content and never a part of it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w") as file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class CodeFileList:
    """
    The file extensions that the language servers of a project handle, kept in a file under ``<SERENA_HOME>/follow_cwd``
    for hooks, which must not import Serena's configuration to find them.
    """

    FOLDER_NAME = "follow_cwd"
    SUFFIX = ".code-files"

    def __init__(self, serena_home: Path | None = None):
        self._folder = (serena_home or Path(SerenaPaths().serena_user_home_dir)) / self.FOLDER_NAME

    def path_of(self, root: Path) -> Path:
        """
        :param root: the real path of a project root
        :return: the file that lists the code-file extensions of the project
        """
        digest = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        return self._folder / f"{digest}{self.SUFFIX}"

    def write(self, root: Path, extensions: list[str]) -> None:
        """
        Writes the extensions one per line, replacing the file atomically.
        """
        write_atomically(self.path_of(root), "".join(f"{extension}\n" for extension in extensions))


class ProjectInspector:
    """
    Reads a project's configuration when its child has started: its name, and the code-file list for hooks.
    """

    def __init__(self, serena_config: SerenaConfig, code_files: CodeFileList | None = None):
        self._serena_config = serena_config
        self._code_files = code_files or CodeFileList()

    def inspect(self, root: Path) -> ProjectFacts:
        """
        Writes the project's code-file list, unless its language servers are not what finds its code (the JetBrains
        backend), and returns the project's facts. Blocking: run it in a thread.
        """
        try:
            project_config = ProjectConfig.load(root, self._serena_config)
        except Exception as e:
            log.warning("Cannot read the configuration of the project at %s: %s", root, e)
            return ProjectFacts(name=root.name)
        backend = project_config.language_backend or self._serena_config.language_backend
        if backend.is_lsp():
            extensions = sorted({ext for ls_id in project_config.language_servers for ext in ls_id.get_source_fn_matcher().file_extensions})
            self._code_files.write(root, extensions)
        return ProjectFacts(name=project_config.project_name)
