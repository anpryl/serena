"""Which files Serena opens so that HLS loads every component of a Haskell project. These tests need no HLS."""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.haskell_language_server import _cabal_components, _haskell_component_files


def _component_files(root: Path, files: dict[str, str]) -> set[str]:
    for relative_path, content in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return set(_haskell_component_files(root, lambda dirname: dirname in ("dist-newstyle", ".stack-work")))


def test_library_executable_and_test_suite(tmp_path: Path) -> None:
    cabal = """\
library
    hs-source-dirs: src
    exposed-modules: Lib
executable app
    main-is: Main.hs
    hs-source-dirs: app
test-suite spec
    type: exitcode-stdio-1.0
    main-is: Spec.hs
    hs-source-dirs: test
Benchmark bench
    type: exitcode-stdio-1.0
    main-is: Bench.hs
    hs-source-dirs: bench
foreign-library flib
    type: native-shared
    other-modules: FLib
    hs-source-dirs: flib
"""
    dependency = "library\n    hs-source-dirs: lib\n"
    files = {
        "p.cabal": cabal,
        "src/Lib.hs": "",
        "app/Main.hs": "",
        "test/Spec.hs": "",
        "bench/Bench.hs": "",
        "flib/Aaa.hs": "",
        "flib/FLib.hs": "",
        # packages under ignored and hidden directories, such as cabal's checkouts of source-repository-package
        "dist-newstyle/src/dep-abc/dep.cabal": dependency,
        "dist-newstyle/src/dep-abc/lib/Dep.hs": "",
        ".stack-work/dep/dep.cabal": dependency,
        ".stack-work/dep/lib/Dep.hs": "",
        ".hidden/dep.cabal": dependency,
        ".hidden/lib/Dep.hs": "",
    }
    assert _component_files(tmp_path, files) == {"src/Lib.hs", "app/Main.hs", "test/Spec.hs", "bench/Bench.hs", "flib/FLib.hs"}


def test_source_directories_imported_from_a_common_stanza(tmp_path: Path) -> None:
    cabal = """\
common testdirs
    hs-source-dirs: test
library
    hs-source-dirs: src
test-suite spec
    import: testdirs
    type: exitcode-stdio-1.0
    main-is: Spec.hs
"""
    files = {"p.cabal": cabal, "src/Lib.hs": "", "test/Spec.hs": ""}
    assert _component_files(tmp_path, files) == {"src/Lib.hs", "test/Spec.hs"}


def test_comments_quotes_lists_and_continuation_lines(tmp_path: Path) -> None:
    cabal = """\
library
-- a comment at column 0 inside the stanza
    Hs-Source-Dirs: "my src"
executable app
    Main-Is: Main.hs
    hs-source-dirs:
        app,
        -- a comment between continuation lines

        extra
"""
    # Setup.hs and scripts/ belong to no component: one of them would be opened if the parsing failed
    files = {"p.cabal": cabal, "my src/Lib.hs": "", "extra/Main.hs": "", "scripts/Tool.hs": "", "Setup.hs": ""}
    assert _component_files(tmp_path, files) == {"my src/Lib.hs", "extra/Main.hs"}


def test_listed_modules_choose_among_files_of_a_shared_directory(tmp_path: Path) -> None:
    cabal = """\
library
    hs-source-dirs: src
    exposed-modules: Lib.Core
executable app
    main-is: Main.hs
    hs-source-dirs: src
library literate
    hs-source-dirs: lit
    exposed-modules: Lit
"""
    files = {"p.cabal": cabal, "src/Aaa.hs": "", "src/Lib/Core.hs": "", "src/Main.hs": "", "lit/Aaa.hs": "", "lit/Lit.lhs": ""}
    assert _component_files(tmp_path, files) == {"src/Lib/Core.hs", "src/Main.hs", "lit/Lit.lhs"}


