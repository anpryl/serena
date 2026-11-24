"""
Tests of the messages that tool results carry about what a rename or a lookup actually did.
"""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from serena.agent import SerenaAgent
from serena.outcome_reports import RenameOutcome, SymbolLookupOutcome
from serena.tools import FindSymbolTool, RenameSymbolTool
from solidlsp.ls_config import LanguageServerId
from test.conftest import agent_for_project_context, get_repo_path


class TestRenameOutcome:
    @staticmethod
    def _outcome(changed_files: tuple[str, ...], n_changes: int, n_other_uses: int) -> RenameOutcome:
        return RenameOutcome("f", "g", changed_files, n_changes, declaration_file="a.js", n_other_uses=n_other_uses)

    def test_message_names_the_changed_files(self) -> None:
        message = self._outcome(("a.js", "b.js"), 2, 1).message()
        assert "a.js" in message and "b.js" in message
        assert "Warning" not in message

    def test_declaration_only_with_references_warns(self) -> None:
        message = self._outcome(("a.js",), 1, 3).message()
        assert "only the declaration was renamed" in message
        assert "rename the remaining uses by hand" in message

    def test_declaration_only_without_references_does_not_warn(self) -> None:
        assert "Warning" not in self._outcome(("a.js",), 1, 0).message()


class TestSymbolLookupOutcome:
    @pytest.mark.parametrize(
        "pattern, relative_path, n_matches, expected_hint",
        [
            ("Calculator/Add", "calc.go", 0, "try `Add`"),
            ("/Calculator/Add", "calc.go", 0, "try `Add`"),
            ("Add", "calc.go", 0, None),
            ("Calculator/Add", "calc.go", 1, None),
            ("Calculator/Add", "calc.py", 0, None),
            ("Calculator/Add", "", 0, None),
        ],
    )
    def test_go_hint(self, pattern: str, relative_path: str, n_matches: int, expected_hint: str | None) -> None:
        hint = SymbolLookupOutcome(pattern, relative_path, n_matches).hint()
        if expected_hint is None:
            assert hint == ""
        else:
            assert "Go methods are top-level symbols" in hint
            assert expected_hint in hint


@pytest.fixture
def renaming_agent(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[SerenaAgent]:
    """
    An agent on a copy of the repo of a language, to which files are added; ``request.param`` is the pair of
    the language and a mapping from relative paths to the contents of the added files.
    """
    language, added_files = request.param
    root = tmp_path / "project"
    shutil.copytree(get_repo_path(language), root)
    for name, content in added_files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content, encoding="utf-8")
    with agent_for_project_context(language, repo_root_override=str(root)) as agent:
        yield agent


@pytest.mark.python
@pytest.mark.parametrize(
    "renaming_agent",
    [
        (
            LanguageServerId.PYTHON,
            {
                "test_repo/rename_def.py": "def renamable_function() -> int:\n    return 1\n",
                "test_repo/rename_use.py": "from test_repo.rename_def import renamable_function\n\nVALUE = renamable_function()\n",
            },
        )
    ],
    indirect=True,
)
def test_rename_result_names_the_changed_files(renaming_agent: SerenaAgent) -> None:
    result = renaming_agent.get_tool(RenameSymbolTool).apply("renamable_function", "test_repo/rename_def.py", "renamed_function")
    assert "test_repo/rename_def.py" in result
    assert "test_repo/rename_use.py" in result
    assert "Warning" not in result


@pytest.mark.typescript
@pytest.mark.parametrize(
    "renaming_agent",
    [
        (
            LanguageServerId.TYPESCRIPT,
            {
                "plain_js/def.js": "export function renamable() { return 1; }\n",
                "plain_js/use.js": "import { renamable } from './def.js';\nexport const value = renamable();\n",
            },
        )
    ],
    indirect=True,
)
def test_rename_in_js_without_config_reports_declaration_only(renaming_agent: SerenaAgent) -> None:
    result = renaming_agent.get_tool(RenameSymbolTool).apply("renamable", "plain_js/def.js", "renamed")
    use_js = (Path(renaming_agent.get_active_project_or_raise().project_root) / "plain_js/use.js").read_text()
    if "renamed" in use_js:
        pytest.skip("the TypeScript server renamed the use as well; the declaration-only case did not occur")
    assert "only the declaration was renamed" in result


@pytest.mark.go
@pytest.mark.parametrize("renaming_agent", [(LanguageServerId.GO, {})], indirect=True)
def test_find_symbol_hints_at_top_level_go_methods(renaming_agent: SerenaAgent) -> None:
    find_symbol = renaming_agent.get_tool(FindSymbolTool)
    nested = find_symbol.apply(name_path_pattern="DemoStruct/Value", relative_path="main.go")
    assert "try `Value`" in nested
    assert "Go methods are top-level symbols" not in find_symbol.apply(name_path_pattern="Value", relative_path="main.go")
