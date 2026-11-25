# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HookEntry:
    """
    One hook command that ``serena setup claude-code --follow-cwd`` installs.
    """

    event: str
    matcher: str
    command: str


FOLLOW_CWD_HOOKS = (
    HookEntry("PreToolUse", "mcp__serena__.*", "serena-hooks check-cwd"),
    HookEntry("PreToolUse", "Read|Bash", "serena-hooks code-guard"),
    HookEntry("PostToolUse", "mcp__serena__.*", "serena-hooks code-guard-record"),
    HookEntry("SubagentStart", "", "serena-hooks brief-subagent"),
    HookEntry("SessionStart", "startup", "serena-hooks activate --follow-cwd"),
    HookEntry("SessionStart", "clear|compact", "serena-hooks reorient"),
)
"""
The hooks of the ``--follow-cwd`` mode. ``auto-approve`` and ``remind`` are not among them.
"""

_REPLACED_COMMAND_PREFIX = "serena-hooks activate"


class SettingsFileError(Exception):
    """
    Raised when the settings file cannot be merged into; the file is left as it was.
    """


class ClaudeCodeSettings:
    """
    The user-level ``settings.json`` of Claude Code, to which the hooks of the ``--follow-cwd`` mode are added without
    touching any other key or hook.
    """

    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def user_settings(cls) -> "ClaudeCodeSettings":
        return cls(Path.home() / ".claude" / "settings.json")

    def install_follow_cwd_hooks(self) -> list[str]:
        """
        Adds the hooks of the ``--follow-cwd`` mode, replacing an existing ``serena-hooks activate`` entry, and writes the
        file atomically if anything changed. A second call changes nothing.

        :return: a description of each change made, empty if there was none
        :raises SettingsFileError: if the file is not a JSON object or its hooks are of an unexpected shape
        """
        settings = self._read()
        hooks = settings.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise SettingsFileError(f"{self.path}: 'hooks' is not an object")
        changes = self._drop_replaced_activate_entries(hooks)
        for entry in FOLLOW_CWD_HOOKS:
            if self._add(hooks, entry):
                changes.append(f"added {entry.event} [{entry.matcher or 'all'}]: {entry.command}")
        if changes:
            self._write(settings)
        return changes

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            settings = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SettingsFileError(f"{self.path} is not valid JSON ({e}); fix it first, nothing was changed") from e
        if not isinstance(settings, dict):
            raise SettingsFileError(f"{self.path} is not a JSON object; nothing was changed")
        return settings

    def _write(self, settings: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = Path(os.path.realpath(self.path))
        mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(settings, file, indent=2, ensure_ascii=False)
                file.write("\n")
            os.chmod(temporary, mode)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def _groups(hooks: dict[str, Any], event: str) -> list[Any]:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SettingsFileError(f"'hooks.{event}' is not a list")
        return groups

    @classmethod
    def _add(cls, hooks: dict[str, Any], entry: HookEntry) -> bool:
        groups = cls._groups(hooks, entry.event)
        for group in groups:
            if isinstance(group, dict) and group.get("matcher", "") == entry.matcher:
                commands = group.setdefault("hooks", [])
                if any(isinstance(hook, dict) and hook.get("command") == entry.command for hook in commands):
                    return False
                commands.append({"type": "command", "command": entry.command})
                return True
        groups.append({"matcher": entry.matcher, "hooks": [{"type": "command", "command": entry.command}]})
        return True

    @classmethod
    def _drop_replaced_activate_entries(cls, hooks: dict[str, Any]) -> list[str]:
        """
        Removes the ``serena-hooks activate`` commands other than the one this mode installs.
        """
        wanted = next(e for e in FOLLOW_CWD_HOOKS if e.command.startswith(_REPLACED_COMMAND_PREFIX))
        changes = []
        groups = cls._groups(hooks, wanted.event)
        for group in list(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for hook in list(group["hooks"]):
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str) or not command.startswith(_REPLACED_COMMAND_PREFIX):
                    continue
                if command == wanted.command and group.get("matcher", "") == wanted.matcher:
                    continue
                group["hooks"].remove(hook)
                changes.append(f"removed {wanted.event} [{group.get('matcher') or 'all'}]: {command}")
            if not group["hooks"]:
                groups.remove(group)
        if not groups:
            del hooks[wanted.event]
        return changes
