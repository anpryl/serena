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
    return set(_haskell_component_files(root, lambda dirname: dirname == "dist-newstyle"))


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
    files = {"p.cabal": cabal, "src/Lib.hs": "", "app/Main.hs": "", "test/Spec.hs": "", "dist-newstyle/Gen.hs": ""}
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
    hs-source-dirs: "src"
executable app
    main-is: Main.hs
    hs-source-dirs:
        app,
        -- a comment between continuation lines
        extra
"""
    files = {"p.cabal": cabal, "src/Lib.hs": "", "app/Main.hs": "", "extra/Extra.hs": ""}
    assert _component_files(tmp_path, files) == {"src/Lib.hs", "app/Main.hs", "extra/Extra.hs"}


def test_every_branch_of_a_conditional(tmp_path: Path) -> None:
    # which branch is active is not known here, so files of both are opened
    cabal = """\
executable app
    main-is: Main.hs
    if os(windows)
        hs-source-dirs: windows
    else
        hs-source-dirs: unix
"""
    files = {"p.cabal": cabal, "windows/Main.hs": "", "unix/Main.hs": ""}
    assert _component_files(tmp_path, files) == {"windows/Main.hs", "unix/Main.hs"}


def test_several_packages(tmp_path: Path) -> None:
    files = {
        "cabal.project": "packages: pkg-a pkg-b",
        "pkg-a/a.cabal": "library\n    hs-source-dirs: src\n",
        "pkg-a/src/A.hs": "",
        "pkg-b/b.cabal": "executable b\n    main-is: Main.hs\n    hs-source-dirs: app\n",
        "pkg-b/app/Main.hs": "",
    }
    assert _component_files(tmp_path, files) == {"pkg-a/src/A.hs", "pkg-b/app/Main.hs"}


def test_package_yaml_without_a_cabal_file(tmp_path: Path) -> None:
    files = {"haskell/package.yaml": "name: p\n", "haskell/src/Lib.hs": "", "haskell/app/Main.hs": "", "haskell/test/Spec.hs": ""}
    assert _component_files(tmp_path, files) == {"haskell/src/Lib.hs", "haskell/app/Main.hs", "haskell/test/Spec.hs"}


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
