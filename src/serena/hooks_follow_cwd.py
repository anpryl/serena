# SPDX-License-Identifier: GPL-3.0-or-later

"""
Hooks for Claude Code sessions that talk to Serena's ``--follow-cwd`` router. Like ``serena.hooks``, this module stays
light: the standard library and ``serena.util.project_root`` only, so a hook command starts fast.
"""

import json
import os
import re
from pathlib import Path

from serena.util.project_root import find_project_root


def _project_name(root: str) -> str:
    try:
        text = (Path(root) / ".serena" / "project.yml").read_text(encoding="utf-8")
    except OSError:
        return Path(root).name
    match = re.search(r"^project_name:\s*[\"']?([^\"'#\n]+?)[\"']?\s*(?:#.*)?$", text, re.MULTILINE)
    return match.group(1) if match else Path(root).name


def _resolved(folder: str, base: str | None) -> str:
    return folder if os.path.isabs(folder) or base is None else os.path.join(base, folder)


def check_cwd_output(hook_input: dict) -> str | None:
    """
    :param hook_input: the PreToolUse input of a Serena tool call
    :return: the JSON to print: a notice (``additionalContext`` only, never a decision) when the project of the
        call's declared ``cwd`` differs from the project of the shell's folder, otherwise None
    """
    hook_cwd = hook_input.get("cwd")
    tool_input = hook_input.get("tool_input")
    declared = tool_input.get("cwd") if isinstance(tool_input, dict) else None
    if not isinstance(hook_cwd, str) or not hook_cwd or not isinstance(declared, str) or not declared:
        return None
    declared = _resolved(declared, hook_cwd)
    hook_root = find_project_root(start=hook_cwd)
    declared_root = find_project_root(start=declared)
    if hook_root == declared_root:
        return None
    context = (
        f"Your shell is in {hook_cwd} (project {hook_root or 'none'}); "
        f"this Serena call ran with cwd {declared} (project {declared_root or 'none'})."
    )
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": context}})


def brief_subagent_output(hook_input: dict) -> str | None:
    """
    :param hook_input: the SubagentStart input
    :return: the JSON that briefs the subagent, or None if its folder is in no project
    """
    cwd = hook_input.get("cwd")
    root = find_project_root(start=cwd) if isinstance(cwd, str) and cwd else None
    if root is None:
        return None
    context = (
        f"Your working directory is {cwd} (project {_project_name(root)} at {root}). "
        "Pass it as `cwd` on every Serena call; after `cd`, the new folder. "
        "Call `initial_instructions` with this `cwd` before your first code task."
    )
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": context}})


def reorient_output(hook_input: dict) -> str | None:
    """
    :param hook_input: the SessionStart input
    :return: the plain text that points the agent back to Serena's instructions, or None if the folder is in no project
    """
    cwd = hook_input.get("cwd")
    root = find_project_root(start=cwd) if isinstance(cwd, str) and cwd else None
    if root is None:
        return None
    return (
        f"Serena: you are in project {_project_name(root)} at {root}. "
        f"Call `initial_instructions` with `cwd: {root}` before your next code task."
    )
