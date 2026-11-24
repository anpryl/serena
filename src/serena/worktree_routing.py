# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import logging
import os
import re
import stat
import subprocess
import tempfile
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from serena.config.serena_config import ProjectConfig
from serena.project import Project
from serena.util.file_system import FileStateDigest
from solidlsp.settings import Rename, WriteBatchPlan, WriteGuard, WriteRefusal
from solidlsp.util.subprocess_util import subprocess_run

if TYPE_CHECKING:
    from serena.agent import SerenaAgent
    from serena.tools import Tool

log = logging.getLogger(__name__)


class WorktreeRoutingRefusal(Exception):
    """
    A tool call's refusal, raised when its path lies in a git worktree the call must not or cannot be routed to.
    """


class WorktreeProjectPins:
    """
    The pins on the project of a git worktree. The tasks running against the project pin it, and its shutdown, once it
    is retired (because its worktree was removed or replaced, or its project is to be rebuilt), is deferred until no
    task pins it, so that a task never loses its language servers midway. A deferred shutdown runs in a thread of its
    own, so that it neither delays nor counts against the task that triggers it.
    """

    def __init__(self, project: Project):
        """
        :param project: the worktree's project
        """
        self.project = project
        """the worktree's project"""
        self._lock = threading.Lock()
        self._pin_count = 0
        self._is_retired = False
        self._is_shutdown_taken = False
        self._has_stopped = threading.Event()

    def pin(self) -> None:
        """
        Pins the project for a task, which must unpin it (see :meth:`unpin`) when it ends.
        """
        with self._lock:
            self._pin_count += 1

    def unpin(self) -> None:
        """
        Releases a task's pin, starting the project's shutdown in the background if it is retired and no other task pins
        it.
        """
        with self._lock:
            self._pin_count -= 1
            is_shutdown_due = self._take_shutdown(self._is_retired and self._pin_count == 0)
        if is_shutdown_due:
            self._shut_down_in_background()

    def retire(self) -> None:
        """
        Retires the project, starting its shutdown in the background now if no task pins it, and otherwise once the last
        pin is released.
        """
        with self._lock:
            self._is_retired = True
            is_shutdown_due = self._take_shutdown(self._pin_count == 0)
        if is_shutdown_due:
            self._shut_down_in_background()

    def stop(self, timeout: float | None = None) -> None:
        """
        Shuts the project down regardless of pins, or, if its shutdown has begun already, waits for it to complete.

        :param timeout: the timeout, in seconds, for the shutdown (or the wait); None for the project's default timeout
            (and an unbounded wait)
        """
        with self._lock:
            self._is_retired = True
            is_shutdown_due = self._take_shutdown(True)
        if is_shutdown_due:
            self._shut_down(timeout=timeout)
        else:
            self._has_stopped.wait(timeout=timeout)

    def has_stopped(self) -> bool:
        """
        :return: whether the project's shutdown has completed
        """
        return self._has_stopped.is_set()

    def _take_shutdown(self, is_condition_met: bool) -> bool:
        """
        :param is_condition_met: whether the condition for a shutdown is met
        :return: whether the caller is to shut the project down, which is so only for the first caller whose condition
            is met; must be called with the lock held
        """
        if not is_condition_met or self._is_shutdown_taken:
            return False
        self._is_shutdown_taken = True
        return True

    def _shut_down_in_background(self) -> None:
        threading.Thread(target=self._shut_down, name=f"WorktreeProjectShutdown:{self.project.project_root}", daemon=True).start()

    def _shut_down(self, timeout: float | None = None) -> None:
        """
        Shuts the project down, logging (rather than raising) a failure.

        :param timeout: the timeout, in seconds, for the shutdown; None for the project's default
        """
        log.info(f"Shutting down the project of the git worktree {self.project.project_root} ...")
        try:
            if timeout is None:
                self.project.shutdown()
            else:
                self.project.shutdown(timeout=timeout)
        except Exception as e:
            log.error(f"Failed to shut down the project of the git worktree {self.project.project_root}: {e}", exc_info=e)
        finally:
            self._has_stopped.set()