def test_main_module_at_the_package_root(tmp_path: Path) -> None:
    files = {"p.cabal": "executable app\n    main-is: Main.hs\n", "Main.hs": "", "Setup.hs": ""}
    assert _component_files(tmp_path, files) == {"Main.hs"}


@pytest.mark.parametrize(
    ("cabal", "files", "expected"),
    [
        pytest.param(
            "    if os(windows)\n        hs-source-dirs: windows\n    else\n        hs-source-dirs: unix\n",
            ["windows/Main.hs", "unix/Main.hs"],
            "unix/Main.hs",
            id="else-branch-first",
        ),
        pytest.param("    if os(windows)\n        hs-source-dirs: windows\n", ["windows/Main.hs", "Main.hs"], "Main.hs", id="no-else"),
        pytest.param("    if flag(alt)\n        hs-source-dirs: alt\n", ["alt/Main.hs"], "alt/Main.hs", id="only-the-branch-has-it"),
    ],
)
def test_one_branch_of_a_conditional(tmp_path: Path, cabal: str, files: list[str], expected: str) -> None:
    # Files of two branches would both be the component's Main module, and HLS indexes whichever it typechecks
    # last, so only one is opened: outside the conditionals first, then the later branch.
    cabal_file = "executable app\n    main-is: Main.hs\n" + cabal
    assert _component_files(tmp_path, {"p.cabal": cabal_file} | dict.fromkeys(files, "")) == {expected}


def test_several_packages(tmp_path: Path) -> None:
    files = {
        "cabal.project": "packages: pkg-a pkg-b",
        "pkg-a/a.cabal": "library\n    hs-source-dirs: src\n",
        "pkg-a/package.yaml": "name: a\n",  # hpack's input; the .cabal file next to it is what counts
        "pkg-a/src/A.hs": "",
        "pkg-a/other/Other.hs": "",
        "pkg-b/b.cabal": "executable b\n    main-is: Main.hs\n    hs-source-dirs: app\n",
        "pkg-b/app/Main.hs": "",
    }
    assert _component_files(tmp_path, files) == {"pkg-a/src/A.hs", "pkg-b/app/Main.hs"}


def test_package_yaml_without_a_cabal_file(tmp_path: Path) -> None:
    files = {
        "haskell/package.yaml": "name: p\n",
        "haskell/Main.hs": "",
        "haskell/src/Lib.hs": "",
        "haskell/app/Main.hs": "",
        "haskell/test/Spec.hs": "",
    }
    expected = {"haskell/Main.hs", "haskell/src/Lib.hs", "haskell/app/Main.hs", "haskell/test/Spec.hs"}
    assert _component_files(tmp_path, files) == expected


def test_project_without_a_package_description(tmp_path: Path) -> None:
    assert _component_files(tmp_path, {"a/A.hs": "", "b/c/B.hs": ""}) == {"a/A.hs", "b/c/B.hs"}


