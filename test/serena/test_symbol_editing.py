"""
Snapshot tests using the (awesome) syrupy pytest plugin https://github.com/syrupy-project/syrupy.
Recreate the snapshots with `pytest --snapshot-update`.
"""

import logging
import os
import shutil
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal, NamedTuple

import pytest
from overrides import overrides
from syrupy import SnapshotAssertion

from serena.code_editor import CodeEditor, LanguageServerCodeEditor
from solidlsp.ls_config import LanguageServerId
from src.serena.symbol import LanguageServerSymbolRetriever
from test.conftest import get_repo_path, language_server_tests_enabled, project_with_ls_context

pytestmark = pytest.mark.snapshot

log = logging.getLogger(__name__)


class LineChange(NamedTuple):
    """Represents a change to a specific line or range of lines."""

    operation: Literal["insert", "delete", "replace"]
    original_start: int
    original_end: int
    modified_start: int
    modified_end: int
    original_lines: list[str]
    modified_lines: list[str]


@dataclass
class CodeDiff:
    """
    Represents the difference between original and modified code.
    Provides object-oriented access to diff information including line numbers.
    """

    relative_path: str
    original_content: str
    modified_content: str
    _line_changes: list[LineChange] = field(init=False)

    def __post_init__(self) -> None:
        """Compute the diff using difflib's SequenceMatcher."""
        original_lines = self.original_content.splitlines(keepends=True)
        modified_lines = self.modified_content.splitlines(keepends=True)

        matcher = SequenceMatcher(None, original_lines, modified_lines)
        self._line_changes = []

        for tag, orig_start, orig_end, mod_start, mod_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            if tag == "insert":
                self._line_changes.append(
                    LineChange(
                        operation="insert",
                        original_start=orig_start,
                        original_end=orig_start,
                        modified_start=mod_start,
                        modified_end=mod_end,
                        original_lines=[],
                        modified_lines=modified_lines[mod_start:mod_end],
                    )
                )
            elif tag == "delete":
                self._line_changes.append(
                    LineChange(
                        operation="delete",
                        original_start=orig_start,
                        original_end=orig_end,
                        modified_start=mod_start,
                        modified_end=mod_start,
                        original_lines=original_lines[orig_start:orig_end],
                        modified_lines=[],
                    )
                )
            elif tag == "replace":
                self._line_changes.append(
                    LineChange(
                        operation="replace",
                        original_start=orig_start,
                        original_end=orig_end,
                        modified_start=mod_start,
                        modified_end=mod_end,
                        original_lines=original_lines[orig_start:orig_end],
                        modified_lines=modified_lines[mod_start:mod_end],
                    )
                )

    @property
    def line_changes(self) -> list[LineChange]:
        """Get all line changes in the diff."""
        return self._line_changes

    @property
    def has_changes(self) -> bool:
        """Check if there are any changes."""
        return len(self._line_changes) > 0

    @property
    def added_lines(self) -> list[tuple[int, str]]:
        """Get all added lines with their line numbers (0-based) in the modified file."""
        result = []
        for change in self._line_changes:
            if change.operation in ("insert", "replace"):
                for i, line in enumerate(change.modified_lines):
                    result.append((change.modified_start + i, line))
        return result

    @property
    def deleted_lines(self) -> list[tuple[int, str]]:
        """Get all deleted lines with their line numbers (0-based) in the original file."""
        result = []
        for change in self._line_changes:
            if change.operation in ("delete", "replace"):
                for i, line in enumerate(change.original_lines):
                    result.append((change.original_start + i, line))
        return result

    @property
    def modified_line_numbers(self) -> list[int]:
        """Get all line numbers (0-based) that were modified in the modified file."""
        line_nums: set[int] = set()
        for change in self._line_changes:
            if change.operation in ("insert", "replace"):
                line_nums.update(range(change.modified_start, change.modified_end))
        return sorted(line_nums)

    @property
    def affected_original_line_numbers(self) -> list[int]:
        """Get all line numbers (0-based) that were affected in the original file."""
        line_nums: set[int] = set()
        for change in self._line_changes:
            if change.operation in ("delete", "replace"):
                line_nums.update(range(change.original_start, change.original_end))
        return sorted(line_nums)

    def get_unified_diff(self, context_lines: int = 3) -> str:
        """Get the unified diff as a string."""
        import difflib

        original_lines = self.original_content.splitlines(keepends=True)
        modified_lines = self.modified_content.splitlines(keepends=True)

        diff = difflib.unified_diff(
            original_lines, modified_lines, fromfile=f"a/{self.relative_path}", tofile=f"b/{self.relative_path}", n=context_lines
        )
        return "".join(diff)

    def get_context_diff(self, context_lines: int = 3) -> str:
        """Get the context diff as a string."""
        import difflib

        original_lines = self.original_content.splitlines(keepends=True)
        modified_lines = self.modified_content.splitlines(keepends=True)

        diff = difflib.context_diff(
            original_lines, modified_lines, fromfile=f"a/{self.relative_path}", tofile=f"b/{self.relative_path}", n=context_lines
        )
        return "".join(diff)


