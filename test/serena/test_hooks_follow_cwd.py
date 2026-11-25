import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from serena.hooks import hook_commands

#: Claude Code shows no hook output of this many characters or more.
MAX_HOOK_OUTPUT_CHARS = 10_000


@pytest.fixture
def two_projects(tmp_path: Path) -> tuple[Path, Path]:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for project in (first, second):
        (project / "src").mkdir(parents=True)
        (project / ".git").mkdir()
    (second / ".serena").mkdir()
    (second / ".serena" / "project.yml").write_text('project_name: "other name"  # comment\n')
    return first.resolve(), second.resolve()


def _run(command: list[str], payload: dict, tmp_path: Path) -> str:
    from unittest.mock import patch

    with patch("serena.hooks.serena_home_dir", str(tmp_path / "home")):
        result = CliRunner().invoke(hook_commands, command, input=json.dumps({"session_id": "s", **payload}))
    assert result.exit_code == 0, result.output
    return result.output


class TestCheckCwd:
    def test_mismatch_gives_notice_without_decision(self, two_projects: tuple[Path, Path], tmp_path: Path) -> None:
        first, second = two_projects
        output = _run(["check-cwd"], {"cwd": str(first / "src"), "tool_input": {"cwd": str(second)}}, tmp_path)
        specific = json.loads(output)["hookSpecificOutput"]
        assert specific["hookEventName"] == "PreToolUse"
        assert f"Your shell is in {first / 'src'} (project {first})" in specific["additionalContext"]
        assert f"cwd {second} (project {second})" in specific["additionalContext"]
        assert "permissionDecision" not in output and "updatedInput" not in output

    def test_same_project_gives_nothing(self, two_projects: tuple[Path, Path], tmp_path: Path) -> None:
        first, _ = two_projects
        assert _run(["check-cwd"], {"cwd": str(first / "src"), "tool_input": {"cwd": str(first)}}, tmp_path) == ""

    def test_relative_declared_cwd_is_resolved_against_the_shell_folder(self, two_projects: tuple[Path, Path], tmp_path: Path) -> None:
        first, _ = two_projects
        assert _run(["check-cwd"], {"cwd": str(first), "tool_input": {"cwd": "src"}}, tmp_path) == ""

    @pytest.mark.parametrize("payload", [{"tool_input": {"cwd": "/x"}}, {"cwd": "/x", "tool_input": {}}, {"cwd": "/x"}])
    def test_missing_cwd_gives_nothing(self, payload: dict, tmp_path: Path) -> None:
        assert _run(["check-cwd"], payload, tmp_path) == ""


class TestPointers:
    def test_brief_subagent_names_folder_and_root(self, two_projects: tuple[Path, Path], tmp_path: Path) -> None:
        _, second = two_projects
        folder = second / "src"
        output = _run(["brief-subagent"], {"cwd": str(folder)}, tmp_path)
        specific = json.loads(output)["hookSpecificOutput"]
        assert specific["hookEventName"] == "SubagentStart"
        context = specific["additionalContext"]
        assert f"{folder} (project other name at {second})" in context
        assert "initial_instructions" in context and len(context) < MAX_HOOK_OUTPUT_CHARS

    def test_reorient_prints_plain_text_with_root(self, two_projects: tuple[Path, Path], tmp_path: Path) -> None:
        first, _ = two_projects
        output = _run(["reorient"], {"cwd": str(first / "src"), "source": "clear"}, tmp_path)
        assert len(output) < MAX_HOOK_OUTPUT_CHARS
        assert (
            output
            == f"Serena: you are in project first at {first}. Call `initial_instructions` with `cwd: {first}` before your next code task.\n"
        )

    def test_no_project_gives_nothing(self, tmp_path: Path) -> None:
        folder = tmp_path / "plain"
        folder.mkdir()
        if subprocess.run(["git", "-C", str(folder), "rev-parse"], check=False, capture_output=True).returncode == 0:
            pytest.skip("tmp_path is inside a git repository")
        for command in (["brief-subagent"], ["reorient"]):
            assert _run(command, {"cwd": str(folder)}, tmp_path) == ""


class TestActivate:
    def _text(self, options: list[str], tmp_path: Path) -> str:
        return json.loads(_run(["activate", *options], {}, tmp_path))["hookSpecificOutput"]["additionalContext"]

    def test_follow_cwd_says_pass_cwd_and_not_activate(self, tmp_path: Path) -> None:
        text = self._text(["--follow-cwd"], tmp_path)
        assert "`cwd`" in text and "activate_project" not in text

    def test_project_given_drops_activate_project(self, tmp_path: Path) -> None:
        assert "activate_project" not in self._text(["--project-given"], tmp_path)

    def test_default_keeps_activate_project(self, tmp_path: Path) -> None:
        assert "activate_project" in self._text([], tmp_path)