def test_unreadable_cabal_file(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    try:
        os.symlink(tmp_path / "missing.cabal", tmp_path / "broken" / "broken.cabal")
    except OSError:
        pytest.skip("cannot create symlinks here")
    # other/ belongs to no package: it would be opened only if the broken file stopped the others
    files = {"good/good.cabal": "library\n    hs-source-dirs: src\n", "good/src/Good.hs": "", "other/Other.hs": ""}
    assert _component_files(tmp_path, files) == {"good/src/Good.hs"}


def test_source_directory_symlinked_from_outside_the_project(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "src"
    outside.mkdir(parents=True)
    (outside / "Lib.hs").write_text("")
    root = tmp_path / "project"
    root.mkdir()
    try:
        os.symlink(outside, root / "src", target_is_directory=True)
    except OSError:
        pytest.skip("cannot create symlinks here")
    assert _component_files(root, {"p.cabal": "library\n    hs-source-dirs: src\n"}) == {"src/Lib.hs"}


def test_conditional_imports(tmp_path: Path) -> None:
    cabal = """\
common win
    hs-source-dirs: windows
common unix
    hs-source-dirs: unix
executable app
    main-is: Main.hs
    if os(windows)
        import: win
    else
        import: unix
"""
    files = {"p.cabal": cabal, "windows/Main.hs": "", "unix/Main.hs": "", "Setup.hs": ""}
    assert _component_files(tmp_path, files) == {"unix/Main.hs"}


def test_modules_outside_conditionals_first(tmp_path: Path) -> None:
    cabal = """\
library
    hs-source-dirs: src
    if os(windows)
        hs-source-dirs: windows
        exposed-modules: Windows
    else
        hs-source-dirs: unix
        exposed-modules: Unix
    exposed-modules: Shared
"""
    files = {"p.cabal": cabal, "src/Shared.hs": "", "windows/Windows.hs": "", "unix/Unix.hs": ""}
    assert _component_files(tmp_path, files) == {"src/Shared.hs"}


def test_components_listing_the_same_module_first(tmp_path: Path) -> None:
    cabal = """\
library
    hs-source-dirs: src
    exposed-modules: Common, Public
library internal
    hs-source-dirs: src
    exposed-modules: Common, Internal
"""
    files = {"p.cabal": cabal, "src/Common.hs": "", "src/Public.hs": "", "src/Internal.hs": ""}
    assert _component_files(tmp_path, files) == {"src/Common.hs", "src/Internal.hs"}


def test_cabal_file_is_read_as_utf8(tmp_path: Path) -> None:
    (tmp_path / "tést").mkdir()
    (tmp_path / "tést" / "Lib.hs").write_text("")
    (tmp_path / "p.cabal").write_bytes("library\n    hs-source-dirs: tést\n".encode())
    # other/ belongs to no component: it would be opened only if the directory name were misread
    assert _component_files(tmp_path, {"other/Other.hs": ""}) == {"tést/Lib.hs"}


def test_main_module_first_even_inside_a_conditional(tmp_path: Path) -> None:
    # The main module is the one file that belongs to this component alone; Util.hs is also the library's.
    cabal = """\
library
    hs-source-dirs: src
    exposed-modules: Lib, Util
executable app
    hs-source-dirs: src
    other-modules: Util
    if os(windows)
        main-is: WinMain.hs
    else
        main-is: Main.hs
"""
    files = {"p.cabal": cabal, "src/Lib.hs": "", "src/Util.hs": "", "src/Main.hs": "", "src/WinMain.hs": ""}
    assert _component_files(tmp_path, files) == {"src/Lib.hs", "src/Main.hs"}


def test_a_field_after_a_conditional_is_outside_it(tmp_path: Path) -> None:
    cabal = """\
executable app
    main-is: Main.hs
    if os(windows)
        ghc-options: -threaded
    hs-source-dirs: app
"""
    assert _component_files(tmp_path, {"p.cabal": cabal, "Main.hs": "", "app/Main.hs": ""}) == {"app/Main.hs"}


def test_conditional_directories_in_an_imported_common_stanza(tmp_path: Path) -> None:
    cabal = """\
common platform
    if os(windows)
        hs-source-dirs: windows
    else
        hs-source-dirs: unix
executable app
    import: platform
    main-is: Main.hs
"""
    files = {"p.cabal": cabal, "windows/Main.hs": "", "unix/Main.hs": "", "Setup.hs": ""}
    assert _component_files(tmp_path, files) == {"unix/Main.hs"}


def test_capitalised_conditionals(tmp_path: Path) -> None:
    cabal = "executable app\n    main-is: Main.hs\n    If os(windows)\n        hs-source-dirs: windows\n    Else\n        hs-source-dirs: unix\n"
    files = {"p.cabal": cabal, "windows/Main.hs": "", "unix/Main.hs": ""}
    assert _component_files(tmp_path, files) == {"unix/Main.hs"}


def test_detailed_test_suite_sharing_the_library_directory(tmp_path: Path) -> None:
    cabal = """\
library
    hs-source-dirs: src
    exposed-modules: Lib
test-suite t
    type: detailed-0.9
    test-module: Tests
    other-modules: Helpers
    hs-source-dirs: src test
"""
    files = {"p.cabal": cabal, "src/Lib.hs": "", "test/Helpers.hs": "", "test/Tests.hs": ""}
    assert _component_files(tmp_path, files) == {"src/Lib.hs", "test/Tests.hs"}


@pytest.mark.parametrize("outside_dir", ["absolute", "../outside"])
def test_source_directory_outside_the_project(tmp_path: Path, outside_dir: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Gen.hs").write_text("")
    root = tmp_path / "project"
    directory = outside.as_posix() if outside_dir == "absolute" else outside_dir
    cabal = f"library\n    hs-source-dirs: {directory} src\n    exposed-modules: Gen, Lib\n"
    # other/ belongs to no component: it would be opened only if the whole component were dropped
    assert _component_files(root, {"p.cabal": cabal, "src/Lib.hs": "", "other/Other.hs": ""}) == {"src/Lib.hs"}


def test_absolute_source_directory_inside_the_project(tmp_path: Path) -> None:
    cabal = (
        f"library\n    hs-source-dirs: {(tmp_path / 'src').as_posix()}\n    exposed-modules: Lib\nexecutable app\n    main-is: Main.hs\n"
    )
    assert _component_files(tmp_path, {"p.cabal": cabal, "src/Lib.hs": "", "Main.hs": ""}) == {"src/Lib.hs", "Main.hs"}


def test_fallback_skips_a_file_another_component_has(tmp_path: Path) -> None:
    files = {"p.cabal": "library\n    hs-source-dirs: src\nlibrary internal\n    hs-source-dirs: src\n", "src/A.hs": "", "src/B.hs": ""}
    assert _component_files(tmp_path, files) == {"src/A.hs", "src/B.hs"}


def test_elif_branches(tmp_path: Path) -> None:
    cabal = """\
executable app
    main-is: Main.hs
    if os(windows)
        hs-source-dirs: windows
    elif os(darwin)
        hs-source-dirs: darwin
    else
        hs-source-dirs: unix
"""
    files = {"p.cabal": cabal, "windows/Main.hs": "", "darwin/Main.hs": "", "unix/Main.hs": ""}
    assert _component_files(tmp_path, files) == {"unix/Main.hs"}


def test_module_list_one_per_line_without_commas(tmp_path: Path) -> None:
    # as hpack writes them; the first module listed is chosen, not the first file
    cabal = "library\n    hs-source-dirs: src\n    exposed-modules:\n        Zeta\n        Alpha\n"
    assert _component_files(tmp_path, {"p.cabal": cabal, "src/Alpha.hs": "", "src/Zeta.hs": ""}) == {"src/Zeta.hs"}


def test_main_module_first_in_a_later_directory(tmp_path: Path) -> None:
    cabal = "executable app\n    hs-source-dirs: src app\n    main-is: Main.hs\n    other-modules: Util\n"
    assert _component_files(tmp_path, {"p.cabal": cabal, "src/Util.hs": "", "app/Main.hs": ""}) == {"app/Main.hs"}


def test_common_stanza_importing_another(tmp_path: Path) -> None:
    cabal = "common base\n    hs-source-dirs: app\ncommon exe\n    import: base\nexecutable app\n    import: exe\n    main-is: Main.hs\n"
    assert _component_files(tmp_path, {"p.cabal": cabal, "app/Main.hs": "", "Main.hs": ""}) == {"app/Main.hs"}


def test_a_continuation_line_that_looks_like_a_field() -> None:
    # `C:` is a Windows drive, not a field named C: a line indented deeper than its field continues it
    cabal = "library\n    hs-source-dirs:\n        C:/repo/src\n    exposed-modules: Lib\n"
    assert _cabal_components(cabal)[0][0] == ["C:/repo/src"]