class EditingTest(ABC):
    def __init__(self, ls_id: LanguageServerId, rel_path: str):
        """
        :param ls_id: the language server to use
        :param rel_path: the relative path of the edited file
        """
        self.rel_path = rel_path
        self.ls_id = ls_id
        self.original_repo_path = get_repo_path(ls_id)
        self.repo_path: Path | None = None

    @contextmanager
    def _setup(self) -> Iterator[LanguageServerSymbolRetriever]:
        """Context manager for setup/teardown with a temporary directory, providing the symbol manager."""
        temp_dir = Path(tempfile.mkdtemp())
        self.repo_path = temp_dir / self.original_repo_path.name
        try:
            print(f"Copying repo from {self.original_repo_path} to {self.repo_path}")
            shutil.copytree(self.original_repo_path, self.repo_path)
            # prevent deadlock on Windows due to file locks caused by antivirus or some other external software
            # wait for a long time here
            if os.name == "nt":
                time.sleep(0.1)
            log.info(f"Creating language server for {self.ls_id} {self.rel_path}")
            with project_with_ls_context(self.ls_id, str(self.repo_path)) as project:
                yield LanguageServerSymbolRetriever(project)
        finally:
            # prevent deadlock on Windows due to lingering file locks
            if os.name == "nt":
                time.sleep(0.1)
            log.info(f"Removing temp directory {temp_dir}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            log.info(f"Temp directory {temp_dir} removed")

    def _read_file(self, rel_path: str) -> str:
        """Read the content of a file in the test repository."""
        assert self.repo_path is not None
        file_path = self.repo_path / rel_path
        with open(file_path, encoding="utf-8") as f:
            return f.read()

    def run_test(self, content_after_ground_truth: SnapshotAssertion | None) -> None:
        with self._setup() as symbol_retriever:
            content_before = self._read_file(self.rel_path)
            code_editor = LanguageServerCodeEditor(symbol_retriever)
            self._apply_edit(code_editor)
            content_after = self._read_file(self.rel_path)
            code_diff = CodeDiff(self.rel_path, original_content=content_before, modified_content=content_after)
            self._test_diff(code_diff, content_after_ground_truth)

    @abstractmethod
    def _apply_edit(self, code_editor: CodeEditor) -> None:
        pass

    def _test_diff(self, code_diff: CodeDiff, snapshot: SnapshotAssertion | None) -> None:
        assert code_diff.has_changes, f"Sanity check failed: No changes detected in {code_diff.relative_path}"
        if snapshot is not None:
            assert code_diff.modified_content == snapshot


# Python test file path
PYTHON_TEST_REL_FILE_PATH = os.path.join("test_repo", "variables.py")

# TypeScript test file path
TYPESCRIPT_TEST_FILE = "index.ts"


class DeleteSymbolTest(EditingTest):
    def __init__(self, ls_id: LanguageServerId, rel_path: str, deleted_symbol: str):
        super().__init__(ls_id, rel_path)
        self.deleted_symbol = deleted_symbol
        self.rel_path = rel_path

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        code_editor.delete_symbol(self.deleted_symbol, self.rel_path)


@pytest.mark.parametrize(
    "test_case",
    [
        pytest.param(
            DeleteSymbolTest(
                LanguageServerId.PYTHON,
                PYTHON_TEST_REL_FILE_PATH,
                "VariableContainer",
            ),
            marks=pytest.mark.python,
        ),
        pytest.param(
            DeleteSymbolTest(
                LanguageServerId.TYPESCRIPT,
                TYPESCRIPT_TEST_FILE,
                "DemoClass",
            ),
            marks=pytest.mark.typescript,
        ),
    ],
)
def test_delete_symbol(test_case, snapshot: SnapshotAssertion):
    test_case.run_test(content_after_ground_truth=snapshot)


NEW_PYTHON_FUNCTION = """def new_inserted_function():
    print("This is a new function inserted before another.")"""

NEW_PYTHON_CLASS_WITH_LEADING_NEWLINES = """

class NewInsertedClass:
    pass
"""

NEW_PYTHON_CLASS_WITH_TRAILING_NEWLINES = """class NewInsertedClass:
    pass


"""

NEW_TYPESCRIPT_FUNCTION = """function newInsertedFunction(): void {
    console.log("This is a new function inserted before another.");
}"""


NEW_PYTHON_VARIABLE = 'new_module_var = "Inserted after typed_module_var"'

NEW_TYPESCRIPT_FUNCTION_AFTER = """function newFunctionAfterClass(): void {
    console.log("This function is after DemoClass.");
}"""


class InsertInRelToSymbolTest(EditingTest):
    def __init__(
        self, ls_id: LanguageServerId, rel_path: str, symbol_name: str, new_content: str, mode: Literal["before", "after"] | None = None
    ):
        super().__init__(ls_id, rel_path)
        self.symbol_name = symbol_name
        self.new_content = new_content
        self.mode: Literal["before", "after"] | None = mode

    def set_mode(self, mode: Literal["before", "after"]):
        self.mode = mode

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        assert self.mode is not None
        if self.mode == "before":
            code_editor.insert_before_symbol(self.symbol_name, self.rel_path, self.new_content)
        elif self.mode == "after":
            code_editor.insert_after_symbol(self.symbol_name, self.rel_path, self.new_content)


@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.parametrize(
    "test_case",
    [
        pytest.param(
            InsertInRelToSymbolTest(
                LanguageServerId.PYTHON,
                PYTHON_TEST_REL_FILE_PATH,
                "use_module_variables",
                NEW_PYTHON_FUNCTION,
            ),
            marks=pytest.mark.python,
        ),
        pytest.param(
            InsertInRelToSymbolTest(
                LanguageServerId.TYPESCRIPT,
                TYPESCRIPT_TEST_FILE,
                "DemoClass",
                NEW_TYPESCRIPT_FUNCTION_AFTER,
            ),
            marks=pytest.mark.typescript,
        ),
        pytest.param(
            InsertInRelToSymbolTest(
                LanguageServerId.TYPESCRIPT,
                TYPESCRIPT_TEST_FILE,
                "helperFunction",
                NEW_TYPESCRIPT_FUNCTION,
            ),
            marks=pytest.mark.typescript,
        ),
    ],
)
def test_insert_in_rel_to_symbol(test_case: InsertInRelToSymbolTest, mode: Literal["before", "after"], snapshot: SnapshotAssertion):
    test_case.set_mode(mode)
    test_case.run_test(content_after_ground_truth=snapshot)


@pytest.mark.python
def test_insert_python_class_before(snapshot: SnapshotAssertion):
    InsertInRelToSymbolTest(
        LanguageServerId.PYTHON,
        PYTHON_TEST_REL_FILE_PATH,
        "VariableDataclass",
        NEW_PYTHON_CLASS_WITH_TRAILING_NEWLINES,
        mode="before",
    ).run_test(snapshot)


@pytest.mark.python
def test_insert_python_class_after(snapshot: SnapshotAssertion):
    InsertInRelToSymbolTest(
        LanguageServerId.PYTHON,
        PYTHON_TEST_REL_FILE_PATH,
        "VariableDataclass",
        NEW_PYTHON_CLASS_WITH_LEADING_NEWLINES,
        mode="after",
    ).run_test(snapshot)


PYTHON_REPLACED_BODY = """def modify_instance_var(self):
        # This body has been replaced
        self.instance_var = "Replaced!"
        self.reassignable_instance_var = 999
"""

TYPESCRIPT_REPLACED_BODY = """function printValue() {
        // This body has been replaced
        console.warn("New value: " + this.value);
    }
"""


class ReplaceBodyTest(EditingTest):
    def __init__(self, ls_id: LanguageServerId, rel_path: str, symbol_name: str, new_body: str):
        super().__init__(ls_id, rel_path)
        self.symbol_name = symbol_name
        self.new_body = new_body

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        code_editor.replace_body(self.symbol_name, self.rel_path, self.new_body)


@pytest.mark.parametrize(
    "test_case",
    [
        pytest.param(
            ReplaceBodyTest(
                LanguageServerId.PYTHON,
                PYTHON_TEST_REL_FILE_PATH,
                "VariableContainer/modify_instance_var",
                PYTHON_REPLACED_BODY,
            ),
            marks=pytest.mark.python,
        ),
        pytest.param(
            ReplaceBodyTest(
                LanguageServerId.TYPESCRIPT,
                TYPESCRIPT_TEST_FILE,
                "DemoClass/printValue",
                TYPESCRIPT_REPLACED_BODY,
            ),
            marks=pytest.mark.typescript,
        ),
    ],
)
def test_replace_body(test_case: ReplaceBodyTest, snapshot: SnapshotAssertion):
    # assert "a" in snapshot
    test_case.run_test(content_after_ground_truth=snapshot)


NIX_ATTR_REPLACEMENT = """c = 3;"""


class NixAttrReplacementTest(EditingTest):
    """Test for replacing individual attributes in Nix that should NOT result in double semicolons."""

    def __init__(self, ls_id: LanguageServerId, rel_path: str, symbol_name: str, new_body: str):
        super().__init__(ls_id, rel_path)
        self.symbol_name = symbol_name
        self.new_body = new_body

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        code_editor.replace_body(self.symbol_name, self.rel_path, self.new_body)


@pytest.mark.nix
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.NIX), reason="Nix tests are disabled (nixd not available)")
def test_nix_symbol_replacement_no_double_semicolon(snapshot: SnapshotAssertion):
    """
    Test that replacing a Nix attribute does not result in double semicolons.

    This test exercises the bug where:
    - Original: users.users.example = { isSystemUser = true; group = "example"; description = "Example service user"; };
    - Replacement: c = 3;
    - Bug result would be: c = 3;; (double semicolon)
    - Correct result should be: c = 3; (single semicolon)

    The replacement body includes a semicolon, but the language server's range extension
    logic should prevent double semicolons.
    """
    test_case = NixAttrReplacementTest(
        LanguageServerId.NIX,
        "default.nix",
        "testUser",  # Simple attrset with multiple key-value pairs
        NIX_ATTR_REPLACEMENT,
    )
    test_case.run_test(content_after_ground_truth=snapshot)


