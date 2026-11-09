"""
The check Serena makes before applying HLS's rename: every use a text search finds must be in the edit. These
tests need no HLS.
"""

from pathlib import Path

import pytest

from solidlsp.language_servers.haskell_language_server import (
    HaskellLanguageServer,
    _haskell_apply_edits,
    _haskell_astral_edits,
    _haskell_code,
    _haskell_exports,
    _haskell_name_at,
    _haskell_name_spans,
    _haskell_rename_misses,
    _haskell_text_edits,
    _haskell_uses_to_check,
)
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_exceptions import SolidLSPException

LIB = """\
module Lib (Config (..), libFn) where

data Config = Config {cfgLevel :: Int}

libFn :: Int -> Int
libFn = (+ 1)

helper :: Int -> Int
helper = libFn
"""


def _uses(sources: dict[str, str], name: str, defining: str = "src/Lib.hs") -> set[tuple[str, int]]:
    return {(path, line) for path, line, _, _ in _haskell_uses_to_check(sources, defining, name)}


def test_comments_and_pragmas_are_blanked_in_place() -> None:
    text = "{-# LANGUAGE X #-}\nx = f -- f here\n{- a {- f -} b -} f\n"
    code = _haskell_code(text)
    assert len(code) == len(text)
    assert code.split("\n") == [" " * 18, "x = f          ", " " * 17 + " f", ""]


def test_strings_operators_and_char_literals_are_not_comments() -> None:
    for text in ['g = "f -- not a comment"', "a --> f", "a |-- f", 'h = mkName "f"']:
        assert _haskell_code(text) == text
    # a double quote in a character literal does not start a string that would hide the comment
    assert _haskell_code("c = '\"' -- f") == "c = '\"'     "


def test_name_at_a_column() -> None:
    assert _haskell_name_at("  y = C.totalArea xs", 8) == "totalArea"
    assert _haskell_name_at("a <+> b", 3) == "<+>"
    assert _haskell_name_at("a   b", 2) is None


@pytest.mark.parametrize(
    ("code", "name", "exported"),
    [
        (LIB, "libFn", True),
        (LIB, "cfgLevel", True),  # a field of the exported `Config (..)`
        (LIB, "helper", False),
        ("module M where\nf = 1\n", "f", True),  # no export list
        ("main = pure ()\nf = 1\n", "main", True),  # no header: `module Main (main) where`
        ("main = pure ()\nf = 1\n", "f", False),
        ("module M (module M) where\nf = 1\n", "f", True),
        ("module M (Describe (..)) where\nclass Describe a where\n  describe :: a -> String\n", "describe", True),
        ("module M (T (A, b)) where\ndata T = A | B {b :: Int}\n", "b", True),
    ],
)
def test_what_a_module_exports(code: str, name: str, exported: bool) -> None:
    assert _haskell_exports(_haskell_code(code), name, _haskell_code(code), ["M"], declares=True) is exported


def test_a_module_without_an_export_list_re_exports_nothing() -> None:
    code = "module Plain where\nimport Lib\n"
    assert not _haskell_exports(code, "libFn", LIB, ["Lib"], declares=False)


def test_uses_in_importers_count_for_an_exported_name() -> None:
    sources = {
        "src/Lib.hs": LIB,
        "app/Main.hs": "module Main (main) where\nimport Lib\nmain = print (libFn 1)\n",
        "other/Other.hs": "module Other where\nlibFn = 2\n",  # does not import Lib: its libFn is another one
    }
    assert _uses(sources, "libFn") == {("app/Main.hs", 2)}


def test_only_qualified_uses_count_where_the_import_is_qualified() -> None:
    sources = {
        "src/Lib.hs": LIB,
        "test/Spec.hs": "module Main (main) where\nimport qualified Lib as L\nmain = print (L.libFn 1, libFn)\n  where\n    libFn = 3\n",
    }
    assert _uses(sources, "libFn") == {("test/Spec.hs", 2)}
    assert [(c[2], c[3]) for c in _haskell_uses_to_check(sources, "src/Lib.hs", "libFn")] == [(16, 21)]


def test_a_name_that_is_not_exported_is_not_searched_for_in_importers() -> None:
    sources = {"src/Lib.hs": LIB, "app/Main.hs": "module Main (main) where\nimport Lib\nhelper = 1\nmain = print helper\n"}
    assert _uses(sources, "helper") == set()


