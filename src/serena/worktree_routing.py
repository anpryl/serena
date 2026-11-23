# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import os
import re
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from serena.config.serena_config import ProjectConfig
from serena.project import Project
from serena.util.file_system import FileStateDigest
from solidlsp.util.subprocess_util import subprocess_run

if TYPE_CHECKING:
    from serena.agent import SerenaAgent
    from serena.tools import Tool

log = logging.getLogger(__name__)


class WorktreeRoutingRefusal(Exception):
    """
    A tool call's refusal, raised when its path lies in a git worktree the call must not or cannot be routed to.
    """


@dataclass(frozen=True)
class WorktreeRoute:
    """
    The routing of a tool call to a git worktree of the active project's repository.
    """

    project: Project
    """the project for the worktree, against which the call runs"""
    kwargs: dict[str, Any]
    """the call's arguments, with `relative_path` rewritten relative to the worktree's root"""

    def annotate_error(self, message: str) -> str:
        """
        :param message: the message of an error the routed call raised
        :return: the message, stating the worktree any relative path in it is relative to (for messages from components
            that do not name files by the project's output paths, see `Project.output_paths`)
        """
        return (
            f"{message}\n(This call ran in the git worktree {self.project.project_root}: a relative path in this message is "
            "relative to that worktree. Pass it as an absolute path under the worktree.)"
        )