class NixBindingEditTest(EditingTest):
    """Replaces the bodies of Nix bindings and checks the file for exactly the expected text replacements."""

    def __init__(self, rel_path: str, edits: list[tuple[str, str | None]], replacements: list[tuple[str, str]]):
        """
        :param rel_path: the relative path of the edited file
        :param edits: (name path, new body) per binding; a new body of None writes back the body as shown
        :param replacements: the (old text, new text) replacements the edits must amount to
        """
        super().__init__(LanguageServerId.NIX, rel_path)
        self.edits = edits
        self.replacements = replacements

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        for name_path, new_body in self.edits:
            if new_body is None:
                new_body = code_editor._find_unique_symbol(name_path, self.rel_path).body
                assert new_body is not None
            code_editor.replace_body(name_path, self.rel_path, new_body)

    @overrides
    def _test_diff(self, code_diff: CodeDiff, snapshot: SnapshotAssertion | None) -> None:
        expected = code_diff.original_content
        for old_text, new_text in self.replacements:
            assert expected.count(old_text) == 1, f"{old_text!r} should occur exactly once in the file"
            expected = expected.replace(old_text, new_text)
        assert code_diff.modified_content == expected


@pytest.mark.nix
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.NIX), reason="Nix tests are disabled (nixd not available)")
def test_nix_binding_body_written_back_unchanged():
    """
    Writing back each binding's body exactly as find_symbol shows it must leave the file unchanged.
    It used to delete the semicolon: the replaced range included it, but the shown body did not.
    """
    name_paths = [
        "plain",
        "quoted-name",
        "split",
        "commented",
        "services/web/hosts/web-host",
        "lambda",
        "formals",
        "formals/b",
        "atFormals",
        "compare",
        "lineComment",
        "blockComment",
        "tight",
        "splitValue",
        "nextLine",
    ]
    NixBindingEditTest("bindings.nix", [(name, None) for name in name_paths], replacements=[]).run_test(None)