def test_cpp_branches_and_record_dot_accesses_count_in_the_declaring_module() -> None:
    lib = LIB + "#if FOO\nviaCpp = helper 1\n#endif\nlevel c = c.cfgLevel\nqualified = Lib.helper\n"
    uses = _uses({"src/Lib.hs": lib}, "helper")
    assert uses == {("src/Lib.hs", 10)}
    assert _uses({"src/Lib.hs": lib}, "cfgLevel") == {("src/Lib.hs", 12)}


def test_comments_do_not_count_and_strings_do() -> None:
    importer = 'module Gen where\nimport Lib\n-- libFn is used below\nviaString = mkName "libFn"\n'
    assert _uses({"src/Lib.hs": LIB, "src/Gen.hs": importer}, "libFn") == {("src/Gen.hs", 3)}


def test_uses_through_a_re_exporting_module_count() -> None:
    sources = {
        "src/Lib.hs": LIB,
        "src/Reexport.hs": "module Reexport (module Lib) where\nimport Lib\n",
        "app/Use.hs": "module Use where\nimport Reexport\nx = libFn 2\n",
        "src/Plain.hs": "module Plain where\nimport Lib\n",
        "app/Other.hs": "module Other where\nimport Plain\nlibFn = 3\n",  # Plain re-exports nothing
    }
    assert _uses(sources, "libFn") == {("app/Use.hs", 2)}


