# SPDX-License-Identifier: GPL-3.0-or-later

"""
The ``--follow-cwd`` code guard (integration design D1): denies the first Read, and the first Bash read or write, of a
code file that Serena has not covered yet, and names the Serena call to use instead.

Hooks run on every Read and Bash call, so this module imports nothing heavy: not ``serena_config`` (about 0.3 s) and not
``serena.router``. It finds the code-file list of a project by the path scheme of ``CodeFileList``
(``serena.router.project_facts``), which a test keeps in step.
"""

import hashlib
import os
import re
from collections.abc import Iterable, Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

import click

from serena import hooks
from serena.hooks import Hook, HookClient, PreToolUseHook, PreToolUseRemindAboutSymbolicToolsHook
from serena.util.project_root import find_project_root

ShellCommandLine = PreToolUseRemindAboutSymbolicToolsHook.ShellCommandLine

CODE_FILES_FOLDER_NAME = "follow_cwd"
CODE_FILES_SUFFIX = ".code-files"


def code_files_path(root: str) -> Path:
    digest = hashlib.sha256(root.encode()).hexdigest()[:16]
    return Path(hooks.serena_home_dir) / CODE_FILES_FOLDER_NAME / f"{digest}{CODE_FILES_SUFFIX}"


def _canonical(path: str | Path) -> str:
    return os.path.realpath(path)


class CoveredFiles:
    """
    The files Serena has covered in one session, and the files this guard has already denied once. Both sets are
    persisted as append-only files, so that hooks of parallel tool calls cannot overwrite each other's entries.
    """

    COVERED_FILE_NAME = "code_guard_covered.txt"
    DENIED_FILE_NAME = "code_guard_denied.txt"

    def __init__(self, session_dir: str | Path):
        self._covered_path = Path(session_dir) / self.COVERED_FILE_NAME
        self._denied_path = Path(session_dir) / self.DENIED_FILE_NAME
        self._covered = self._read(self._covered_path)
        self._denied = self._read(self._denied_path)

    @staticmethod
    def _read(path: Path) -> set[str]:
        try:
            return set(path.read_text().splitlines())
        except OSError:
            return set()

    @staticmethod
    def _append(path: Path, paths: Iterable[str]) -> None:
        lines = "".join(f"{p}\n" for p in paths)
        if not lines:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as file:
            file.write(lines)

    def is_covered(self, path: str) -> bool:
        return path in self._covered

    def was_denied(self, path: str) -> bool:
        return path in self._denied

    def add_covered(self, paths: Iterable[str]) -> None:
        new = set(paths) - self._covered
        self._covered |= new
        self._append(self._covered_path, sorted(new))

    def add_denied(self, paths: Iterable[str]) -> None:
        new = set(paths) - self._denied
        self._denied |= new
        self._append(self._denied_path, sorted(new))