@pytest.mark.nix
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.NIX), reason="Nix tests are disabled (nixd not available)")
def test_nix_binding_after_an_astral_character_written_back_unchanged():
    """
    Serena applies a column as a string index, so the semicolon a binding owns is found the same way: converted
    from UTF-16 alone, a write-back after an astral character also deleted the character after the semicolon.
    """
    # not `inline/y`: a symbol that starts after an astral character on its line is shifted by Serena's column
    # handling for every language, whatever its semicolon
    name_paths = ["astral", "next", "inline", "inline/x", "last", "last/z"]
    NixBindingEditTest("astral.nix", [(name, None) for name in name_paths], replacements=[]).run_test(None)


@pytest.mark.nix
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.NIX), reason="Nix tests are disabled (nixd not available)")
def test_nix_binding_replaced_in_shown_shape():
    """A new body in the shape find_symbol shows replaces exactly the binding, with neither a lost nor a doubled `;`."""
    edits = [
        ("quoted-name", '"quoted-name" = 2;', '"quoted-name" = 20;'),
        ("split", "split\n    = 3;", "split = 30;"),
        ("lambda", "lambda = x: x;", "lambda = y: y;"),
    ]
    NixBindingEditTest(
        "bindings.nix",
        [(name, new_body) for name, _, new_body in edits],
        replacements=[(old_body, new_body) for _, old_body, new_body in edits],
    ).run_test(None)


@pytest.mark.nix
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.NIX), reason="Nix tests are disabled (nixd not available)")
def test_nix_dotted_key_written_back_and_replaced():
    """Each segment of a path in one statement is written back unchanged, and replaced in the shape shown."""
    name_paths = ["single/path", "single/path/only", "spread", "spread/over", "services/web/enable", "piece/a", "whole/inner"]
    NixBindingEditTest("dotted.nix", [(name, None) for name in name_paths], replacements=[]).run_test(None)
    NixBindingEditTest(
        "dotted.nix",
        [("single", "single.path.only = 10;"), ("piece/b", "b = 30;")],
        replacements=[("single.path.only = 1;", "single.path.only = 10;"), ("piece.b = 3;", "piece.b = 30;")],
    ).run_test(None)