class WorktreeRouter:
    """
    Router for tool calls whose `relative_path` is an absolute path inside another git worktree of the active project's
    repository. Such a call is routed to a project for that worktree; any other call is left as it is.

    The worktrees are read from git on every call that could route. A path is matched as spelled: walking up from the
    path itself, each directory is compared with the listed roots by device and inode, so that differently spelled
    paths of one root (e.g. via a symlinked ancestor) match alike, while a symlink within the path is never taken for
    the root it points to.
    """

    @dataclass(frozen=True)
    class _FileId:
        """
        The identity of a file system entry (device and inode), independent of the path spelling used to reach it.
        """

        device: int
        inode: int

        @classmethod
        def of(cls, path: str) -> Optional["WorktreeRouter._FileId"]:
            """
            :param path: the path of the entry, following symlinks
            :return: the entry's identity or None if the path cannot be accessed
            """
            try:
                stat_result = os.stat(path)
            except OSError:
                return None
            return cls(device=stat_result.st_dev, inode=stat_result.st_ino)

        @classmethod
        def of_directory(cls, path: str) -> Optional["WorktreeRouter._FileId"]:
            """
            :param path: the path of the entry; symlinks among its ancestors are followed, but the entry itself is not
            :return: the entry's identity or None if it is not a directory (in particular, if it is a symlink) or cannot be
                accessed
            """
            try:
                stat_result = os.lstat(path)
            except OSError:
                return None
            if not stat.S_ISDIR(stat_result.st_mode):
                return None
            return cls(device=stat_result.st_dev, inode=stat_result.st_ino)

    @dataclass(frozen=True)
    class _Worktree:
        """
        An entry of `git worktree list --porcelain`.
        """

        root: str
        is_bare: bool
        is_prunable: bool

    @dataclass(frozen=True)
    class _ProjectEntry:
        """
        A cached worktree project, along with what decides whether it is still current.
        """

        project: Project
        read_ignore_file_paths: tuple[str, ...]
        """the paths of the ignore files read when the project was built"""
        ignore_file_digests: dict[str, str]
        """the state (see `FileStateDigest`) of each ignore file the project's ignore rules may depend on (see
        `_list_ignore_file_paths`), as it was when the project was built, by path"""

    @dataclass(frozen=True)
    class _WorktreeListing:
        """
        The worktrees git lists for a repository.
        """

        worktrees: list["WorktreeRouter._Worktree"]
        is_complete: bool
        """whether the listing names every worktree; not so for newline-terminated output, which cannot name a path
        holding a newline"""

    @dataclass(frozen=True)
    class _RoutableRoots:
        """
        The worktree roots calls may be routed to, by identity.
        """

        root_ids: dict["WorktreeRouter._FileId", str]
        is_complete: bool
        """whether the listing the roots come from names every worktree (see `_WorktreeListing.is_complete`)"""

    _OTHER_LISTING_LINE_PREFIXES = ("HEAD ", "branch ", "detached", "locked")
    """the prefixes of the lines of `git worktree list --porcelain` that are not parsed"""

    _PATH_SEPARATORS_PATTERN = "|".join(re.escape(separator) for separator in (os.sep, os.altsep) if separator)
    """a pattern matching every path separator of the platform"""

    def __init__(self, agent: "SerenaAgent"):
        """
        :param agent: the agent whose active project determines the repository and whose configuration enables routing
        """
        self._agent = agent
        self._entries_by_root_id: dict[WorktreeRouter._FileId, WorktreeRouter._ProjectEntry] = {}
        self._projects_lock = threading.Lock()

    def route(self, tool: "Tool", kwargs: dict[str, Any]) -> WorktreeRoute | None:
        """
        Determines the routing of a tool call.

        :param tool: the tool being called
        :param kwargs: the arguments of the tool call
        :return: the call's routing or None if the call runs against the active project as it is
        :raises WorktreeRoutingRefusal: if the path's spelling and its resolution select different worktrees; if it lies
            in another worktree while the active project's root is not the root of its own worktree; if the worktree
            fails the identity check or its project configuration does not make the tool available; or if the worktrees
            cannot be listed and the path may lie in a worktree nested inside the active project
        """
        # check applicability
        if not self._is_enabled():
            return None
        path = kwargs.get("relative_path")
        if not isinstance(path, str) or not os.path.isabs(path):
            return None
        active_project = self._agent.get_active_project()
        if active_project is None:
            return None

        # determine the worktree root the path selects as spelled and the one it resolves into, the active project's own
        # worktree included; a `..` segment after a symlink names another file than its lexical normalisation, so a path
        # with such a segment selects no root by its spelling
        roots = self._get_routable_roots(active_project.project_root)
        if roots is None:
            if self._may_be_inside_repository(active_project.project_root):
                self._check_no_possible_nested_worktree(path, active_project.project_root, set())
            return None
        root_ids = roots.root_ids
        own_root = self._find_nearest_root(active_project.project_root, root_ids)
        own_root_id = own_root[1] if own_root is not None else None
        resolved_root = self._find_nearest_root(os.path.realpath(path), root_ids)
        resolved_root_id = resolved_root[1] if resolved_root is not None else None
        spelled_root = None
        if os.pardir not in re.split(self._PATH_SEPARATORS_PATTERN, path):
            path = os.path.normpath(path)
            spelled_root = self._find_nearest_root(path, root_ids)

        # a path the spelling does not route runs against the active project, unless it resolves into another worktree
        if spelled_root is None or spelled_root[1] == own_root_id:
            if resolved_root_id is not None and resolved_root_id != own_root_id:
                raise WorktreeRoutingRefusal(
                    f"Refusing {path}: it resolves into the git worktree {root_ids[resolved_root_id]} through a symbolic link "
                    "or a `..` segment. Pass a path inside that worktree without either."
                )
            if not roots.is_complete:
                self._check_no_possible_nested_worktree(path, active_project.project_root, set(root_ids))
            return None
        root_spelling, root_id = spelled_root
        root = root_ids[root_id]

        # a worktree's project would be rooted at the worktree's root, which is wider than the active project's scope if
        # the active project's root is a subdirectory of its own worktree
        if own_root_id is None or own_root_id != self._FileId.of_directory(active_project.project_root):
            raise WorktreeRoutingRefusal(
                f"Refusing to route {path} to the git worktree {root}: the active project's root ({active_project.project_root}) "
                "is not the root of its git worktree."
            )

        # a routed path must resolve into the worktree its spelling selects
        if resolved_root_id != root_id:
            destination = f"the git worktree {root_ids[resolved_root_id]}" if resolved_root_id is not None else "outside the worktree"
            raise WorktreeRoutingRefusal(f"Refusing to route {path} to the git worktree {root}: a symbolic link leads to {destination}.")
        if not roots.is_complete:
            self._check_no_possible_nested_worktree(path, root, set(root_ids))

        # check that the worktree belongs to the active project's repository
        common_dir_id = self._get_git_common_dir_id(root)
        if common_dir_id is None or common_dir_id != self._get_git_common_dir_id(active_project.project_root):
            raise WorktreeRoutingRefusal(
                f"Refusing to route to the git worktree {root}: its git common directory is not the one of the active "
                f"project's repository ({active_project.project_root})."
            )

        # check, against one snapshot of the worktree's configuration, that it makes the tool available, and obtain the
        # project built with that configuration
        project_root = str(Path(root).resolve())
        try:
            project_config = self._load_project_config(project_root, active_project)
            self._check_language_backend(project_root, project_config)
            self._check_tool_availability(tool, project_root, project_config)
            project = self._get_project(project_root, root_id, project_config)
        except WorktreeRoutingRefusal:
            raise
        except Exception as e:
            raise WorktreeRoutingRefusal(
                f"Cannot route to the git worktree {project_root}: setting up its project failed ({e.__class__.__name__}: {e})"
            ) from e

        # rewrite the path relative to the worktree's root
        return WorktreeRoute(project=project, kwargs={**kwargs, "relative_path": os.path.relpath(path, root_spelling)})

    def shutdown(self, timeout: float) -> None:
        """
        Shuts down the projects of all worktrees, stopping their language servers.

        :param timeout: the timeout, in seconds, for the shutdown of each project
        """
        with self._projects_lock:
            projects = [entry.project for entry in self._entries_by_root_id.values()]
            self._entries_by_root_id.clear()
        for project in projects:
            log.info(f"Shutting down the project of the git worktree {project.project_root} ...")
            project.shutdown(timeout=timeout)

    def _is_enabled(self) -> bool:
        return (
            self._agent.serena_config.route_worktree_paths
            and self._agent.get_agent_interface().is_tools()
            and self._agent.get_language_backend().is_lsp()
        )

    @staticmethod
    def _run_git(*args: str) -> subprocess.CompletedProcess | None:
        """
        :param args: the arguments to pass to git
        :return: the completed git process or None if git cannot be run (e.g. it is not installed) or its output cannot be
            decoded (e.g. a path that is not valid UTF-8)
        """
        try:
            return subprocess_run(["git", *args])
        except (OSError, UnicodeDecodeError) as e:
            log.debug(f"Cannot run git: {e}")
            return None

    @classmethod
    def _list_worktrees(cls, repository_path: str) -> "WorktreeRouter._WorktreeListing | None":
        """
        :param repository_path: a path inside the repository
        :return: the repository's worktrees, as listed by git; None if git cannot list them (e.g. git is not installed, or
            the path is not inside a repository) or the listing cannot be parsed unambiguously
        """
        # list with NUL-terminated lines, falling back to newline-terminated ones for git versions without `-z` (< 2.36)
        line_terminator = "\0"
        result = cls._run_git("-C", repository_path, "worktree", "list", "--porcelain", "-z")
        if result is not None and result.returncode != 0:
            line_terminator = "\n"
            result = cls._run_git("-C", repository_path, "worktree", "list", "--porcelain")
        if result is None or result.returncode != 0:
            return None

        # parse the lines, entries being separated by an empty line
        worktrees = []
        root: str | None = None
        is_bare = is_prunable = False
        for line in result.stdout.split(line_terminator):
            if line == "":
                if root is not None:
                    worktrees.append(cls._Worktree(root=root, is_bare=is_bare, is_prunable=is_prunable))
                root = None
                is_bare = is_prunable = False
            elif line.startswith("worktree "):
                root = line.removeprefix("worktree ")
            elif line == "bare":
                is_bare = True
            elif line == "prunable" or line.startswith("prunable "):
                is_prunable = True
            elif line_terminator == "\n" and not line.startswith(cls._OTHER_LISTING_LINE_PREFIXES):
                # the rest of a worktree path holding a newline, which newline-terminated lines cannot represent
                return None
        return cls._WorktreeListing(worktrees=worktrees, is_complete=line_terminator == "\0")

    @classmethod
    def _get_routable_roots(cls, repository_path: str) -> "WorktreeRouter._RoutableRoots | None":
        """
        :param repository_path: a path inside the repository
        :return: the roots of the repository's accessible worktrees, bare and prunable ones excluded; None if git cannot
            list the worktrees
        """
        listing = cls._list_worktrees(repository_path)
        if listing is None:
            return None
        root_ids = {}
        for worktree in listing.worktrees:
            if worktree.is_bare or worktree.is_prunable:
                continue
            root_id = cls._FileId.of(worktree.root)
            if root_id is None:
                continue
            # a root from an incomplete listing may be the prefix of a path holding a newline, so git must confirm it
            if not listing.is_complete and cls._get_worktree_top_level_id(worktree.root) != root_id:
                continue
            root_ids[root_id] = worktree.root
        return cls._RoutableRoots(root_ids=root_ids, is_complete=listing.is_complete)

    @classmethod
    def _get_worktree_top_level_id(cls, path: str) -> Optional["WorktreeRouter._FileId"]:
        """
        :param path: a path inside a git worktree
        :return: the identity of the worktree's top-level directory or None if git cannot determine it
        """
        result = cls._run_git("-C", path, "rev-parse", "--show-toplevel")
        if result is None or result.returncode != 0:
            return None
        return cls._FileId.of(result.stdout.removesuffix("\n"))

    @classmethod
    def _find_nearest_root(cls, path: str, root_ids: dict["WorktreeRouter._FileId", str]) -> tuple[str, "WorktreeRouter._FileId"] | None:
        """
        :param path: an absolute, normalised path without `..` segments
        :param root_ids: the worktree roots, by identity
        :return: the nearest ancestor of the path (or the path itself) which is a root, as a prefix of the given path,
            along with the root's identity; None if the path lies in no root
        """
        candidate = path
        while True:
            candidate_id = cls._FileId.of_directory(candidate)
            if candidate_id is not None and candidate_id in root_ids:
                return candidate, candidate_id
            parent = os.path.dirname(candidate)
            if parent == candidate:
                return None
            candidate = parent

    @staticmethod
    def _may_be_inside_repository(path: str) -> bool:
        """
        :param path: an absolute path
        :return: whether the path may be inside a git repository, i.e. whether it or one of its ancestors holds a `.git`
            entry or the environment names a git directory; False means git cannot find a repository there
        """
        if "GIT_DIR" in os.environ:
            return True
        candidate = path
        while True:
            if os.path.lexists(os.path.join(candidate, ".git")):
                return True
            parent = os.path.dirname(candidate)
            if parent == candidate:
                return False
            candidate = parent

    @classmethod
    def _check_no_possible_nested_worktree(cls, path: str, enclosing_root: str, listed_root_ids: set["WorktreeRouter._FileId"]) -> None:
        """
        Checks, for use when the worktrees cannot be listed completely, that a path cannot lie in an unlisted worktree
        nested inside the given root, i.e. that no directory between the root and the path, as spelled or as resolved,
        holds a `.git` entry, unless it is a listed root.

        :param path: an absolute path
        :param enclosing_root: the root of the project the path would otherwise run against
        :param listed_root_ids: the identities of the worktree roots git did list
        :raises WorktreeRoutingRefusal: if the path may lie in an unlisted nested worktree
        """
        enclosing_root_id = cls._FileId.of(enclosing_root)
        for candidate_path in (os.path.normpath(path), os.path.realpath(path)):
            # walk up to the enclosing root, remembering the nearest unlisted directory holding a `.git` entry
            git_dir_holder: str | None = None
            candidate = candidate_path
            while True:
                if enclosing_root_id is not None and cls._FileId.of(candidate) == enclosing_root_id:
                    if git_dir_holder is not None:
                        raise WorktreeRoutingRefusal(
                            f"Refusing {path}: {git_dir_holder} holds a `.git` entry, so it may be a git worktree nested in "
                            f"{enclosing_root}, and git's worktree listing cannot rule that out (git is unavailable or "
                            "failed, or is older than 2.36 and cannot list worktrees unambiguously)."
                        )
                    break
                if (
                    git_dir_holder is None
                    and os.path.lexists(os.path.join(candidate, ".git"))
                    and cls._FileId.of(candidate) not in listed_root_ids
                ):
                    git_dir_holder = candidate
                parent = os.path.dirname(candidate)
                if parent == candidate:
                    break
                candidate = parent

    @classmethod
    def _get_git_common_dir_id(cls, path: str) -> Optional["WorktreeRouter._FileId"]:
        """
        :param path: a path inside a git worktree
        :return: the identity of the worktree's git common directory or None if git cannot determine it
        """
        result = cls._run_git("-C", path, "rev-parse", "--git-common-dir")
        if result is None or result.returncode != 0:
            return None
        # the directory is given relative to the one git runs in, unless it is absolute
        return cls._FileId.of(os.path.join(path, result.stdout.removesuffix("\n")))

    def _get_project(self, project_root: str, root_id: "WorktreeRouter._FileId", project_config: ProjectConfig) -> Project:
        """
        :param project_root: the worktree's root, as listed by git, with symlinks resolved
        :param root_id: the identity of the worktree's root
        :param project_config: the worktree's project configuration as currently on disk (see :meth:`_load_project_config`)
        :return: the worktree's project, created (with its language servers started) if it does not exist yet, or if the
            existing one was made for another path (e.g. before the worktree was moved), with other configuration
            settings or with ignore files whose contents have changed since, in which case the existing one is shut down
        """
        with self._projects_lock:
            entry = self._entries_by_root_id.get(root_id)
            if entry is not None and (
                entry.project.project_root != project_root
                or not entry.project.project_config.has_same_settings(project_config)
                or self._get_current_ignore_file_digests(project_root, entry.read_ignore_file_paths) != entry.ignore_file_digests
            ):
                log.info(f"Replacing the project of the git worktree {entry.project.project_root} (now at {project_root})")
                del self._entries_by_root_id[root_id]
                entry.project.shutdown()
                entry = None
            if entry is None:
                # take the state of the ignore files before building, so that a change while building is seen at the next
                # call; the files the build reads are taken as read
                ignore_file_digests = self._get_current_ignore_file_digests(project_root, ())
                log.info(f"Creating the project of the git worktree {project_root}")
                project = Project(
                    project_root=project_root,
                    project_config=project_config,
                    serena_config=self._agent.serena_config,
                    absolute_output_paths=True,
                )
                project.set_agent(self._agent)
                project.create_language_server_manager()
                read_ignore_file_digests = project.get_read_ignore_file_digests()
                ignore_file_digests.update(read_ignore_file_digests)
                entry = self._ProjectEntry(
                    project=project,
                    read_ignore_file_paths=tuple(read_ignore_file_digests),
                    ignore_file_digests=ignore_file_digests,
                )
                self._entries_by_root_id[root_id] = entry
            return entry.project

    @classmethod
    def _get_current_ignore_file_digests(cls, project_root: str, read_ignore_file_paths: tuple[str, ...]) -> dict[str, str]:
        """
        :param project_root: the worktree's root
        :param read_ignore_file_paths: the paths of the ignore files read when the project was built
        :return: the current state (see `FileStateDigest`) of each of these files and of each file listed by
            `_list_ignore_file_paths`, by path
        """
        paths = cls._list_ignore_file_paths(project_root) | set(read_ignore_file_paths)
        return {path: FileStateDigest.of_path(path) for path in sorted(paths)}

    @classmethod
    def _list_ignore_file_paths(cls, project_root: str) -> set[str]:
        """
        Lists, without walking the worktree, the ignore files beyond those read when its project was built that its
        ignore rules may depend on: every `.gitignore` git tracks in the worktree (read from the index) and the
        repository's `info/exclude`. A newly created untracked `.gitignore` is therefore taken into account only once
        the project is rebuilt.

        :param project_root: the worktree's root
        :return: the absolute paths of the files
        """
        paths = set()
        tracked = cls._run_git("-C", project_root, "ls-files", "--cached", "-z", "--", ":(glob)**/.gitignore")
        if tracked is not None and tracked.returncode == 0:
            for relative_path in tracked.stdout.split("\0"):
                if relative_path == ".gitignore" or relative_path.endswith("/.gitignore"):
                    paths.add(os.path.join(project_root, relative_path))
        exclude = cls._run_git("-C", project_root, "rev-parse", "--git-path", "info/exclude")
        if exclude is not None and exclude.returncode == 0:
            # the path is given relative to the directory git runs in, unless it is absolute
            paths.add(os.path.normpath(os.path.join(project_root, exclude.stdout.removesuffix("\n"))))
        return paths

    def _load_project_config(self, project_root: str, active_project: Project) -> ProjectConfig:
        """
        :param project_root: the worktree's root
        :param active_project: the active project
        :return: the worktree's own project configuration, loaded as for any project root (including its local
            overrides) but without writing to disk, or, if the worktree has none, a copy of the active project's
            configuration, without its language-server settings and activation command if the active project is not
            trusted
        """
        serena_config = self._agent.serena_config
        if not os.path.exists(serena_config.get_project_yml_location(project_root)):
            project_config = active_project.project_config.copy()
            if not active_project.is_trusted():
                # the settings the active project's configuration may not set must not apply elsewhere either
                project_config.ls_specific_settings = {}
                project_config.activation_command = None
            return project_config
        return ProjectConfig.load(project_root, serena_config=serena_config, save_if_incomplete=False)

    def _check_language_backend(self, project_root: str, project_config: ProjectConfig) -> None:
        """
        :param project_root: the root of the worktree's project
        :param project_config: the worktree's project configuration
        :raises WorktreeRoutingRefusal: if the configuration requires another language backend than the session's, which
            the session could not activate either
        """
        required_backend = project_config.language_backend
        session_backend = self._agent.get_language_backend()
        if required_backend is not None and required_backend != session_backend:
            raise WorktreeRoutingRefusal(
                f"Refusing to route to the git worktree {project_root}: its project configuration requires the "
                f"{required_backend} backend (language_backend), but this session uses {session_backend}."
            )

    def _check_tool_availability(self, tool: "Tool", project_root: str, project_config: ProjectConfig) -> None:
        """
        :param tool: the tool being called
        :param project_root: the root of the worktree's project
        :param project_config: the worktree's project configuration
        :raises WorktreeRoutingRefusal: if the tool would not be active upon activation of a project with this configuration
        """
        tool_name = tool.get_name()
        if tool_name in self._agent.get_active_tool_names_for_project_config(project_config):
            return
        if project_config.read_only and tool.can_edit():
            setting = "read_only"
        elif tool_name in project_config.excluded_tools:
            setting = "excluded_tools"
        else:
            setting = "excluded_tools, fixed_tools, included_optional_tools, default_modes or added_modes"
        raise WorktreeRoutingRefusal(
            f"Refusing to run '{tool_name}' in the git worktree {project_root}: its project configuration does not make the "
            f"tool available ({setting})."
        )
