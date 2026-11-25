import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from serena.hooks import hook_commands
from serena.hooks_code_guard import code_files_path
from serena.router.project_facts import CodeFileList

SESSION = "guard-session"


class Project:
    def __init__(self, tmp_path: Path, extensions: list[str] | None = None):
        self.home = tmp_path / "home"
        self.root = (tmp_path / "repo").resolve()
        (self.root / ".git").mkdir(parents=True)
        (self.root / "pkg").mkdir()
        (self.root / "app.py").write_text("def create_user(): ...\n")
        (self.root / "pkg" / "service.py").write_text("x = 1\n")
        (self.root / "config.json").write_text("{}\n")
        if extensions is not None:
            CodeFileList(self.home).write(self.root, extensions)

    def run(self, command: str, payload: dict) -> dict | None:
        payload = {"session_id": SESSION, "cwd": str(self.root), **payload}
        with patch("serena.hooks.serena_home_dir", str(self.home)):
            result = CliRunner().invoke(hook_commands, [command], input=json.dumps(payload))
        assert result.exit_code == 0, result.output
        return json.loads(result.output)["hookSpecificOutput"] if result.output.strip() else None

    def read(self, path: str) -> dict | None:
        return self.run("code-guard", {"tool_name": "Read", "tool_input": {"file_path": path}})

    def bash(self, command: str, cwd: Path | None = None) -> dict | None:
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        if cwd is not None:
            payload["cwd"] = str(cwd)
        return self.run("code-guard", payload)

    def serena_call(self, tool_input: dict, tool_response: object, cwd: Path | None = None) -> None:
        payload = {"tool_name": "mcp__serena__get_symbols_overview", "tool_input": tool_input, "tool_response": tool_response}
        if cwd is not None:
            payload["cwd"] = str(cwd)
        assert self.run("code-guard-record", payload) is None


@pytest.fixture
def project(tmp_path: Path) -> Project:
    return Project(tmp_path, [".py"])


def _denied(output: dict | None) -> bool:
    return output is not None and output["permissionDecision"] == "deny"


class TestDenials:
    def test_read_of_code_file_is_denied_once_then_passes(self, project: Project):
        first = project.read(str(project.root / "app.py"))
        assert _denied(first)
        assert first is not None
        assert "get_symbols_overview" in first["permissionDecisionReason"]
        assert "app.py" in first["permissionDecisionReason"]
        assert project.read(str(project.root / "app.py")) is None

    @pytest.mark.parametrize(
        "command",
        [
            "sed -n 1,9p app.py",
            "cat app.py && echo done",
            "head -n 5 pkg/service.py",
            "cat < app.py",
            "grep -n x app.py | tail -3 && bat app.py",
        ],
    )
    def test_bash_read_is_denied(self, project: Project, command: str):
        output = project.bash(command)
        assert _denied(output)
        assert output is not None
        assert "find_symbol" in output["permissionDecisionReason"]

    @pytest.mark.parametrize("command", ["sed -i 's/1/2/' app.py", "cat > app.py <<EOF\nx\nEOF", "echo x | tee app.py", "echo x >> app.py"])
    def test_bash_write_to_existing_code_file_is_denied(self, project: Project, command: str):
        output = project.bash(command)
        assert _denied(output)
        assert output is not None
        assert "replace_symbol_body" in output["permissionDecisionReason"]

    def test_each_file_is_denied_once_per_session(self, project: Project):
        assert _denied(project.bash("cat app.py"))
        assert project.bash("cat app.py") is None
        assert _denied(project.bash("cat app.py pkg/service.py")) is True
        assert project.bash("cat pkg/service.py") is None


class TestPasses:
    @pytest.mark.parametrize("command", ["grep -rn x .", "rg create_user", "ls -la", "cat config.json", "echo hi"])
    def test_searches_and_other_files_pass(self, project: Project, command: str):
        assert project.bash(command) is None

    def test_write_creating_a_file_passes(self, project: Project):
        assert project.bash("cat > new.py <<EOF\nx = 1\nEOF") is None
        assert project.bash("echo x | tee new2.py") is None

    def test_json_file_in_a_python_project_passes(self, project: Project):
        assert project.read(str(project.root / "config.json")) is None

    def test_project_without_a_code_file_list_gets_no_guard(self, tmp_path: Path):
        project = Project(tmp_path)
        assert project.read(str(project.root / "app.py")) is None
        assert project.bash("cat app.py") is None

    def test_file_outside_every_project_passes(self, project: Project, tmp_path: Path):
        outside = tmp_path / "elsewhere" / "x.py"
        outside.parent.mkdir()
        outside.write_text("x = 1\n")
        assert project.read(str(outside)) is None

    def test_extension_match_ignores_case(self, tmp_path: Path):
        project = Project(tmp_path, [".PY"])
        assert _denied(project.read(str(project.root / "app.py")))


class TestRecording:
    def test_file_covered_from_a_subfolder_cwd_passes(self, project: Project):
        project.serena_call({"relative_path": "pkg/service.py", "cwd": str(project.root / "pkg")}, [{"type": "text", "text": "[]"}])
        assert project.read(str(project.root / "pkg" / "service.py")) is None
        assert _denied(project.read(str(project.root / "app.py")))

    def test_hook_cwd_is_used_when_the_call_has_no_cwd(self, project: Project):
        project.serena_call({"relative_path": "pkg/service.py"}, [], cwd=project.root / "pkg")
        assert project.bash("cat pkg/service.py") is None

    def test_list_shaped_response_records_result_paths(self, project: Project):
        text = json.dumps({"app.py": [{"name_path": "create_user"}]})
        project.serena_call({"name_path_pattern": "create_user", "cwd": str(project.root)}, [{"type": "text", "text": text}])
        assert project.read(str(project.root / "app.py")) is None

    def test_dict_shaped_response_records_result_paths(self, project: Project):
        text = json.dumps([{"relative_path": "pkg/service.py", "name_path": "x"}])
        project.serena_call({"cwd": str(project.root)}, {"content": [{"type": "text", "text": text}], "isError": False})
        assert project.read(str(project.root / "pkg" / "service.py")) is None

    def test_failed_call_covers_nothing(self, project: Project):
        project.serena_call({"relative_path": "app.py", "cwd": str(project.root)}, {"content": [], "isError": True})
        assert _denied(project.read(str(project.root / "app.py")))

    def test_other_tools_are_not_recorded(self, project: Project):
        project.run(
            "code-guard-record",
            {"tool_name": "Bash", "tool_input": {"relative_path": "app.py"}, "tool_response": []},
        )
        assert _denied(project.read(str(project.root / "app.py")))

    def test_absolute_input_path_is_recorded(self, project: Project):
        project.serena_call({"relative_path": str(project.root / "app.py"), "cwd": str(project.root)}, [])
        assert project.read(str(project.root / "app.py")) is None


def test_code_files_path_matches_the_router_writer(tmp_path: Path):
    root = tmp_path / "repo"
    with patch("serena.hooks.serena_home_dir", str(tmp_path / "home")):
        assert code_files_path(str(root)) == CodeFileList(tmp_path / "home").path_of(root)