class NixRefusedEditTest(EditingTest):
    """An edit the editor must refuse, leaving the file unchanged."""

    def __init__(self, rel_path: str, edit: Callable[[CodeEditor], None], reason: str):
        super().__init__(LanguageServerId.NIX, rel_path)
        self.edit = edit
        self.reason = reason

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        with pytest.raises(ValueError, match=self.reason):
            self.edit(code_editor)

    @overrides
    def _test_diff(self, code_diff: CodeDiff, snapshot: SnapshotAssertion | None) -> None:
        assert not code_diff.has_changes


@pytest.mark.nix
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.NIX), reason="Nix tests are disabled (nixd not available)")
@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        (lambda editor: editor.replace_body("services", "dotted.nix", "services.web.enable = false;"), "more than one statement"),
        (lambda editor: editor.replace_body("services/web", "dotted.nix", "web.enable = false;"), "more than one statement"),
        (lambda editor: editor.replace_body("piece", "dotted.nix", "piece.a = 10;"), "more than one statement"),
        (lambda editor: editor.replace_body("whole", "dotted.nix", "whole = { inner = 10; };"), "more than one statement"),
        (lambda editor: editor.replace_body("outer", "dotted.nix", "outer = 0;"), "more than one statement"),
        (lambda editor: editor.delete_symbol("piece", "dotted.nix"), "more than one statement"),
        # deleting `path` of `single.path.only = 1;` left `single.` to take the next binding
        (lambda editor: editor.delete_symbol("single/path", "dotted.nix"), "continues the attribute path"),
        (lambda editor: editor.delete_symbol("piece/a", "dotted.nix"), "continues the attribute path"),
        # `lines` of `spread.over` + newline + `.lines = 2;` is on the path's second line
        (lambda editor: editor.insert_before_symbol("spread/over/lines", "dotted.nix", "added = 1;"), "continues an attribute path"),
        # `leaf` shares its line with `across`, but the path begins at `broken.` on the line above
        (lambda editor: editor.insert_before_symbol("broken/across/leaf", "dotted.nix", "added = 1;"), "continues an attribute path"),
        # `added` of `late.` + newline + `added = 1;` begins its line, inside the path; deleting it left `late.`
        (lambda editor: editor.insert_before_symbol("late/added", "dotted.nix", "x = 1;"), "continues an attribute path"),
        (lambda editor: editor.delete_symbol("late/added", "dotted.nix"), "continues the attribute path"),
        (lambda editor: editor.replace_body("late", "dotted.nix", "late = 0;"), "more than one statement"),
        # after `first.b = 1;` comes `other = {` on the same line, so the next line is inside `other`
        (lambda editor: editor.insert_after_symbol("first", "dotted.nix", "x = 1;"), "does not end its line"),
        (lambda editor: editor.insert_before_symbol("other", "dotted.nix", "x = 1;"), "does not begin its line"),
        (lambda editor: editor.insert_after_symbol("inline/member", "dotted.nix", "x = 1;"), "does not end its line"),
        (lambda editor: editor.insert_before_symbol("inline/member", "dotted.nix", "x = 1;"), "does not begin its line"),
        # `/* first */ commentOther = { /* second */` is no comment: a comment ends at its own `*/`
        (lambda editor: editor.insert_after_symbol("commentA", "dotted.nix", "x = 1;"), "does not end its line"),
    ],
)
def test_nix_edit_that_would_break_an_attribute_path_is_refused(edit: Callable[[CodeEditor], None], reason: str):
    """
    Statements that share a prefix are one attribute in nixd's tree: `piece` covers only its name, and `whole` only
    its first statement. Replacing either wrote over part of a statement, or left `whole.extra` to clash.
    """
    NixRefusedEditTest("dotted.nix", edit, reason).run_test(None)


@pytest.mark.nix
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.NIX), reason="Nix tests are disabled (nixd not available)")
@pytest.mark.parametrize(
    ("edit", "reason"),
    [
        # deleting the lambda of `lambda = x: x;` left `lambda = ;`
        (lambda editor: editor.delete_symbol("lambda/x", "bindings.nix"), "not a whole binding"),
        # `w: w;` begins its line, but code before it went into `wrapped =`
        (lambda editor: editor.insert_before_symbol("wrapped/w", "bindings.nix", "added = 1;"), "not a whole binding"),
        (lambda editor: editor.insert_before_symbol("toString", "bindings.nix", "added = 1;"), "not a whole binding"),
        # `nextLine = 1` ends its line, but its `;` is on the next one
        (lambda editor: editor.insert_after_symbol("nextLine", "bindings.nix", "added = 1;"), "does not end with the `;`"),
    ],
)
def test_nix_part_of_a_binding_is_not_edited_alone(edit: Callable[[CodeEditor], None], reason: str):
    NixRefusedEditTest("bindings.nix", edit, reason).run_test(None)


