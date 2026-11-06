"""Which files Serena opens so that HLS loads every component of a Haskell project. These tests need no HLS."""

import os
from pathlib import Path

import pytest

from solidlsp.language_servers.haskell_language_server import _haskell_component_files


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
"""
    dependency = "library\n    hs-source-dirs: lib\n"
    files = {
        "p.cabal": cabal,
        "src/Lib.hs": "",
        "app/Main.hs": "",
        "test/Spec.hs": "",
        # packages under ignored and hidden directories, such as cabal's checkouts of source-repository-package
        "dist-newstyle/src/dep-abc/dep.cabal": dependency,
        "dist-newstyle/src/dep-abc/lib/Dep.hs": "",
        ".stack-work/dep/dep.cabal": dependency,
        ".stack-work/dep/lib/Dep.hs": "",
        ".hidden/dep.cabal": dependency,
        ".hidden/lib/Dep.hs": "",
    }
    assert _component_files(tmp_path, files) == {"src/Lib.hs", "app/Main.hs", "test/Spec.hs"}


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
    # scripts/ belongs to no component: it would be opened only if the parsing failed and left nothing to open
    files = {"p.cabal": cabal, "my src/Lib.hs": "", "extra/Main.hs": "", "scripts/Tool.hs": ""}
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
    files = {"p.cabal": cabal, "src/Aaa.hs": "", "src/Lib/Core.hs": "", "src/Main.hs": "", "lit/Lit.lhs": ""}
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
    files = {"good/good.cabal": "library\n    hs-source-dirs: src\n", "good/src/Good.hs": ""}
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
