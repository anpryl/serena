from click.testing import CliRunner

from serena.cli import TopLevelCommands

READ_BEFORE_EDIT_CONDITION = "when reading it wouldn't need a permission prompt and the Read tool is available"


def test_claude_code_system_prompt_states_documented_read_before_edit_rule() -> None:
    result = CliRunner().invoke(TopLevelCommands(), ["print-system-prompt", "--context", "claude-code", "--only-instructions"])

    assert result.exit_code == 0, result.output
    prompt = " ".join(result.output.split())
    assert "will deny such edits" not in prompt
    assert READ_BEFORE_EDIT_CONDITION in prompt
    assert "prefer Serena's editing tools" in prompt