@pytest.mark.nix
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.NIX), reason="Nix tests are disabled (nixd not available)")
@pytest.mark.parametrize(
    "edit",
    [
        # four characters outside the Basic Multilingual Plane before its end: the editor's range took `afterFamily`'s
        # binding too
        lambda editor: editor.replace_body("family", "astral.nix", 'family = "x";'),
        lambda editor: editor.delete_symbol("afterFamily", "astral.nix"),
        lambda editor: editor.replace_body("inline/y", "astral.nix", "y = 3;"),
        # a segment's range ends where its binding's does, past the same characters
        lambda editor: editor.replace_body("emojiPath", "astral.nix", 'emojiPath.key = "x";'),
    ],
)
def test_nix_symbol_after_an_astral_character_is_not_edited_whole(edit: Callable[[CodeEditor], None]):
    NixRefusedEditTest("astral.nix", edit, "outside the Basic Multilingual Plane").run_test(None)


class NixInsertTest(EditingTest):
    def __init__(self, name_path: str, before: bool, after_text: str):
        super().__init__(LanguageServerId.NIX, "dotted.nix")
        self.name_path, self.before, self.after_text = name_path, before, after_text

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        insert = code_editor.insert_before_symbol if self.before else code_editor.insert_after_symbol
        insert(self.name_path, self.rel_path, "  added = 1;")

    @overrides
    def _test_diff(self, code_diff: CodeDiff, snapshot: SnapshotAssertion | None) -> None:
        assert code_diff.modified_content == code_diff.original_content.replace(self.after_text, self.after_text + "  added = 1;\n")


@pytest.mark.nix
@pytest.mark.skipif(not language_server_tests_enabled(LanguageServerId.NIX), reason="Nix tests are disabled (nixd not available)")
@pytest.mark.parametrize(
    ("name_path", "before", "after_text"),
    [
        ("services", True, "    .lines = 2;\n"),
        ("single/path", False, "  single.path.only = 1;\n"),
        ("other", False, "other = {\n    child = 2;\n  };\n"),
    ],
)
def test_nix_insert_next_to_an_attribute_path(name_path: str, before: bool, after_text: str):
    """Inserting before a path's first segment, or after any segment of a statement ending its line, goes between statements."""
    NixInsertTest(name_path, before, after_text).run_test(None)


# A single `type` declaration body that re-supplies the leading `type` keyword, as a user would
# after copying the body returned by find_symbol. Replacing the body with this must not duplicate
# the keyword.
GO_DECL_REPLACEMENT = """type NamedInt int64"""


class GoDeclReplacementTest(EditingTest):
    """Test for replacing a single Go ``type``/``var``/``const`` declaration that should NOT result
    in a duplicated leading keyword (e.g. ``type type NamedInt``).
    """

    def __init__(self, ls_id: LanguageServerId, rel_path: str, symbol_name: str, new_body: str):
        super().__init__(ls_id, rel_path)
        self.symbol_name = symbol_name
        self.new_body = new_body

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        code_editor.replace_body(self.symbol_name, self.rel_path, self.new_body)

    @overrides
    def _test_diff(self, code_diff: CodeDiff, snapshot: SnapshotAssertion | None) -> None:
        # the leading declaration keyword must appear exactly once (no `type type` corruption)
        for keyword in ("type", "var", "const"):
            assert f"{keyword} {keyword} " not in code_diff.modified_content, (
                f"Replacement duplicated the '{keyword}' keyword: {code_diff.modified_content!r}"
            )
        return super()._test_diff(code_diff, snapshot)


@pytest.mark.go
@pytest.mark.skipif(sys.platform == "win32", reason="gopls is not provisioned on the Windows CI runner")
def test_go_symbol_replacement_no_double_keyword(snapshot: SnapshotAssertion):
    """
    Test that replacing a Go ``type``/``var``/``const`` declaration does not duplicate the leading
    keyword.

    This exercises the bug where:
    - Original: type NamedInt int
    - Replacement body (as displayed by find_symbol): type NamedInt int64
    - Bug result would be: type type NamedInt int64 (the original `type ` prefix is kept because the
      gopls symbol range starts at the identifier, and the supplied body adds another `type`)
    - Correct result should be: type NamedInt int64

    gopls reports the symbol range of such declarations starting at the identifier (after the
    keyword), unlike ``func`` declarations whose range includes the ``func`` keyword. The gopls
    range extension prevents the duplicated keyword.
    """
    test_case = GoDeclReplacementTest(
        LanguageServerId.GO,
        "symbol_body.go",
        "NamedInt",
        GO_DECL_REPLACEMENT,
    )
    test_case.run_test(content_after_ground_truth=snapshot)


# A single `export const` declaration body that re-supplies the leading `export const` prefix, as a
# user would after copying the body returned by find_symbol. Replacing the body with this must not
# duplicate the prefix.
TS_DECL_REPLACEMENT = """export const twice = (n: number): number => n * 3;"""