def test_text_edits_from_both_forms_of_workspace_edit(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    edit = {
        "changes": {(root / "app/Main.hs").as_uri(): [_range(2, 14, 19)]},
        "documentChanges": [
            {"textDocument": {"uri": (root / "src/Gen.hs").as_uri(), "version": None}, "edits": [_range(3, 0, 4)]},
            {"kind": "create", "uri": (root / "new.hs").as_uri()},
        ],
    }
    assert _haskell_text_edits(edit, root) == {"app/Main.hs": [(2, 14, 2, 19, "f")], "src/Gen.hs": [(3, 0, 3, 4, "f")]}


def test_edits_are_applied_at_utf16_columns() -> None:
    """LSP counts an astral character as two columns; HLS replaces whole lines too."""
    text = 'a\nx = ("\U0001f600", libFn 1)\nb\n'
    assert _haskell_apply_edits(text, [(1, 11, 1, 16, "g")]) == 'a\nx = ("\U0001f600", g 1)\nb\n'
    # an earlier edit that changes the length must not shift a later one
    assert _haskell_apply_edits(text, [(0, 0, 0, 1, "ccc"), (1, 0, 2, 0, "y = 2\n")]) == "ccc\ny = 2\nb\n"


def test_a_use_the_edit_leaves_is_a_miss_even_on_a_rewritten_line(tmp_path: Path) -> None:
    """HLS rewrites `f c = cfgLevel c + c.cfgLevel` as a whole line but renames only the selector."""
    root = tmp_path.resolve()
    lib = LIB + "f c = cfgLevel c + c.cfgLevel\n"
    use = "module Main (main) where\nimport Lib\nmain = print (cfgLevel (Config 1))\n"
    sources = {"src/Lib.hs": lib, "app/Main.hs": use}
    lib_uri, main_uri = (root / "src/Lib.hs").as_uri(), (root / "app/Main.hs").as_uri()
    rewrite = {
        "range": {"start": {"line": 9, "character": 0}, "end": {"line": 10, "character": 0}},
        "newText": "f c = level c + c.cfgLevel\n",
    }
    edit = {"changes": {lib_uri: [rewrite], main_uri: [_range(2, 14, 22)]}}
    assert _haskell_rename_misses(sources, "src/Lib.hs", "cfgLevel", edit, root) == [("src/Lib.hs", 9, "f c = level c + c.cfgLevel")]
    rewrite["newText"] = "f c = level c + c.level\n"
    assert _haskell_rename_misses(sources, "src/Lib.hs", "cfgLevel", edit, root) == []


def test_a_re_export_chain_through_a_module_mentioning_neither_name() -> None:
    sources = {
        "src/Lib.hs": LIB,
        "src/Re1.hs": "module Re1 (module Lib) where\nimport Lib\n",
        "src/Re2.hs": "module Re2 (module Re1) where\nimport Re1\n",
        "app/Use.hs": "module Use where\nimport Re2\nx = libFn\n",
    }
    assert _uses(sources, "libFn") == {("app/Use.hs", 2)}


def test_every_import_path_of_a_module_counts() -> None:
    use = "module Use where\nimport qualified Lib as L\nimport qualified Re as R\nx = L.libFn\ny = R.libFn\n"
    sources = {"src/Lib.hs": LIB, "src/Re.hs": "module Re (module Lib) where\nimport Lib\n", "app/Use.hs": use}
    assert _uses(sources, "libFn") == {("app/Use.hs", 3), ("app/Use.hs", 4)}


def test_qualified_operators_and_template_haskell_quotes() -> None:
    lib = "module Lib ((<+>), libFn, T) where\n(<+>) = (+)\nlibFn = 1\ndata T = T\n"
    use = "module Use where\nimport qualified Lib as L\nimport Lib (libFn, T)\nx = 1 L.<+> 2\ny = varE 'libFn\nz = conT ''T\n"
    sources = {"src/Lib.hs": lib, "app/Use.hs": use}
    assert _uses(sources, "<+>") == {("app/Use.hs", 3)}
    assert _uses(sources, "libFn") == {("app/Use.hs", 2), ("app/Use.hs", 4)}
    assert _uses(sources, "T") == {("app/Use.hs", 2), ("app/Use.hs", 5)}
    assert _haskell_name_at("x = 1 L.<+> 2", 9) == "<+>"


def test_headers_and_imports_found_where_layout_allows_them() -> None:
    sources = {
        "src/Lib.hs": "{- license -} module Lib (libFn) where\nlibFn = 1\n",
        "app/Indented.hs": "module Indented where\n  import Lib\n  x = libFn\n",
        "app/Binding.hs": "module Binding where\nimport Lib\nqualifiedResult = libFn 1\n",  # not ImportQualifiedPost
        "app/Unicode.hs": "module École (y) where\nimport Lib\ny = libFn\n",
    }
    assert _uses(sources, "libFn") == {("app/Indented.hs", 2), ("app/Binding.hs", 2), ("app/Unicode.hs", 2)}
    unicode = {"src/Lib.hs": "module École (libFn) where\nlibFn = 1\n", "app/Use.hs": "module Use where\nimport École\nx = libFn\n"}
    assert _uses(unicode, "libFn") == {("app/Use.hs", 2)}


def test_members_of_operator_and_indented_types_exported_through_wildcards() -> None:
    operator = "module Lib ((:+:) (..)) where\ndata a :+: b = Lft a | Rgt b\n"
    assert _uses({"src/Lib.hs": operator, "app/Use.hs": "module Use where\nimport Lib\nx = Lft 1\n"}, "Lft") == {("app/Use.hs", 2)}
    # a module body may be indented as a whole
    indented = "module Lib (T (..)) where\n  data T = T {field :: Int}\n  other = 1\n"
    assert _uses({"src/Lib.hs": indented, "app/Use.hs": "module Use where\nimport Lib\ny = field\n"}, "field") == {("app/Use.hs", 2)}
    assert _uses({"src/Lib.hs": indented, "app/Use.hs": "module Use where\nimport Lib\ny = other\n"}, "other") == set()


def test_record_projections_count_in_the_declaring_module() -> None:
    assert _uses({"src/Lib.hs": LIB + "y = (.cfgLevel) (Config 1)\n"}, "cfgLevel") == {("src/Lib.hs", 9)}


def test_string_gaps_and_unicode_operators_do_not_confuse_comments() -> None:
    assert "libFn" not in _haskell_code('s = "a\\\n\n    \\b" -- libFn\n')
    assert "libFn" in _haskell_code("x = 1 --⊕ libFn 2\n")


def test_template_haskell_quotes_and_strings_count_in_the_declaring_module() -> None:
    lib = LIB + 'n = \'libFn\nm = mkName "libFn"\n'
    assert _uses({"src/Lib.hs": lib}, "libFn") == {("src/Lib.hs", 9), ("src/Lib.hs", 10)}


def test_unicode_operators() -> None:
    lib = "module Lib ((⊕)) where\n(⊕) = (+)\n#if X\nx = 1 ⊕ 2\n#endif\n"
    assert _uses({"src/Lib.hs": lib}, "⊕") == {("src/Lib.hs", 3)}
    assert _haskell_name_at("x = 1 ⊕ 2", 6) == "⊕"
    assert list(_haskell_name_spans("a <+>⊕ b", "<+>")) == []


def test_import_lists_and_what_they_bring_into_scope() -> None:
    def uses(importer: str) -> list[tuple[int, int, int]]:
        return [use[1:] for use in _haskell_uses_to_check({"src/Lib.hs": LIB, "app/Use.hs": importer}, "src/Lib.hs", "libFn")]

    # a qualified import's list names it
    assert uses("module Use where\nimport qualified Lib as L (libFn)\nx = L.libFn\n") == [(1, 27, 32), (2, 6, 11)]
    # imported unqualified, a name qualified by another module is not it
    assert uses("module Use where\nimport Lib\nimport qualified Other as O\nx = (libFn, O.libFn)\n") == [(3, 5, 10)]
    # an import bringing nothing, or hiding the name, leaves a same-named local alone; the hiding list names it
    assert uses("module Use where\nimport Lib ()\nlibFn = 2\n") == []
    assert uses("module Use where\nimport Lib hiding (libFn)\nlibFn = 2\n") == [(1, 19, 24)]
    # explicit layout, and ImportQualifiedPost with `qualified` on the next line
    assert uses("module Use where { import Lib; x = libFn }\n") == [(0, 35, 40)]
    assert uses("module Use where\nimport Lib\n  qualified as L\nx = (L.libFn, libFn)\n  where libFn = 2\n") == [(3, 7, 12)]


def test_a_class_with_a_superclass_context_is_not_taken_for_its_superclass() -> None:
    lib = "module Lib (T (..)) where\nclass T a => U a\nclass T a where\n  f :: a -> Int\n"
    assert _uses({"src/Lib.hs": lib, "app/Use.hs": "module Use where\nimport Lib\ny = f\n"}, "f") == {("app/Use.hs", 2)}


def test_a_module_named_like_the_type_is_not_a_use_of_it() -> None:
    sources = {
        "src/T.hs": "module T (T (..)) where\ndata T = MkT\n",
        "app/Use.hs": "module Use where\nimport T (T (..))\nx :: T\nx = MkT\ny = T.MkT\n",
    }
    assert [use[1:] for use in _haskell_uses_to_check(sources, "src/T.hs", "T") if use[0] == "app/Use.hs"] == [(1, 10, 11), (2, 5, 6)]


def test_edits_at_the_same_position_and_at_the_end_of_the_file() -> None:
    assert _haskell_apply_edits("x = f\n", [(0, 4, 0, 4, "Lib."), (0, 4, 0, 5, "g")]) == "x = Lib.g\n"
    assert _haskell_apply_edits("x = f", [(0, 0, 1, 0, "x = g"), (0, 5, 0, 5, " + f")]) == "x = g + f"


def test_only_an_astral_character_before_an_edit_makes_it_unsafe() -> None:
    sources = {"a.hs": 'f = "\U0001f600"\ng = "\U0001f600" ++ f\n'}
    assert _haskell_astral_edits(sources, {"a.hs": [(0, 0, 0, 1, "h")]}) == []
    assert _haskell_astral_edits(sources, {"a.hs": [(1, 14, 1, 15, "h")]}) == [("a.hs", 1, 'g = "\U0001f600" ++ f')]


def _guarded_server(root: Path, edit: dict, monkeypatch: pytest.MonkeyPatch) -> HaskellLanguageServer:
    ls = HaskellLanguageServer.__new__(HaskellLanguageServer)
    ls.repository_root_path = str(root)
    monkeypatch.setattr(ls, "_ensure_hls_components_loaded", lambda: True, raising=False)
    monkeypatch.setattr(ls, "is_ignored_path", lambda relative_path, ignore_unsupported_files=True: False, raising=False)
    monkeypatch.setattr(SolidLanguageServer, "request_rename_symbol_edit", lambda self, *args: edit)
    return ls


def _write(root: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (root / relative_path).write_text(content)


def _range(line: int, start: int, end: int) -> dict:
    return {"range": {"start": {"line": line, "character": start}, "end": {"line": line, "character": end}}, "newText": "f"}


def test_a_complete_rename_is_returned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve()
    _write(root, {"src/Lib.hs": LIB, "app/Main.hs": "module Main (main) where\nimport Lib\nmain = print (libFn 1)\n"})
    lib_uri, main_uri = (root / "src/Lib.hs").as_uri(), (root / "app/Main.hs").as_uri()
    edit = {"changes": {lib_uri: [_range(0, 25, 30), _range(4, 0, 5), _range(5, 0, 5), _range(8, 9, 14)], main_uri: [_range(2, 14, 19)]}}
    ls = _guarded_server(root, edit, monkeypatch)
    assert ls.request_rename_symbol_edit("src/Lib.hs", 4, 0, "f") is edit


def test_symlinked_and_unreadable_files_are_left_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A symlink would count the target's uses twice, under a path HLS's edit does not name."""
    root = tmp_path.resolve()
    use = "module Main (main) where\nimport Lib\nmain = print (libFn 1)\n"
    _write(root, {"src/Lib.hs": LIB, "app/Main.hs": use})
    try:
        (root / "app" / "Alias.hs").symlink_to(root / "app" / "Main.hs")
        (root / "app" / "Broken.hs").symlink_to(root / "app" / "Missing.hs")
    except OSError:
        pytest.skip("symlinks cannot be created here")
    lib_uri, main_uri = (root / "src/Lib.hs").as_uri(), (root / "app/Main.hs").as_uri()
    edit = {"changes": {lib_uri: [_range(0, 25, 30), _range(4, 0, 5), _range(5, 0, 5), _range(8, 9, 14)], main_uri: [_range(2, 14, 19)]}}
    ls = _guarded_server(root, edit, monkeypatch)
    assert ls.request_rename_symbol_edit("src/Lib.hs", 4, 0, "f") is edit


def test_the_renamed_position_is_a_utf16_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With an astral character before the name, the UTF-16 column is past its string index."""
    root = tmp_path.resolve()
    lib = "module Lib (f) where\n{- \U0001f600 -} f = 1\n"
    _write(root, {"src/Lib.hs": lib, "app/Main.hs": "module Main (main) where\nimport Lib\nmain = print f\n"})
    # f is at string index 8 and UTF-16 column 9; read as an index, 9 is the space after it
    ls = _guarded_server(root, {"changes": {(root / "src/Lib.hs").as_uri(): [_range(1, 9, 10)]}}, monkeypatch)
    # read unconverted, the name would be None and the edit returned unchecked; Serena would write it a column off
    with pytest.raises(SolidLSPException, match=r"Not renaming 'f': Serena would write HLS's edit at the wrong column"):
        ls.request_rename_symbol_edit("src/Lib.hs", 1, 9, "g")


def test_a_re_exporting_module_is_read_though_it_mentions_neither_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve()
    _write(
        root,
        {
            "src/Lib.hs": LIB,
            "src/Re1.hs": "module Re1 (module Lib) where\nimport Lib\n",
            "src/Re2.hs": "module Re2 (module Re1) where\nimport Re1\n",
            "app/Use.hs": "module Use where\nimport Re2\nx = libFn\n",
        },
    )
    ls = _guarded_server(root, {"changes": {(root / "src/Lib.hs").as_uri(): [_range(4, 0, 5), _range(5, 0, 5)]}}, monkeypatch)
    with pytest.raises(SolidLSPException, match=r"app/Use\.hs:3"):
        ls.request_rename_symbol_edit("src/Lib.hs", 4, 0, "f")


def test_a_rename_loads_the_components_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HLS knows only the importers in components it has loaded, as for references."""
    root = tmp_path.resolve()
    _write(root, {"src/Lib.hs": LIB})
    calls: list[str] = []
    ls = _guarded_server(root, {"changes": {}}, monkeypatch)
    monkeypatch.setattr(ls, "_ensure_hls_components_loaded", lambda: calls.append("loaded") or True, raising=False)
    monkeypatch.setattr(SolidLanguageServer, "request_rename_symbol_edit", lambda self, *args: calls.append("renamed") or {"changes": {}})
    ls.request_rename_symbol_edit("src/Lib.hs", 4, 0, "f")
    assert calls == ["loaded", "renamed"]


def test_a_rename_missing_a_use_is_refused_with_the_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path.resolve()
    unloaded = "module Main (main) where\nimport Lib\nmain = print (libFn 2)\n"
    _write(
        root, {"src/Lib.hs": LIB, "app/Main.hs": "module Main (main) where\nimport Lib\nmain = print (libFn 1)\n", "alt/Main.hs": unloaded}
    )
    lib_uri, main_uri = (root / "src/Lib.hs").as_uri(), (root / "app/Main.hs").as_uri()
    edit = {"changes": {lib_uri: [_range(0, 25, 30), _range(4, 0, 5), _range(5, 0, 5), _range(8, 9, 14)], main_uri: [_range(2, 14, 19)]}}
    ls = _guarded_server(root, edit, monkeypatch)
    with pytest.raises(SolidLSPException, match=r"leaves it on 1 line.*alt/Main\.hs:3: main = print \(libFn 2\)"):
        ls.request_rename_symbol_edit("src/Lib.hs", 4, 0, "f")
