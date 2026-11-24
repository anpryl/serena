"""
Defines settings for Solid-LSP
"""
# SPDX-License-Identifier: MIT

import logging
import os
import pathlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Union

from sensai.util.helper import count_not_none
from sensai.util.string import ToStringMixin

if TYPE_CHECKING:
    from solidlsp.ls_config import LanguageServerId, LanguageServerIdLike

log = logging.getLogger(__name__)


SOLIDLSP_RESOURCES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")


class WriteRefusal(Exception):
    """
    A write guard's refusal of a write (see :class:`WriteGuard`).
    """


@dataclass(frozen=True)
class Rename:
    """
    A rename of a file (or directory).
    """

    old_path: str
    """the absolute path renamed"""
    new_path: str
    """the absolute path it is renamed to"""


@dataclass
class WriteBatchPlan:
    """
    The writes a batch of writes to a project's files will make (see :meth:`WriteGuard.write_batch_context`).
    """

    file_paths: list[str] = field(default_factory=list)
    """the absolute paths of the files whose contents the batch writes"""
    renames: list[Rename] = field(default_factory=list)
    """the renames the batch makes"""


class WriteGuard(ABC):
    """
    The checks applied to the file system writes made on a project's behalf, right before each write.
    """

    @abstractmethod
    def check_write(self, path: str) -> None:
        """
        Checks a write to the project's files (the repository).

        :param path: the absolute path to be written to (created, modified, renamed or removed)
        :raises WriteRefusal: if the write must not happen
        """

    def check_rename(self, old_path: str, new_path: str) -> None:
        """
        Checks a rename of one of the project's files (or directories).

        :param old_path: the absolute path to be renamed
        :param new_path: the absolute path it is to be renamed to
        :raises WriteRefusal: if the rename must not happen
        """
        self.check_write(old_path)
        self.check_write(new_path)

    def check_entry_write(self, path: str) -> None:
        """
        Checks a write acting on the entry at a path of the project's files itself (e.g. its removal), which does not
        follow the path's last component if it is a symbolic link. By default, it is checked as any write.

        :param path: the absolute path of the entry
        :raises WriteRefusal: if the write must not happen
        """
        self.check_write(path)

    def check_data_entry_write(self, path: str) -> None:
        """
        Checks a write acting on the entry at a path of the project's data folder itself (e.g. its removal), which does
        not follow the path's last component if it is a symbolic link. By default, it is checked as any data write.

        :param path: the absolute path of the entry
        :raises WriteRefusal: if the write must not happen
        """
        self.check_data_write(path)

    def report_write(self, path: str) -> None:
        """
        Reports a write to one of the project's files, right after it was made (following :meth:`check_write`), so that
        the guard can record what the write left. By default, nothing is recorded.

        :param path: the absolute path written to
        """

    def report_rename(self, rename: Rename) -> None:
        """
        Reports a rename, right after it was made (following :meth:`check_rename`), so that the guard can record what
        the rename left. By default, nothing is recorded.

        :param rename: the rename
        """

    @contextmanager
    def write_batch_context(self, plan: WriteBatchPlan) -> Iterator[None]:
        """
        Context manager for a batch of writes to the project's files (e.g. a language server's multi-file edit), checking
        every one of them (see :meth:`check_write` and :meth:`check_rename`) before the first is made: a write refused
        by this check causes no write at all. Each write is checked again as it is made; a write refused then is not
        made, and whether the batch's earlier writes are rolled back is up to the guard (see overriding methods; by
        default, they stay in place).

        :param plan: the writes the batch will make
        :raises WriteRefusal: if a write of the batch must not happen
        """
        for path in plan.file_paths:
            self.check_write(path)
        for rename in plan.renames:
            self.check_rename(rename.old_path, rename.new_path)
        yield

    @abstractmethod
    def check_data_write(self, path: str) -> None:
        """
        Checks a write to the project's data folder (e.g. a language server's cache folder or cache file).

        :param path: the absolute path to be written to (created or modified)
        :raises WriteRefusal: if the write must not happen
        """


@dataclass
class SolidLSPSettings:
    """
    Configures SolidLSP-specific data storage as well as global settings.

    Note: Server instance-specific settings belong in LanguageServerConfig, not here.
    """

    solidlsp_dir: str = str(pathlib.Path.home() / ".solidlsp")
    """
    Path to the directory in which to store global Solid-LSP data (which is not project-specific)
    """
    project_data_path: str = ""
    """
    Absolute path to a directory where Solid-LSP can store project-specific data, e.g. cache files.
    For instance, if this is "/home/user/myproject/.solidlsp",
    then Solid-LSP will store project-specific data (e.g. caches) in that directory.
    """
    ls_specific_settings: dict[Union[str, "LanguageServerId"], dict[str, Any]] = field(default_factory=dict)
    """
    Advanced configuration option allowing to configure language server implementation specific options.
    Maps language server identifiers or their string keys to a dictionary of options for that language server.
    See constructors of the corresponding LS implementations within SolidLSP to see which options are available.
    Some options are centrally supported. See Serena's documentation for details.
    """
    write_guard: WriteGuard | None = None
    """
    The checks to apply right before Solid-LSP writes to the repository or to the project data folder, if any.
    """

    def __post_init__(self) -> None:
        os.makedirs(str(self.solidlsp_dir), exist_ok=True)
        os.makedirs(str(self.ls_resources_dir), exist_ok=True)

    @property
    def ls_resources_dir(self) -> str:
        return os.path.join(str(self.solidlsp_dir), "language_servers", "static")

    class CustomLSSettings(ToStringMixin):
        """
        Represents custom (user-specified) settings for a specific language server.
        """

        def __init__(self, settings: dict[str, Any] | None) -> None:
            self.settings = settings or {}

        def get(self, key: str, default_value: Any = None) -> Any:
            """
            Returns the custom setting for the given key or the default value if not set.
            If a custom value is set for the given key, the retrieval is logged.

            :param key: the key
            :param default_value: the default value to use if no custom value is set
            :return: the value
            """
            if key in self.settings:
                value = self.settings[key]
                log.info("Using custom LS setting %s for key '%s'", value, key)
            else:
                value = default_value
            return value

    def get_ls_specific_settings(self, ls_id: "LanguageServerIdLike") -> CustomLSSettings:
        """
        Gets the custom settings for the given language server

        :param ls_id: the language server identifier for which to retrieve settings
        :return: a dictionary of settings for the language server
        """
        from solidlsp.ls_config import LanguageServerId

        settings_dict = self.ls_specific_settings.get(ls_id.get_key())
        if isinstance(ls_id, LanguageServerId):
            settings_dict2 = self.ls_specific_settings.get(ls_id)
            if count_not_none(settings_dict, settings_dict2) > 1:
                raise ValueError(f"Duplicate LS-specific settings for {ls_id.get_key()}: Found keys {ls_id.get_key()} and {ls_id}")
            settings_dict = settings_dict or settings_dict2
        return self.CustomLSSettings(settings_dict)