class TsDeclReplacementTest(EditingTest):
    """Test for replacing a single TypeScript `const`/`let`/`var` declaration that should NOT
    result in a duplicated leading `export`/keyword (e.g. `export const export const twice = ...`)
    or a duplicated trailing semicolon (e.g. `... n * 3;;`).
    """

    def __init__(self, ls_id: LanguageServerId, rel_path: str, symbol_name: str, new_body: str):
        super().__init__(ls_id, rel_path)
        self.symbol_name = symbol_name
        self.new_body = new_body

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        code_editor.replace_body(self.symbol_name, self.rel_path, self.new_body)

    @overrides
    def _test_diff(self, code_diff: CodeDiff, snapshot: SnapshotAssertion | None) -> None:
        # the leading `export`/declaration keyword must appear exactly once (no `export const
        # export const` corruption); this doubles as a syntax check, since a duplicated prefix is
        # `export`/`const`/`let`/`var` immediately followed by whitespace and the same word again
        for keyword in ("export const", "const", "let", "var"):
            assert f"{keyword} {keyword} " not in code_diff.modified_content, (
                f"Replacement duplicated the '{keyword}' prefix: {code_diff.modified_content!r}"
            )
        # the trailing semicolon must appear exactly once, not doubled up
        assert ";;" not in code_diff.modified_content, f"Replacement duplicated the trailing semicolon: {code_diff.modified_content!r}"
        return super()._test_diff(code_diff, snapshot)


@pytest.mark.typescript
def test_typescript_symbol_replacement_no_double_keyword(snapshot: SnapshotAssertion):
    """
    Test that replacing a TypeScript `const`/`let`/`var` declaration does not duplicate the leading
    `export`/keyword or the trailing semicolon.

    This exercises the bug where:
    - Original: export const twice = (n: number): number => n * 2;
    - Replacement body (as displayed by find_symbol): export const twice = (n: number): number => n * 3;
    - Bug result would be: export const export const twice = (n: number): number => n * 3;; (the
      original `export const ` prefix and trailing `;` are kept because the tsserver symbol range
      starts at the identifier and ends before the semicolon, and the supplied body adds both back)
    - Correct result should be: export const twice = (n: number): number => n * 3;

    tsserver reports the symbol range of such declarations starting at the identifier (after the
    keyword(s)) and ending before the semicolon, unlike `function`/`class` declarations whose range
    includes the leading keyword (and are not semicolon-terminated). The range extension in
    TypeScriptLanguageServer.request_document_symbols prevents both the duplicated prefix and the
    duplicated semicolon (GH #1956).
    """
    test_case = TsDeclReplacementTest(
        LanguageServerId.TYPESCRIPT,
        "symbol_body.ts",
        "twice",
        TS_DECL_REPLACEMENT,
    )
    test_case.run_test(content_after_ground_truth=snapshot)


class RenameSymbolTest(EditingTest):
    def __init__(self, ls_id: LanguageServerId, rel_path: str, symbol_name: str, new_name: str):
        super().__init__(ls_id, rel_path)
        self.symbol_name = symbol_name
        self.new_name = new_name

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        code_editor.rename_symbol(self.symbol_name, self.rel_path, self.new_name)

    @overrides
    def _test_diff(self, code_diff: CodeDiff, snapshot: SnapshotAssertion | None) -> None:
        # sanity check (e.g., for newly generated snapshots) that the new name is actually in the modified content
        assert self.new_name in code_diff.modified_content, f"New name '{self.new_name}' not found in modified content."
        return super()._test_diff(code_diff, snapshot)


@pytest.mark.python
def test_rename_symbol(snapshot: SnapshotAssertion):
    test_case = RenameSymbolTest(
        LanguageServerId.PYTHON,
        PYTHON_TEST_REL_FILE_PATH,
        "typed_module_var",
        "renamed_typed_module_var",
    )
    test_case.run_test(content_after_ground_truth=snapshot)


class InsertAndDeleteSymbolTest(EditingTest):
    """
    Inserts a symbol after another symbol and then deletes the inserted symbol, which should result
    in no change to the file content.
    """

    def __init__(self, ls_id: LanguageServerId, rel_path: str, rel_symbol: str, deleted_symbol: str, insertion: str):
        """
        :param ls_id: specifies the language server to use
        :param rel_path: relative path of the file
        :param rel_symbol: symbol after which the insertion is made
        :param deleted_symbol: the symbol to be deleted after the insertion
        :param insertion: text which inserts the symbol `deleted_symbol`
        """
        super().__init__(ls_id, rel_path)
        self.rel_symbol = rel_symbol
        self.deleted_symbol = deleted_symbol
        self.rel_path = rel_path
        self.insertion = insertion

    def _apply_edit(self, code_editor: CodeEditor) -> None:
        code_editor.insert_after_symbol(self.rel_symbol, self.rel_path, self.insertion)
        code_editor.delete_symbol(self.deleted_symbol, self.rel_path)

    def _test_diff(self, code_diff: CodeDiff, snapshot: SnapshotAssertion | None) -> None:
        assert not code_diff.has_changes