class CodeFileGuardHook(PreToolUseHook):
    """
    PreToolUse hook on Read and Bash. A file is denied when its project has a code-file list that names the file's
    extension, Serena has not covered the file, and this is the first attempt at it.
    """

    _SHELL_WRITE_OPERATORS = frozenset((">", ">>", ">|", "&>", "&>>"))
    _SHELL_READ_OPERATOR = "<"

    _READ_ADVICE = (
        "Serena has not covered {files} yet. Read code with Serena's tools instead of {tool}:"
        " mcp__serena__get_symbols_overview for a file's structure,"
        " mcp__serena__find_symbol with include_body=true for a symbol's source,"
        " mcp__serena__find_referencing_symbols for its callers."
        " Pass your cwd, and a path relative to the project root."
        " If Serena cannot handle the file, repeat this call and it will go through."
    )
    _WRITE_ADVICE = (
        "Serena has not covered {files} yet. Edit code with Serena's tools instead of {tool}:"
        " mcp__serena__replace_symbol_body, mcp__serena__insert_after_symbol or mcp__serena__replace_content,"
        " after mcp__serena__get_symbols_overview or mcp__serena__find_symbol."
        " Pass your cwd, and a path relative to the project root."
        " If Serena cannot edit the file, repeat this call and it will go through."
    )

    def __init__(self, client: HookClient):
        super().__init__(client)
        self._cwd = str(self._input_data.get("cwd") or os.getcwd())
        self._extensions_by_root: dict[str, frozenset[str]] = {}

    def _shell_targets(self, command: str) -> tuple[list[str], list[str]]:
        """:return: the operands the command reads, and those it writes (some are not files; the caller filters)"""
        reads: list[str] = []
        writes: list[str] = []
        for invocation in ShellCommandLine(command).invocations:
            program = invocation.program
            if program == "tee":
                writes.extend(invocation.get_operands())
            elif program in PreToolUseRemindAboutSymbolicToolsHook._READ_SHELL_COMMANDS:
                if PreToolUseRemindAboutSymbolicToolsHook._is_read_invocation(invocation):
                    reads.extend(invocation.get_operands())
                else:
                    writes.extend(invocation.get_operands())
        tokens = ShellCommandLine._tokenize(command)
        for operator, target in pairwise(tokens):
            if operator in self._SHELL_WRITE_OPERATORS:
                writes.append(target)
            elif operator == self._SHELL_READ_OPERATOR:
                reads.append(target)
        return reads, writes

    def _extensions_of(self, root: str) -> frozenset[str]:
        if root not in self._extensions_by_root:
            try:
                lines = code_files_path(root).read_text().splitlines()
            except OSError:
                lines = []
            self._extensions_by_root[root] = frozenset(line.strip().lower() for line in lines if line.strip())
        return self._extensions_by_root[root]

    def _is_code_file(self, path: str) -> bool:
        suffix = Path(path).suffix.lower()
        if not suffix:
            return False
        root = find_project_root(start=Path(path).parent)
        return root is not None and suffix in self._extensions_of(root)

    def _candidates(self, operands: Iterable[str], must_exist: bool) -> list[str]:
        """:return: the canonical paths of the operands which are code files (existing ones, if ``must_exist``)"""
        paths: list[str] = []
        for operand in operands:
            operand = operand.strip()
            if not operand or operand == "-":
                continue
            path = _canonical(os.path.join(self._cwd, os.path.expanduser(operand)))
            if must_exist and not os.path.isfile(path):
                continue
            if self._is_code_file(path):
                paths.append(path)
        return list(dict.fromkeys(paths))

    def _find_targets(self) -> tuple[str, list[str], str] | None:
        """:return: the tool to name in the advice, the paths at stake, and the advice template, if there are any"""
        if self._tool_input is None:
            return None
        if self._tool_name == "read":
            file_path = self._tool_input.get("file_path")
            if isinstance(file_path, str):
                return "Read", self._candidates([file_path], must_exist=False), self._READ_ADVICE
            return None
        if self._tool_name == "bash":
            command = self._tool_input.get("command")
            if not isinstance(command, str) or not command.strip():
                return None
            reads, writes = self._shell_targets(command)
            read_paths = self._candidates(reads, must_exist=False)
            write_paths = self._candidates(writes, must_exist=True)
            if read_paths:
                return "a shell read", list(dict.fromkeys(read_paths + write_paths)), self._READ_ADVICE
            return "a shell write", write_paths, self._WRITE_ADVICE
        return None

    def execute(self) -> None:
        targets = self._find_targets()
        if targets is None or not targets[1]:
            return
        tool, paths, advice = targets
        state = CoveredFiles(self.session_persistence_dir)
        pending = [p for p in paths if not state.is_covered(p) and not state.was_denied(p)]
        if not pending:
            return
        state.add_denied(pending)
        names = ", ".join(self._display_name(p) for p in pending)
        output = self.OutputData(permission_decision="deny", permission_decision_reason=advice.format(files=names, tool=tool))
        click.echo(output.to_json_string(self._client))

    def _display_name(self, path: str) -> str:
        root = find_project_root(start=Path(path).parent)
        return os.path.relpath(path, root) if root is not None else path


class CoveredFilesRecorderHook(Hook):
    """
    PostToolUse hook on Serena's tools. Records the files the call named, and the files its result names, as covered.
    Relative paths are Serena's: relative to the project root of the call's ``cwd``, not to ``cwd`` itself.
    """

    _PATH_PATTERN = re.compile(r"[\w@+~./-]+\.\w+")

    def __init__(self, client: HookClient):
        super().__init__(client)
        self._tool_name = str(self._input_data.get("tool_name") or self._input_data.get("toolName") or "")
        raw_input = self._input_data.get("tool_input") or self._input_data.get("toolInput")
        self._tool_input: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        self._tool_response = self._input_data.get("tool_response", self._input_data.get("toolResponse"))
        self._hook_cwd = str(self._input_data.get("cwd") or os.getcwd())

    @classmethod
    def _strings_of(cls, value: Any) -> Iterator[str]:
        """The strings of a tool response, whether it is a list of content blocks, a dict (MCP result) or text."""
        if isinstance(value, str):
            yield value
        elif isinstance(value, list):
            for item in value:
                yield from cls._strings_of(item)
        elif isinstance(value, dict):
            for item in value.values():
                yield from cls._strings_of(item)

    def _input_paths(self) -> list[str]:
        paths: list[str] = []
        for key, value in self._tool_input.items():
            if key == "cwd" or key.startswith("name") or not key.endswith(("path", "paths")):
                continue
            paths.extend(self._strings_of(value))
        return paths

    def execute(self) -> None:
        if not self._tool_name.startswith("mcp__serena__"):
            return
        if isinstance(self._tool_response, dict) and self._tool_response.get("isError") is True:
            return
        cwd = self._tool_input.get("cwd")
        root = find_project_root(start=cwd if isinstance(cwd, str) and os.path.isabs(cwd) else self._hook_cwd)
        if root is None:
            return
        candidates = self._input_paths()
        for text in self._strings_of(self._tool_response):
            candidates.extend(self._PATH_PATTERN.findall(text))
        covered = {_canonical(os.path.join(root, os.path.expanduser(c))) for c in candidates if c.strip()}
        CoveredFiles(self.session_persistence_dir).add_covered(covered)
