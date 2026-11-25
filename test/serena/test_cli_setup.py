import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from serena.cli import TopLevelCommands
from serena.util.shell import ShellCommandResult

GROK_ADD_COMMAND = "grok mcp add --scope user serena -- serena start-mcp-server --context=grok --project-from-cwd"


def _result(command: str, return_code: int = 0, stdout: str = "", stderr: str = "") -> ShellCommandResult:
    return ShellCommandResult(stdout=stdout, stderr=stderr, return_code=return_code, cwd=".")


def test_setup_grok_success(monkeypatch):
    commands: list[str] = []

    def fake_execute_shell_command(command: str, capture_stderr: bool = False) -> ShellCommandResult:
        commands.append(command)
        if command == "grok --version":
            return _result(command, stdout="grok 0.2.82 (6d0b07d2de) [stable]\n")
        if command == "grok mcp add --help":
            return _result(command, stdout="Add or update an MCP server\n")
        if command == GROK_ADD_COMMAND:
            return _result(command)
        return _result(command, return_code=1)

    monkeypatch.setattr("serena.config.client_setup.execute_shell_command", fake_execute_shell_command)

    result = CliRunner().invoke(TopLevelCommands.setup, ["grok"])

    assert result.exit_code == 0, result.output
    assert "successfully set up for grok" in result.output
    assert "recommend" in result.output.lower()
    assert "030_clients.html#grok" in result.output
    assert commands == ["grok --version", "grok mcp add --help", GROK_ADD_COMMAND]


def test_setup_grok_not_applicable_exits_1(monkeypatch):
    def fake_execute_shell_command(command: str, capture_stderr: bool = False) -> ShellCommandResult:
        if command == "grok --version":
            return _result(command, return_code=1)
        return _result(command)

    monkeypatch.setattr("serena.config.client_setup.execute_shell_command", fake_execute_shell_command)

    result = CliRunner().invoke(TopLevelCommands.setup, ["grok"])

    assert result.exit_code == 1
    assert "Cannot apply setup for client 'grok'" in result.output


def test_setup_grok_apply_failure_exits_1(monkeypatch):
    def fake_execute_shell_command(command: str, capture_stderr: bool = False) -> ShellCommandResult:
        if command == "grok --version":
            return _result(command, stdout="grok 0.2.82 (6d0b07d2de) [stable]\n")
        if command == "grok mcp add --help":
            return _result(command, stdout="Add or update an MCP server\n")
        if command == GROK_ADD_COMMAND:
            return _result(command, return_code=1, stderr="boom")
        return _result(command, return_code=1)

    monkeypatch.setattr("serena.config.client_setup.execute_shell_command", fake_execute_shell_command)

    result = CliRunner().invoke(TopLevelCommands.setup, ["grok"])

    assert result.exit_code == 1
    assert "Failed to set up Serena for grok" in result.output
    assert "recommend" not in result.output.lower()
    assert "030_clients.html#grok" not in result.output


def test_init_suggests_grok_when_detected(monkeypatch):
    def fake_execute_shell_command(command: str, capture_stderr: bool = False) -> ShellCommandResult:
        if command == "grok --version":
            return _result(command, stdout="grok 0.2.82 (6d0b07d2de) [stable]\n")
        if command == "grok mcp add --help":
            return _result(command, stdout="Add or update an MCP server\n")
        return _result(command, return_code=1)

    def fake_init(**kwargs) -> SimpleNamespace:
        return SimpleNamespace(config_file_path="/tmp/serena_config.yml")

    monkeypatch.setattr("serena.config.client_setup.execute_shell_command", fake_execute_shell_command)
    monkeypatch.setattr("serena.cli.SerenaConfig.init", fake_init)

    result = CliRunner().invoke(TopLevelCommands.init, [])

    assert result.exit_code == 0, result.output
    assert "serena setup grok" in result.output
    assert "serena setup claude-code" not in result.output


STAND_IN_CLAUDE = """#!/bin/sh
echo "$@" >> "$CLAUDE_LOG"
case "$1 $2" in
  "--version "*) echo "2.1.0 (Claude Code)" ;;
  "mcp get") [ -n "$CLAUDE_HAS_SERENA" ] ;;
esac
"""
FOLLOW_CWD_ADD = "mcp add --scope user serena -- serena start-mcp-server --context=claude-code --follow-cwd"


@pytest.fixture
def stand_in_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """
    A `claude` script on PATH that records its arguments, under a temp HOME; returns the HOME and the log.
    """
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(STAND_IN_CLAUDE)
    script.chmod(0o755)
    log_file = tmp_path / "claude.log"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_LOG", str(log_file))
    monkeypatch.delenv("CLAUDE_HAS_SERENA", raising=False)
    return home, log_file