def test_insert_and_delete_no_change():
    """
    Tests that inserting and immediately deleting a symbol results in no change to the file content.
    """
    insertion = "    def inserted_function():\n        pass"
    test_case = InsertAndDeleteSymbolTest(
        LanguageServerId.PYTHON,
        PYTHON_TEST_REL_FILE_PATH,
        "VariableContainer/modify_instance_var",
        "VariableContainer/inserted_function",
        insertion,
    )
    test_case.run_test(None)


# ===== VUE WRITE OPERATIONS TESTS =====

VUE_TEST_FILE = os.path.join("src", "components", "CalculatorButton.vue")
VUE_STORE_FILE = os.path.join("src", "stores", "calculator.ts")

NEW_VUE_HANDLER = """const handleDoubleClick = () => {
    pressCount.value++;
    emit('click', props.label);
}"""


@pytest.mark.parametrize(
    "test_case",
    [
        pytest.param(
            DeleteSymbolTest(
                LanguageServerId.VUE,
                VUE_TEST_FILE,
                "handleMouseEnter",
            ),
            marks=pytest.mark.vue,
        ),
    ],
)
def test_delete_symbol_vue(test_case: DeleteSymbolTest, snapshot: SnapshotAssertion) -> None:
    test_case.run_test(content_after_ground_truth=snapshot)


@pytest.mark.parametrize("mode", ["before", "after"])
@pytest.mark.parametrize(
    "test_case",
    [
        pytest.param(
            InsertInRelToSymbolTest(
                LanguageServerId.VUE,
                VUE_TEST_FILE,
                "handleClick",
                NEW_VUE_HANDLER,
            ),
            marks=pytest.mark.vue,
        ),
    ],
)
def test_insert_in_rel_to_symbol_vue(
    test_case: InsertInRelToSymbolTest,
    mode: Literal["before", "after"],
    snapshot: SnapshotAssertion,
) -> None:
    test_case.set_mode(mode)
    test_case.run_test(content_after_ground_truth=snapshot)


VUE_REPLACED_HANDLECLICK_BODY = """const handleClick = () => {
    if (!props.disabled) {
        pressCount.value = 0;  // Reset instead of incrementing
        emit('click', props.label);
    }
}"""


@pytest.mark.parametrize(
    "test_case",
    [
        pytest.param(
            ReplaceBodyTest(
                LanguageServerId.VUE,
                VUE_TEST_FILE,
                "handleClick",
                VUE_REPLACED_HANDLECLICK_BODY,
            ),
            marks=pytest.mark.vue,
        ),
    ],
)
def test_replace_body_vue(test_case: ReplaceBodyTest, snapshot: SnapshotAssertion) -> None:
    test_case.run_test(content_after_ground_truth=snapshot)


VUE_REPLACED_PRESSCOUNT_BODY = """const pressCount = ref(100)"""


@pytest.mark.parametrize(
    "test_case",
    [
        pytest.param(
            ReplaceBodyTest(
                LanguageServerId.VUE,
                VUE_TEST_FILE,
                "pressCount",
                VUE_REPLACED_PRESSCOUNT_BODY,
            ),
            marks=pytest.mark.vue,
        ),
    ],
)
def test_replace_body_vue_with_disambiguation(test_case: ReplaceBodyTest, snapshot: SnapshotAssertion) -> None:
    """Test symbol disambiguation when replacing body in Vue files.

    This test verifies the fix for the Vue LSP symbol duplication issue.
    When the LSP returns two symbols with the same name (e.g., pressCount appears both as
    a definition `const pressCount = ref(0)` and as a shorthand property in `defineExpose({ pressCount })`),
    the _find_unique_symbol method should prefer the symbol with the larger range (the definition).

    The test exercises this by calling replace_body on 'pressCount', which internally calls
    _find_unique_symbol and should correctly select the definition (line 40, 19 chars) over
    the reference (line 97, 10 chars).
    """
    test_case.run_test(content_after_ground_truth=snapshot)


VUE_STORE_REPLACED_CLEAR_BODY = """function clear() {
    // Modified: Reset to initial state with a log
    console.log('Clearing calculator state');
    displayValue.value = '0';
    expression.value = '';
    operationHistory.value = [];
    lastResult.value = undefined;
}"""


@pytest.mark.parametrize(
    "test_case",
    [
        pytest.param(
            ReplaceBodyTest(
                LanguageServerId.VUE,
                VUE_STORE_FILE,
                "clear",
                VUE_STORE_REPLACED_CLEAR_BODY,
            ),
            marks=pytest.mark.vue,
        ),
    ],
)
def test_replace_body_vue_ts_file(test_case: ReplaceBodyTest, snapshot: SnapshotAssertion) -> None:
    """Test that TypeScript files within Vue projects can be edited."""
    test_case.run_test(content_after_ground_truth=snapshot)