@dataclass(frozen=True)
class WorktreeRoute:
    """
    The routing of a tool call to a git worktree of the active project's repository. The route pins the worktree's
    project until it is released (see :meth:`release`).
    """

    pins: WorktreeProjectPins
    """the pins on the project for the worktree, against which the call runs, one of them held by this route"""
    write_guard: "WorktreeRouter._WriteGuard"
    """the write guard of the worktree's project, bound to the call's task (see :meth:`WorktreeRouter._WriteGuard.bind_task`)"""
    kwargs: dict[str, Any]
    """the call's arguments, with `relative_path` rewritten relative to the worktree's root"""

    @property
    def project(self) -> Project:
        """
        :return: the project for the worktree, against which the call runs
        """
        return self.pins.project

    def release(self) -> None:
        """
        Releases the route's pin on the worktree's project and unbinds its write guard from the task, to be called, in the
        task's thread, once the task the call runs in ends.
        """
        self.write_guard.unbind_task()
        self.pins.unpin()

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
    class _GitDir:
        """
        The git directory of a worktree (e.g. `.git/worktrees/<name>` for a linked worktree), as git names it.
        """

        path: str
        """the directory's absolute path"""
        id: "WorktreeRouter._FileId"
        """the directory's identity"""

        def is_unchanged(self) -> bool:
            """
            :return: whether the directory at the path still has the identity
            """
            return WorktreeRouter._FileId.of(self.path) == self.id

    class _WriteGuard(WriteGuard):
        """
        The write guard of a worktree's project. A routed call runs against the project only while the worktree's
        identity (the inodes of its root and of its git directory) is the one the project was built for, so the identity
        a routed task starts with is the guard's. A write is permitted only while the worktree's identity is still that
        one, so that a task that outlives its call (e.g. a timed-out rename applying its edits one by one) cannot write
        into a worktree recreated at the same path, and only if its target's real path lies inside the worktree's real
        path, or, for a write to the project's Serena data folder, inside the worktree's or the data folder's real path
        as it was when the project was built (the folder's location being Serena's own configuration). A write to the
        worktree's files is moreover permitted only while the worktree's configuration, as now on disk, still makes the
        tool of the routed task making it available (e.g. not after a checkout to a branch that sets `read_only`), so
        that a task that outlives its call cannot edit against a configuration that no longer permits it. In a batch of
        writes (see :meth:`write_batch_context`), all of this is checked for every write before the first and again as
        each write is made; a write refused mid-batch for identity or location rolls nothing back (the earlier writes went
        to a checkout that is no longer the worktree, or their undoing would be refused alike) and leaves them in place,
        whereas any other refusal mid-batch (the configuration, a rename's target that holds another entry, a planned
        file that cannot be read) first has the batch's earlier writes rolled back (see :class:`_Batch`).
        The check and the write that follows it remain two steps.
        """

        @dataclass(frozen=True)
        class _EntryState:
            """
            The state of a file system entry a batch's write (or undoing) left, by which an undoing verifies that the
            entry is still exactly what the batch left (e.g. not replaced by a symbolic link).
            """

            device: int
            inode: int
            content_digest: str | None
            """the SHA-256 digest of the contents of a regular file; None for any other entry (a directory or a symbolic
            link, for instance)"""

            @classmethod
            def of(cls, path: str) -> "WorktreeRouter._WriteGuard._EntryState":
                """
                :param path: the entry's absolute path, which is not followed if it is a symbolic link
                :return: the entry's state
                :raises OSError: if the entry cannot be accessed, or is a regular file that cannot be read
                """
                stat_result = os.lstat(path)
                content_digest = None
                if stat.S_ISREG(stat_result.st_mode):
                    content_digest = hashlib.sha256(WorktreeRouter._WriteGuard._read_without_following(path)).hexdigest()
                return cls(device=stat_result.st_dev, inode=stat_result.st_ino, content_digest=content_digest)

        class _UndoStep(ABC):
            """
            The undoing of one of a batch's writes, applied only while the entry at each of its paths is still what the
            batch's own latest write or undoing left there (see :meth:`WorktreeRouter._WriteGuard._Batch.verify_left`).
            """

            @abstractmethod
            def get_paths(self) -> list[str]:
                """
                :return: the absolute paths the undoing writes to
                """

            @abstractmethod
            def undo(self, batch: "WorktreeRouter._WriteGuard._Batch") -> None:
                """
                Undoes the write, updating what the batch left at its paths.

                :param batch: the batch
                :raises Exception: if the write cannot be undone (e.g. its entries were changed since), in which case
                    nothing is written
                """

        class _ContentRestore(_UndoStep):
            """
            The restoring of a file's contents (and mode) from before the batch's first write to it at its path.
            """

            def __init__(self, path: str):
                """
                :param path: the absolute real path of the file about to be written, whose current contents and mode
                    are taken
                :raises OSError: if the file cannot be read
                """
                self._path = path
                self._original = WorktreeRouter._WriteGuard._read_without_following(path)
                self._mode = stat.S_IMODE(os.lstat(path).st_mode)

            def get_paths(self) -> list[str]:
                return [self._path]

            def undo(self, batch: "WorktreeRouter._WriteGuard._Batch") -> None:
                batch.verify_left(self._path)
                directory = os.path.dirname(self._path)
                fd, temp_path = tempfile.mkstemp(dir=directory, prefix=os.path.basename(self._path) + ".", suffix=".restore")
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(self._original)
                    os.chmod(temp_path, self._mode)
                    # verify again right before the replacement, which replaces the entry at the path itself (a symbolic
                    # link included) and never writes through a link
                    batch.verify_left(self._path)
                    os.replace(temp_path, self._path)
                except BaseException:
                    if os.path.lexists(temp_path):
                        os.remove(temp_path)
                    raise
                batch.record_left(self._path)

        class _RenameUndo(_UndoStep):
            """
            The undoing of a rename (of a file or a directory), by renaming back.
            """

            def __init__(self, rename: Rename):
                self._rename = rename

            def get_paths(self) -> list[str]:
                return [self._rename.old_path, self._rename.new_path]

            def undo(self, batch: "WorktreeRouter._WriteGuard._Batch") -> None:
                old_path, new_path = self._rename.old_path, self._rename.new_path
                if WorktreeRouter._WriteGuard._is_other_entry(old_path, new_path):
                    raise WriteRefusal(f"{old_path} is no longer free to rename {new_path} back to")
                batch.verify_left(new_path)
                os.rename(new_path, old_path)
                batch.move_left(new_path, old_path)

        class _Batch:
            """
            A batch of writes being applied. It records how to undo each of its planned writes, and one record per path
            of what its own latest write or undoing left there, which a rename moves along with the entry (a directory
            rename, every record beneath it); every undoing verifies against that record, so that the batch can be
            rolled back when a later write of it is refused (other than for identity or location), whatever the order of
            its writes and renames. Writes outside its plan (e.g. a language server's launch writes during a restart) are checked but
            not recorded, as they are not the batch's edits.
            """

            def __init__(self, plan: WriteBatchPlan) -> None:
                """
                :param plan: the writes the batch will make
                """
                self._planned_file_paths = {os.path.normpath(path) for path in plan.file_paths}
                """the batch's planned content writes, by the path they are made with (resolved only as each is made)"""
                self._planned_renames = set(plan.renames)
                self._undo_steps: list[WorktreeRouter._WriteGuard._UndoStep] = []
                self._content_restore_paths: set[str] = set()
                self._checked_real_paths: dict[str, str] = {}
                """the real path each planned content write was checked for, by the path it was checked with"""
                self._left_states: dict[str, WorktreeRouter._WriteGuard._EntryState | None] = {}
                """what the batch's latest write or undoing left at each entry (see :meth:`_get_entry_path`); None where
                the batch wrote but what it left is unknown"""

            @staticmethod
            def _get_entry_path(path: str) -> str:
                """
                :param path: an absolute path
                :return: the path of the entry at the path itself, by its parent directory's real path, not following its
                    last component
                """
                entry_path = WorktreeRouter._WriteGuard._locate_entry(path)
                return entry_path if entry_path is not None else os.path.realpath(path)

            def has_completed_writes(self) -> bool:
                """
                :return: whether the batch has completed any of its planned writes
                """
                return bool(self._left_states)

            def record_left(self, path: str, is_regular_file_expected: bool = False) -> None:
                """
                Records what the batch's write (or undoing) just made left at an entry.

                :param path: the absolute path of the entry
                :param is_regular_file_expected: whether the write leaves a regular file there (a write of a file's
                    contents); anything else found there is recorded as unknown
                """
                entry_path = self._get_entry_path(path)
                try:
                    state: WorktreeRouter._WriteGuard._EntryState | None = WorktreeRouter._WriteGuard._EntryState.of(entry_path)
                except OSError:
                    state = None
                if state is not None and is_regular_file_expected and state.content_digest is None:
                    state = None
                self._left_states[entry_path] = state

            def verify_left(self, path: str) -> None:
                """
                :param path: the absolute path of an entry
                :raises WriteRefusal: unless the entry is still what the batch's latest write or undoing left
                """
                entry_path = self._get_entry_path(path)
                left_state = self._left_states.get(entry_path)
                if left_state is None:
                    raise WriteRefusal(f"what the batch left at {path} is unknown")
                try:
                    current_state = WorktreeRouter._WriteGuard._EntryState.of(entry_path)
                except OSError as e:
                    raise WriteRefusal(f"{path} is no longer what the batch left there ({e})") from e
                if current_state != left_state:
                    raise WriteRefusal(f"{path} was changed since the batch left it")

            def move_left(self, old_path: str, new_path: str) -> None:
                """
                Moves the records at and beneath an entry along with its rename, then records the entry at its new path.

                :param old_path: the absolute path renamed
                :param new_path: the absolute path it was renamed to
                """
                old_entry_path = self._get_entry_path(old_path)
                new_entry_path = self._get_entry_path(new_path)
                for path in [p for p in self._left_states if p == old_entry_path or p.startswith(old_entry_path + os.sep)]:
                    self._left_states[new_entry_path + path[len(old_entry_path) :]] = self._left_states.pop(path)
                self.record_left(new_path)

            def record_content_write(self, path: str) -> None:
                """
                :param path: the absolute path of a file about to be written; if it is one of the batch's planned content
                    writes, the file it now resolves to (which earlier renames of the batch may have made reachable) has
                    its contents before the batch's first write to it kept
                :raises WriteRefusal: if a planned write's file cannot be read to be restored
                """
                if os.path.normpath(path) not in self._planned_file_paths:
                    return
                real_path = os.path.realpath(path)
                self._checked_real_paths[path] = real_path
                if real_path in self._content_restore_paths:
                    return
                try:
                    step = WorktreeRouter._WriteGuard._ContentRestore(real_path)
                except OSError as e:
                    raise WriteRefusal(
                        f"Refusing to write {path}: it cannot be read, to be restored if the batch is rolled back ({e})."
                    ) from e
                self._content_restore_paths.add(real_path)
                self._undo_steps.append(step)

            def record_written(self, path: str) -> None:
                """
                :param path: the absolute path of a file just written, as checked (see :meth:`record_content_write`)
                """
                real_path = self._checked_real_paths.get(path)
                if real_path is not None and real_path in self._content_restore_paths:
                    self.record_left(real_path, is_regular_file_expected=True)

            def record_rename(self, rename: Rename) -> None:
                """
                :param rename: a rename about to be made; recorded if it is one of the batch's planned renames
                """
                if rename in self._planned_renames:
                    self._undo_steps.append(WorktreeRouter._WriteGuard._RenameUndo(rename))

            def record_renamed(self, rename: Rename) -> None:
                """
                :param rename: a rename just made
                """
                if rename not in self._planned_renames:
                    return
                self.move_left(rename.old_path, rename.new_path)
                # a later write at the old path (or beneath it) writes another entry, whose contents are to be kept anew
                old_entry_path = self._get_entry_path(rename.old_path)
                self._content_restore_paths = {
                    p for p in self._content_restore_paths if not (p == old_entry_path or p.startswith(old_entry_path + os.sep))
                }

            def roll_back(self, guard: "WorktreeRouter._WriteGuard") -> list[str]:
                """
                Undoes the batch's recorded writes in reverse order, each undoing checked by the guard by identity and
                location (see :meth:`WorktreeRouter._WriteGuard.check_undo`), not by the configuration, as it undoes the
                call's own writes, and applied only while its entries are still what the batch left.

                :param guard: the guard
                :return: the paths whose undoing failed or was refused (and which are thus left changed), each being
                    logged
                """
                failed_paths: list[str] = []
                for step in reversed(self._undo_steps):
                    try:
                        for path in step.get_paths():
                            guard.check_undo(path)
                        step.undo(self)
                    except Exception as e:
                        log.error(f"Not rolling back a write to {', '.join(step.get_paths())}: {e}", exc_info=e)
                        failed_paths.extend(step.get_paths())
                self._undo_steps = []
                return failed_paths

        class _RoutedTask(threading.local):
            """
            The routed task running in the current thread against the guard's project, if any.
            """

            tool: Optional["Tool"] = None
            """the task's tool"""
            active_project: Project | None = None
            """the active project when the task was routed, whose configuration the worktree's falls back to"""
            batch: Optional["WorktreeRouter._WriteGuard._Batch"] = None
            """the batch of writes the thread is applying, if any"""

        def __init__(
            self,
            router: "WorktreeRouter",
            root: str,
            root_id: "WorktreeRouter._FileId",
            git_dir: "WorktreeRouter._GitDir",
        ):
            """
            :param router: the router, which checks a tool against the worktree's current configuration
            :param root: the worktree's root, with symlinks resolved
            :param root_id: the identity of the worktree's root
            :param git_dir: the worktree's git directory
            """
            self._router = router
            self._root = root
            self._root_id = root_id
            self._git_dir = git_dir
            self._data_folder: str | None = None
            self._routed_task = self._RoutedTask()

        def set_data_folder(self, data_folder: str) -> None:
            """
            Sets the project's Serena data folder, before the project is built, as Serena's configuration resolves it
            (the built project must name the same folder); until then, data-folder writes are permitted inside the
            worktree only.

            :param data_folder: the project's Serena data folder, with symlinks resolved
            """
            self._data_folder = data_folder

        def bind_task(self, tool: "Tool", active_project: Project) -> None:
            """
            Binds the guard, for the current thread, to a routed task, whose tool the worktree's configuration must keep
            making available for the task's writes to the worktree's files (see :meth:`unbind_task`).

            :param tool: the task's tool
            :param active_project: the active project when the task was routed
            """
            self._routed_task.tool = tool
            self._routed_task.active_project = active_project

        def unbind_task(self) -> None:
            """
            Unbinds the guard, for the current thread, from the routed task bound by :meth:`bind_task`.
            """
            self._routed_task.tool = None
            self._routed_task.active_project = None

        def check_write(self, path: str) -> None:
            batch = self._routed_task.batch
            with self._stating_earlier_writes_stay(batch):
                self._check_identity(path)
                self._check_location(path, as_entry=False)
            self._check_configuration(path)
            if batch is not None:
                with self._rolling_back_on_refusal(batch):
                    batch.record_content_write(path)

        def report_write(self, path: str) -> None:
            batch = self._routed_task.batch
            if batch is not None:
                batch.record_written(path)

        def check_rename(self, old_path: str, new_path: str) -> None:
            batch = self._routed_task.batch
            with self._stating_earlier_writes_stay(batch):
                self._check_rename_location(old_path, new_path)
            with self._rolling_back_on_refusal(batch):
                if self._is_other_entry(new_path, old_path):
                    raise WriteRefusal(f"Refusing to rename {old_path} to {new_path}: {new_path} already exists.")
            self._check_configuration(new_path)
            if batch is not None:
                batch.record_rename(Rename(old_path=old_path, new_path=new_path))

        def report_rename(self, rename: Rename) -> None:
            batch = self._routed_task.batch
            if batch is not None:
                batch.record_renamed(rename)

        def check_undo(self, path: str) -> None:
            """
            Checks a write by identity and location only, as applied to the undoing of a batch's own writes.

            The undoings act on the entries themselves (a replace or a rename), so the entry is located without following
            its last component (see :meth:`_check_location`).

            :param path: the absolute path to be written to
            :raises WriteRefusal: if the worktree was removed or replaced, or the entry does not lie inside it
            """
            self._check_identity(path)
            self._check_location(path, as_entry=True)

        def check_entry_write(self, path: str) -> None:
            with self._stating_earlier_writes_stay(self._routed_task.batch):
                self._check_identity(path)
                self._check_location(path, as_entry=True)
            self._check_configuration(path)

        def _check_location(self, path: str, as_entry: bool) -> None:
            """
            :param path: the absolute path to be written to
            :param as_entry: whether the write acts on the entry at the path itself (e.g. a rename or a removal), which
                is then located by its parent directory's real path, not following its last component; otherwise, the
                write follows the path to its real path (e.g. writing a file's contents)
            :raises WriteRefusal: if the path (or entry) does not lie inside the worktree
            """
            if not self._lies_inside(path, self._root, as_entry=as_entry):
                raise WriteRefusal(f"Refusing to write {path}: it does not lie inside the git worktree {self._root}.")

        @contextmanager
        def write_batch_context(self, plan: WriteBatchPlan) -> Iterator[None]:
            for path in plan.file_paths:
                self.check_write(path)
            self._check_planned_renames(plan.renames)
            self._routed_task.batch = self._Batch(plan)
            try:
                yield
            finally:
                self._routed_task.batch = None

        def _check_planned_renames(self, renames: list[Rename]) -> None:
            """
            Checks a batch's renames before the batch's first write, following them in order: a rename's target must be
            free, i.e. hold no entry, or hold one an earlier rename of the batch moves away, and not be taken by an
            earlier rename of the batch (which would make the rename replace an entry beyond undoing).

            :param renames: the batch's renames, in order
            :raises WriteRefusal: if a rename must not happen
            """
            vacated_paths: set[str] = set()
            taken_paths: set[str] = set()
            for rename in renames:
                self._check_rename_location(rename.old_path, rename.new_path)
                old_entry_path = self._locate_entry(rename.old_path) or rename.old_path
                new_entry_path = self._locate_entry(rename.new_path) or rename.new_path
                is_vacated = any(new_entry_path == p or new_entry_path.startswith(p + os.sep) for p in vacated_paths)
                if new_entry_path in taken_paths or (self._is_other_entry(rename.new_path, rename.old_path) and not is_vacated):
                    raise WriteRefusal(f"Refusing to rename {rename.old_path} to {rename.new_path}: {rename.new_path} already exists.")
                self._check_configuration(rename.new_path)
                vacated_paths.add(old_entry_path)
                vacated_paths.discard(new_entry_path)
                taken_paths.discard(old_entry_path)
                taken_paths.add(new_entry_path)

        @contextmanager
        def _rolling_back_on_refusal(self, batch: Optional["WorktreeRouter._WriteGuard._Batch"]) -> Iterator[None]:
            """
            Context manager for a check whose refusal in the middle of a batch rolls the batch back (any refusal other
            than for the worktree's identity or location, while the worktree is still the one the batch wrote to).

            :param batch: the batch being applied, if any
            """
            try:
                yield
            except WriteRefusal as e:
                if batch is None or not batch.has_completed_writes():
                    raise
                failed_paths = batch.roll_back(self)
                if failed_paths:
                    outcome = (
                        f"Rolling back the batch's earlier writes failed for {', '.join(failed_paths)}, which are left "
                        "changed; the others were rolled back."
                    )
                else:
                    outcome = "The batch's earlier writes were rolled back."
                raise WriteRefusal(f"{e} {outcome}") from e

        @contextmanager
        def _stating_earlier_writes_stay(self, batch: Optional["WorktreeRouter._WriteGuard._Batch"]) -> Iterator[None]:
            """
            Context manager for an identity or location check, whose refusal rolls nothing back, adding to a refusal in
            the middle of a batch that has completed writes that these stay in place.

            :param batch: the batch being applied, if any
            """
            try:
                yield
            except WriteRefusal as e:
                if batch is None or not batch.has_completed_writes():
                    raise
                raise WriteRefusal(f"{e} The batch's earlier writes stay in place; they are not undone.") from e

        def _check_rename_location(self, old_path: str, new_path: str) -> None:
            """
            :param old_path: the absolute path to be renamed
            :param new_path: the absolute path it is to be renamed to
            :raises WriteRefusal: if the rename fails the identity or location check
            """
            self.check_undo(old_path)
            self.check_undo(new_path)

        def _check_configuration(self, path: str) -> None:
            """
            :param path: the absolute path to be written to
            :raises WriteRefusal: if the worktree's configuration no longer makes the routed task's tool available (see
                :meth:`_check_tool_still_available`), after rolling back the batch being applied, if any
            """
            with self._rolling_back_on_refusal(self._routed_task.batch):
                self._check_tool_still_available(path)

        def _check_tool_still_available(self, path: str) -> None:
            tool = self._routed_task.tool
            active_project = self._routed_task.active_project
            if tool is None or active_project is None:
                return
            try:
                setting = self._router._get_current_unavailability_setting(tool, self._root, active_project)
            except Exception as e:
                raise WriteRefusal(
                    f"Refusing to write {path}: the configuration of the git worktree {self._root} cannot be read "
                    f"({e.__class__.__name__}: {e})."
                ) from e
            if setting is not None:
                raise WriteRefusal(
                    f"Refusing to write {path} for '{tool.get_name()}': the project configuration of the git worktree "
                    f"{self._root}, as now on disk, no longer makes the tool available ({setting})."
                )

        def check_data_write(self, path: str) -> None:
            self._check_data_location(path, as_entry=False)

        def check_data_entry_write(self, path: str) -> None:
            self._check_data_location(path, as_entry=True)

        def _check_data_location(self, path: str, as_entry: bool) -> None:
            """
            :param path: the absolute path of a write to the project's Serena data folder
            :param as_entry: whether the write acts on the entry at the path itself (see :meth:`_check_location`)
            :raises WriteRefusal: if the worktree was removed or replaced, or the path (or entry) lies neither inside the
                worktree nor inside the data folder
            """
            with self._stating_earlier_writes_stay(self._routed_task.batch):
                self._check_identity(path)
                is_in_data_folder = self._data_folder is not None and self._lies_inside(path, self._data_folder, as_entry=as_entry)
                if not (self._lies_inside(path, self._root, as_entry=as_entry) or is_in_data_folder):
                    raise WriteRefusal(
                        f"Refusing to write {path}: it lies neither inside the git worktree {self._root} nor inside its "
                        f"project's Serena data folder {self._data_folder}."
                    )

        def _check_identity(self, path: str) -> None:
            if WorktreeRouter._FileId.of_directory(self._root) != self._root_id or not self._git_dir.is_unchanged():
                raise WriteRefusal(
                    f"Refusing to write {path}: the git worktree {self._root} was removed or replaced since this call started."
                )

        @staticmethod
        def _read_without_following(path: str) -> bytes:
            """
            :param path: the absolute path of a file
            :return: the file's contents
            :raises OSError: if the file cannot be read, in particular if the path is a symbolic link (where the platform
                supports refusing to follow one)
            """
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                return f.read()

        @staticmethod
        def _lies_inside(path: str, directory: str, as_entry: bool = False) -> bool:
            """
            :param path: an absolute path
            :param directory: an absolute directory path, with symlinks resolved
            :param as_entry: whether to locate the entry at the path itself, by its parent directory's real path, rather
                than the path's real path (which follows its last component if that is a symbolic link)
            :return: whether the path's real path (or the entry) lies inside the directory (or is the directory); paths on
                different drives do not
            """
            located_path = WorktreeRouter._WriteGuard._locate_entry(path) if as_entry else os.path.realpath(path)
            if located_path is None:
                return False
            try:
                return os.path.commonpath([located_path, directory]) == directory
            except ValueError:
                return False

        @staticmethod
        def _locate_entry(path: str) -> str | None:
            """
            :param path: an absolute path
            :return: the path of the entry at the path itself, by its parent directory's real path, not following its last
                component; None if the last component names no entry of its own (empty, `.` or `..`)
            """
            name = os.path.basename(path)
            if name in ("", os.curdir, os.pardir):
                return None
            return os.path.join(os.path.realpath(os.path.dirname(path)), name)

        @staticmethod
        def _is_other_entry(path: str, other_path: str) -> bool:
            """
            :param path: an absolute path
            :param other_path: another absolute path
            :return: whether an entry exists at the path and is not the entry at the other path (it is the same, e.g.,
                for spellings differing in case only on a case-insensitive file system)
            """
            try:
                stat_result = os.lstat(path)
            except OSError:
                return False
            try:
                other_stat_result = os.lstat(other_path)
            except OSError:
                return True
            return (stat_result.st_dev, stat_result.st_ino) != (other_stat_result.st_dev, other_stat_result.st_ino)

    @dataclass
    class _LoadLock:
        """
        A root's load lock, along with the number of calls holding it or waiting for it.
        """

        lock: threading.Lock = field(default_factory=threading.Lock)
        user_count: int = 0

    @dataclass(frozen=True)
    class _ProjectEntry:
        """
        A cached worktree project, along with what decides whether it is still current.
        """

        pins: WorktreeProjectPins
        """the pins on the project, which defer its shutdown until no task runs against it"""
        write_guard: "WorktreeRouter._WriteGuard"
        """the project's write guard"""
        repository_id: "WorktreeRouter._FileId"
        """the identity of the git common directory of the worktree's repository"""
        git_dir: "WorktreeRouter._GitDir"
        """the worktree's git directory when the project was built"""
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
        self._load_locks_by_root_id: dict[WorktreeRouter._FileId, WorktreeRouter._LoadLock] = {}
        """the load locks of the roots for which a call is setting up a project (see :meth:`_hold_load_lock`)"""
        self._retired_pins: list[WorktreeProjectPins] = []
        """the pins on the retired projects whose shutdown may not have completed"""
        self._is_shut_down = False
        self._entries_lock = threading.Lock()
        """the lock guarding the entries, load locks, retired pins and shutdown state (held only briefly, never while a
        project is built or shut down)"""

    def route(self, tool: "Tool", kwargs: dict[str, Any]) -> WorktreeRoute | None:
        """
        Determines the routing of a tool call.

        :param tool: the tool being called
        :param kwargs: the arguments of the tool call
        :return: the call's routing (holding a pin on the worktree's project, to be released once the call's task ends)
            or None if the call runs against the active project as it is
        :raises WorktreeRoutingRefusal: if the path's spelling and its resolution select different worktrees (through a
            symbolic link or a `..` segment); if it lies in another worktree while the active project's root is not the
            root of its own worktree; if the worktrees cannot be listed completely and the path may lie in an unlisted
            worktree nested inside the active project or the worktree; if the worktree's git common directory is not
            the active project's; if git cannot determine the worktree's git directory; if the worktree's project
            configuration requires another language backend or does not make the tool available; if its project's Serena
            data folder, configured inside the worktree, is reached through a symbolic link leading out of it; if setting
            up the worktree's project fails; if the worktree's project was dropped while being set up, because another call
            found the worktree removed or replaced; or if the router has been shut down
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
        repository_id = self._get_git_common_dir_id(active_project.project_root)
        if repository_id is not None:
            self._drop_stale_entries(root_ids, repository_id)
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
        if common_dir_id is None or common_dir_id != repository_id:
            self._drop_entry(root_id)
            raise WorktreeRoutingRefusal(
                f"Refusing to route to the git worktree {root}: its git common directory is not the one of the active "
                f"project's repository ({active_project.project_root})."
            )
        # obtain the worktree's project, pinned for the call
        project_root = str(Path(root).resolve())
        try:
            entry = self._get_pinned_project(tool, root, project_root, root_id, common_dir_id, active_project)
        except WorktreeRoutingRefusal:
            raise
        except Exception as e:
            raise WorktreeRoutingRefusal(
                f"Cannot route to the git worktree {project_root}: setting up its project failed ({e.__class__.__name__}: {e})"
            ) from e

        # rewrite the path relative to the worktree's root
        entry.write_guard.bind_task(tool, active_project)
        return WorktreeRoute(
            pins=entry.pins, write_guard=entry.write_guard, kwargs={**kwargs, "relative_path": os.path.relpath(path, root_spelling)}
        )

    def shutdown(self, timeout: float) -> None:
        """
        Shuts down the projects of all worktrees, stopping their language servers, whether or not a task pins them, and
        waits for the shutdowns of retired projects that are in progress. Projects whose building completes afterwards
        are shut down at once (and their calls refused).

        :param timeout: the timeout, in seconds, for the shutdown of each project
        """
        with self._entries_lock:
            self._is_shut_down = True
            all_pins = [entry.pins for entry in self._entries_by_root_id.values()] + self._retired_pins
            self._entries_by_root_id.clear()
            self._retired_pins = []
        for pins in all_pins:
            pins.stop(timeout=timeout)

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

    @classmethod
    def _get_git_dir(cls, path: str) -> Optional["WorktreeRouter._GitDir"]:
        """
        :param path: a path inside a git worktree
        :return: the worktree's git directory or None if git cannot determine it
        """
        result = cls._run_git("-C", path, "rev-parse", "--git-dir")
        if result is None or result.returncode != 0:
            return None
        # the directory is given relative to the one git runs in, unless it is absolute
        git_dir_path = os.path.normpath(os.path.join(path, result.stdout.removesuffix("\n")))
        git_dir_id = cls._FileId.of(git_dir_path)
        if git_dir_id is None:
            return None
        return cls._GitDir(path=git_dir_path, id=git_dir_id)

    def _drop_stale_entries(self, root_ids: dict["WorktreeRouter._FileId", str], repository_id: "WorktreeRouter._FileId") -> None:
        """
        Drops the cached projects of the worktrees whose git directory changed identity, and those of the listed
        repository's worktrees that are no longer routable roots, retiring them (see :meth:`_pop_entry_for_retirement`).
        The projects of other repositories' worktrees (cached while a project of another repository was active) are not
        judged by the listing.

        :param root_ids: the roots git currently lists for the repository, by identity (see :meth:`_get_routable_roots`)
        :param repository_id: the identity of the repository's git common directory
        """
        with self._entries_lock:
            stale_root_ids = [
                root_id
                for root_id, entry in self._entries_by_root_id.items()
                if not entry.git_dir.is_unchanged() or (entry.repository_id == repository_id and root_id not in root_ids)
            ]
            stale_pins = [self._pop_entry_for_retirement(root_id) for root_id in stale_root_ids]
        for pins in stale_pins:
            log.info(f"Dropping the project of the git worktree {pins.project.project_root}: the worktree was removed or replaced")
            pins.retire()

    def _drop_entry(self, root_id: "WorktreeRouter._FileId", entry: Optional["WorktreeRouter._ProjectEntry"] = None) -> None:
        """
        Drops a worktree's cached project (if any), retiring it (see :meth:`_pop_entry_for_retirement`).

        :param root_id: the identity of the worktree's root
        :param entry: the entry to drop, if only that one is to be dropped; None to drop any entry for the root
        """
        with self._entries_lock:
            cached_entry = self._entries_by_root_id.get(root_id)
            if cached_entry is None or (entry is not None and cached_entry is not entry):
                return
            pins = self._pop_entry_for_retirement(root_id)
        pins.retire()

    def _pop_entry_for_retirement(self, root_id: "WorktreeRouter._FileId") -> WorktreeProjectPins:
        """
        Removes a worktree's cached project, keeping track of it until its shutdown has completed, so that
        :meth:`shutdown` stops it or waits for it; must be called with the entries lock held, so that a concurrent
        :meth:`shutdown` either finds the project cached or tracked. The caller then retires it (see
        :meth:`WorktreeProjectPins.retire`).

        :param root_id: the identity of the worktree's root, which must have a cached project
        :return: the pins on the project
        """
        pins = self._entries_by_root_id.pop(root_id).pins
        self._retired_pins = [retired for retired in self._retired_pins if not retired.has_stopped()]
        self._retired_pins.append(pins)
        return pins

    @contextmanager
    def _hold_load_lock(self, root_id: "WorktreeRouter._FileId") -> Iterator[None]:
        """
        Context manager for holding a root's load lock, which serialises the setting up of the root's project. The lock
        exists only while calls hold it or wait for it: it is created by the first and removed by the last, both while
        the entries lock is held, so all calls for one root identity always share one lock.

        :param root_id: the identity of the root
        """
        with self._entries_lock:
            load_lock = self._load_locks_by_root_id.setdefault(root_id, self._LoadLock())
            load_lock.user_count += 1
        try:
            with load_lock.lock:
                yield
        finally:
            with self._entries_lock:
                load_lock.user_count -= 1
                if load_lock.user_count == 0:
                    del self._load_locks_by_root_id[root_id]

    def _get_pinned_project(
        self,
        tool: "Tool",
        root: str,
        project_root: str,
        root_id: "WorktreeRouter._FileId",
        repository_id: "WorktreeRouter._FileId",
        active_project: Project,
    ) -> "WorktreeRouter._ProjectEntry":
        """
        Obtains the worktree's project, pinned for the calling task. Projects of different worktrees are set up
        concurrently, while a worktree's project is set up behind the worktree's load lock: the worktree's git directory
        and configuration are read, the tool is checked against that configuration and the project is (re)built, all
        while holding the lock, so that a call never acts on a snapshot older than the cached project's.

        :param tool: the tool being called
        :param root: the worktree's root, as listed by git
        :param project_root: the worktree's root, as listed by git, with symlinks resolved
        :param root_id: the identity of the worktree's root
        :param repository_id: the identity of the git common directory of the worktree's repository
        :param active_project: the active project
        :return: the entry of the worktree's project, whose pins hold a pin for the caller; the project is created (with its
            language servers started) if it does not exist yet, or if the existing one was made for another path (e.g.
            before the worktree was moved), for another git directory, with other configuration settings or with ignore
            files whose contents have changed since, in which case the existing one is retired
        :raises WorktreeRoutingRefusal: if the data folder's location is refused (see :meth:`_check_data_folder_location`);
            if git cannot determine the worktree's git directory; if the worktree's
            configuration requires another language backend or does not make the tool available; if the worktree's
            project was dropped concurrently, because another call found the worktree removed or replaced; or if the
            router has been shut down
        """
        with self._hold_load_lock(root_id):
            # refuse a data folder that a link leads out of the worktree, before anything is read or written through it
            self._check_data_folder_location(project_root)

            # read the worktree's git directory and configuration, and check, against this one snapshot of the
            # configuration, that it makes the tool available
            git_dir = self._get_git_dir(root)
            if git_dir is None:
                self._drop_entry(root_id)
                raise WorktreeRoutingRefusal(f"Refusing to route to the git worktree {root}: git cannot determine its git directory.")
            project_config = self._load_project_config(project_root, active_project)
            self._check_language_backend(project_root, project_config)
            self._check_tool_availability(tool, project_root, project_config)

            # replace the cached project if it is no longer current
            with self._entries_lock:
                entry = self._entries_by_root_id.get(root_id)
            if entry is not None and not self._is_current(entry, project_root, git_dir, project_config):
                log.info(f"Replacing the project of the git worktree {entry.pins.project.project_root} (now at {project_root})")
                self._drop_entry(root_id, entry)
                entry = None

            # build the project if necessary (unless the router has been shut down), publishing it unless the router has
            # been shut down meanwhile
            is_published = True
            if entry is None:
                with self._entries_lock:
                    if self._is_shut_down:
                        raise WorktreeRoutingRefusal(f"Refusing to route to the git worktree {project_root}: Serena is shutting down.")
                entry = self._create_entry(project_root, root_id, repository_id, git_dir, project_config)
                with self._entries_lock:
                    is_published = not self._is_shut_down
                    if is_published:
                        self._entries_by_root_id[root_id] = entry
            if not is_published:
                entry.pins.stop()
                raise WorktreeRoutingRefusal(f"Refusing to route to the git worktree {project_root}: Serena is shutting down.")

            # pin the project unless it was dropped meanwhile, in which case it is retired already
            with self._entries_lock:
                if self._entries_by_root_id.get(root_id) is not entry:
                    raise WorktreeRoutingRefusal(
                        f"Refusing to route to the git worktree {project_root}: it was removed or replaced, or Serena is shutting down."
                    )
                entry.pins.pin()
            return entry

    def _check_data_folder_location(self, project_root: str) -> None:
        """
        Checks the location of the worktree project's Serena data folder: a folder Serena's configuration places
        outside the worktree is accepted, but one whose configured location lies inside the worktree must not resolve
        out of it through a symbolic link.

        :param project_root: the worktree's root, with symlinks resolved
        :raises WorktreeRoutingRefusal: if the data folder's configured location lies inside the worktree and a symbolic
            link among its components leads out of the worktree
        """
        data_folder = os.path.normpath(self._agent.serena_config.get_project_serena_folder(project_root))
        if not self._is_lexically_inside(data_folder, project_root):
            return
        candidate = project_root
        for component in os.path.relpath(data_folder, project_root).split(os.sep):
            if component == os.curdir:
                continue
            candidate = os.path.join(candidate, component)
            if not os.path.islink(candidate):
                continue
            real_path = os.path.realpath(candidate)
            if not self._is_lexically_inside(real_path, project_root):
                raise WorktreeRoutingRefusal(
                    f"Refusing to route to the git worktree {project_root}: its project's Serena data folder {data_folder} "
                    f"is reached through the symbolic link {candidate}, which leads out of the worktree (to {real_path})."
                )

    @staticmethod
    def _is_lexically_inside(path: str, directory: str) -> bool:
        """
        :param path: an absolute, normalised path
        :param directory: an absolute, normalised directory path
        :return: whether the path, as spelled, lies inside the directory (or is the directory); paths on different
            drives do not
        """
        try:
            return os.path.commonpath([path, directory]) == directory
        except ValueError:
            return False

    def _is_current(
        self,
        entry: "WorktreeRouter._ProjectEntry",
        project_root: str,
        git_dir: "WorktreeRouter._GitDir",
        project_config: ProjectConfig,
    ) -> bool:
        """
        :param entry: a cached worktree project
        :param project_root: the worktree's root, as listed by git, with symlinks resolved
        :param git_dir: the worktree's git directory, as git currently names it
        :param project_config: the worktree's project configuration as currently on disk
        :return: whether the project was made for this root and git directory, with these configuration settings and the
            current contents of the ignore files
        """
        project = entry.pins.project
        return (
            project.project_root == project_root
            and entry.git_dir.id == git_dir.id
            and project.project_config.has_same_settings(project_config)
            and self._get_current_ignore_file_digests(project_root, entry.read_ignore_file_paths) == entry.ignore_file_digests
        )

    def _create_entry(
        self,
        project_root: str,
        root_id: "WorktreeRouter._FileId",
        repository_id: "WorktreeRouter._FileId",
        git_dir: "WorktreeRouter._GitDir",
        project_config: ProjectConfig,
    ) -> "WorktreeRouter._ProjectEntry":
        """
        :param project_root: the worktree's root, as listed by git, with symlinks resolved
        :param root_id: the identity of the worktree's root
        :param repository_id: the identity of the git common directory of the worktree's repository
        :param git_dir: the worktree's git directory, as git currently names it
        :param project_config: the worktree's project configuration as currently on disk
        :return: the entry of a newly built project for the worktree, with its language servers started and writes
            guarded by the worktree's current identity and location (see :class:`_WriteGuard`)
        :raises WorktreeRoutingRefusal: if the project's Serena data folder, configured inside the worktree, resolves out
            of it once the project is built (in which case no language server is started)
        """
        # take the state of the ignore files before building, so that a change while building is seen at the next call;
        # the files the build reads are taken as read
        ignore_file_digests = self._get_current_ignore_file_digests(project_root, ())
        log.info(f"Creating the project of the git worktree {project_root}")
        # the guard knows the data folder before the build, whose own writes (the data folder's .gitignore and memories
        # folder) it checks
        write_guard = self._WriteGuard(self, project_root, root_id, git_dir)
        data_folder = os.path.normpath(self._agent.serena_config.get_project_serena_folder(project_root))
        real_data_folder = os.path.realpath(data_folder)
        if self._is_lexically_inside(data_folder, project_root) and not self._is_lexically_inside(real_data_folder, project_root):
            # the folder turned into a link out of the worktree after the location check
            raise WorktreeRoutingRefusal(
                f"Refusing to route to the git worktree {project_root}: its project's Serena data folder {data_folder} "
                f"leads out of the worktree (to {real_data_folder})."
            )
        write_guard.set_data_folder(real_data_folder)
        project = Project(
            project_root=project_root,
            project_config=project_config,
            serena_config=self._agent.serena_config,
            absolute_output_paths=True,
            write_guard=write_guard,
        )
        if os.path.realpath(project.path_to_serena_data_folder()) != real_data_folder:
            # refuse before any language server is started (none has been yet, so nothing is left running)
            raise WorktreeRoutingRefusal(
                f"Refusing to route to the git worktree {project_root}: its project's Serena data folder changed while the "
                f"project was built (from {real_data_folder} to {os.path.realpath(project.path_to_serena_data_folder())})."
            )
        project.set_agent(self._agent)
        project.create_language_server_manager()
        read_ignore_file_digests = project.get_read_ignore_file_digests()
        ignore_file_digests.update(read_ignore_file_digests)
        return self._ProjectEntry(
            pins=WorktreeProjectPins(project),
            write_guard=write_guard,
            repository_id=repository_id,
            git_dir=git_dir,
            read_ignore_file_paths=tuple(read_ignore_file_digests),
            ignore_file_digests=ignore_file_digests,
        )

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

    def _get_current_unavailability_setting(self, tool: "Tool", project_root: str, active_project: Project) -> str | None:
        """
        :param tool: the tool of a routed task
        :param project_root: the root of the worktree's project
        :param active_project: the active project when the task was routed
        :return: the setting(s) by which the worktree's configuration, as now on disk (see :meth:`_load_project_config`),
            does not make the tool available, or None if it does (see :meth:`_get_unavailability_setting`)
        """
        return self._get_unavailability_setting(tool, self._load_project_config(project_root, active_project))

    def _get_unavailability_setting(self, tool: "Tool", project_config: ProjectConfig) -> str | None:
        """
        :param tool: a tool
        :param project_config: a project configuration
        :return: None if the tool would be active upon activation of a project with this configuration; otherwise the
            setting(s) that make it unavailable, as named in refusals
        """
        tool_name = tool.get_name()
        if tool_name in self._agent.get_active_tool_names_for_project_config(project_config):
            return None
        if project_config.read_only and tool.can_edit():
            return "read_only"
        if tool_name in project_config.excluded_tools:
            return "excluded_tools"
        return "excluded_tools, fixed_tools, included_optional_tools, default_modes or added_modes"

    def _check_tool_availability(self, tool: "Tool", project_root: str, project_config: ProjectConfig) -> None:
        """
        :param tool: the tool being called
        :param project_root: the root of the worktree's project
        :param project_config: the worktree's project configuration
        :raises WorktreeRoutingRefusal: if the tool would not be active upon activation of a project with this configuration
        """
        setting = self._get_unavailability_setting(tool, project_config)
        if setting is not None:
            raise WorktreeRoutingRefusal(
                f"Refusing to run '{tool.get_name()}' in the git worktree {project_root}: its project configuration does not "
                f"make the tool available ({setting})."
            )