def _settings_file(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def _commands(settings: dict, event: str) -> list[tuple[str, str]]:
    return [(group["matcher"], hook["command"]) for group in settings["hooks"].get(event, []) for hook in group["hooks"]]


FOLLOW_CWD_COMMANDS = {
    "PreToolUse": [("mcp__serena__.*", "serena-hooks check-cwd"), ("Read|Bash", "serena-hooks code-guard")],
    "PostToolUse": [("mcp__serena__.*", "serena-hooks code-guard-record")],
    "SubagentStart": [("", "serena-hooks brief-subagent")],
    "SessionStart": [("startup", "serena-hooks activate --follow-cwd"), ("clear|compact", "serena-hooks reorient")],
}


def test_follow_cwd_runs_add_and_creates_the_settings_file(stand_in_claude):
    home, log_file = stand_in_claude

    result = CliRunner().invoke(TopLevelCommands.setup, ["claude-code", "--follow-cwd"])

    assert result.exit_code == 0, result.output
    assert log_file.read_text().splitlines() == ["--version", "mcp get serena", FOLLOW_CWD_ADD]
    settings = json.loads(_settings_file(home).read_text())
    assert {event: _commands(settings, event) for event in settings["hooks"]} == FOLLOW_CWD_COMMANDS
    assert "added SessionStart [startup]: serena-hooks activate --follow-cwd" in result.output


def test_follow_cwd_removes_an_existing_server_first(stand_in_claude, monkeypatch):
    _, log_file = stand_in_claude
    monkeypatch.setenv("CLAUDE_HAS_SERENA", "1")

    result = CliRunner().invoke(TopLevelCommands.setup, ["claude-code", "--follow-cwd"])

    assert result.exit_code == 0, result.output
    assert log_file.read_text().splitlines() == ["--version", "mcp get serena", "mcp remove -s user serena", FOLLOW_CWD_ADD]


def test_follow_cwd_keeps_other_keys_and_hooks(stand_in_claude):
    home, _ = stand_in_claude
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/guard.sh"}]}
    existing = {"model": "opus", "hooks": {"PreToolUse": [other], "Notification": [{"matcher": "", "hooks": [{"command": "n.sh"}]}]}}
    _settings_file(home).parent.mkdir()
    _settings_file(home).write_text(json.dumps(existing))

    result = CliRunner().invoke(TopLevelCommands.setup, ["claude-code", "--follow-cwd"])

    assert result.exit_code == 0, result.output
    settings = json.loads(_settings_file(home).read_text())
    assert settings["model"] == "opus"
    assert settings["hooks"]["Notification"] == existing["hooks"]["Notification"]
    assert settings["hooks"]["PreToolUse"][0] == other
    assert _commands(settings, "PreToolUse") == [("Bash", "~/guard.sh"), *FOLLOW_CWD_COMMANDS["PreToolUse"]]


def test_follow_cwd_replaces_todays_activate_hook_and_leaves_the_other_serena_hooks(stand_in_claude):
    home, _ = stand_in_claude
    todays = {
        "PreToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": "serena-hooks remind --client=claude-code"}]}],
        "SessionStart": [
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": "serena-hooks activate --client=claude-code"},
                    {"type": "command", "command": "other-start.sh"},
                ],
            }
        ],
    }
    _settings_file(home).parent.mkdir()
    _settings_file(home).write_text(json.dumps({"hooks": todays}))

    result = CliRunner().invoke(TopLevelCommands.setup, ["claude-code", "--follow-cwd"])

    assert result.exit_code == 0, result.output
    settings = json.loads(_settings_file(home).read_text())
    assert _commands(settings, "SessionStart") == [
        ("", "other-start.sh"),
        ("startup", "serena-hooks activate --follow-cwd"),
        ("clear|compact", "serena-hooks reorient"),
    ]
    assert ("", "serena-hooks remind --client=claude-code") in _commands(settings, "PreToolUse")
    assert "removed SessionStart [all]: serena-hooks activate --client=claude-code" in result.output


def test_a_second_follow_cwd_run_changes_nothing(stand_in_claude):
    home, _ = stand_in_claude
    CliRunner().invoke(TopLevelCommands.setup, ["claude-code", "--follow-cwd"])
    first = _settings_file(home).read_bytes()
    _settings_file(home).write_bytes(first)
    before = _settings_file(home).stat().st_mtime_ns

    result = CliRunner().invoke(TopLevelCommands.setup, ["claude-code", "--follow-cwd"])

    assert result.exit_code == 0, result.output
    assert _settings_file(home).read_bytes() == first
    assert _settings_file(home).stat().st_mtime_ns == before
    assert "no change" in result.output


def test_follow_cwd_leaves_an_invalid_settings_file_untouched_and_fails(stand_in_claude):
    home, _ = stand_in_claude
    _settings_file(home).parent.mkdir()
    _settings_file(home).write_text("{not json")

    result = CliRunner().invoke(TopLevelCommands.setup, ["claude-code", "--follow-cwd"])

    assert result.exit_code == 1
    assert _settings_file(home).read_text() == "{not json"
    assert not list(_settings_file(home).parent.glob(".settings.json.*"))


def test_without_the_option_claude_code_setup_runs_todays_command_and_writes_no_settings(stand_in_claude):
    home, log_file = stand_in_claude

    result = CliRunner().invoke(TopLevelCommands.setup, ["claude-code"])

    assert result.exit_code == 0, result.output
    assert log_file.read_text().splitlines() == [
        "--version",
        "mcp add --scope user serena -- serena start-mcp-server --context=claude-code --project-from-cwd",
    ]
    assert not _settings_file(home).exists()


def test_follow_cwd_is_refused_for_other_clients():
    result = CliRunner().invoke(TopLevelCommands.setup, ["grok", "--follow-cwd"])

    assert result.exit_code == 2
    assert "only supported for claude-code" in result.output
