"""
Cross-component references for Haskell.

This module starts its own language server, with an empty HLS cache. HLS keeps its reference index under
$XDG_CACHE_HOME, and an index written while other tests had the executable open would let these references
reach it even without the component loading under test.
"""

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from solidlsp.language_servers.haskell_language_server import HaskellLanguageServer
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_exceptions import SolidLSPException
from test.conftest import get_repo_path, language_server_tests_enabled, start_ls_context

pytestmark = [
    pytest.mark.haskell,
    pytest.mark.skipif(
        not language_server_tests_enabled(LanguageServerId.HASKELL), reason="Haskell tests are disabled (HLS not available)"
    ),
]


@pytest.fixture(scope="module")
def fresh_haskell_ls(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SolidLanguageServer]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("hls-cache")))
        with start_ls_context(LanguageServerId.HASKELL) as ls:
            yield ls


@pytest.fixture(scope="module")
def ls_with_unloaded_module(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SolidLanguageServer]:
    """The test repository plus extra/Unloaded.hs, which calls isPositive but is in no cabal component."""
    repo = tmp_path_factory.mktemp("haskell-repo") / "repo"
    shutil.copytree(get_repo_path(LanguageServerId.HASKELL), repo, ignore=shutil.ignore_patterns("dist-newstyle", ".serena"))
    (repo / "extra").mkdir()
    (repo / "extra" / "Unloaded.hs").write_text(
        "module Unloaded (main) where\n\nimport Helper (isPositive)\n\nmain :: IO ()\nmain = print (isPositive 1)\n"
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("hls-cache")))
        with start_ls_context(LanguageServerId.HASKELL, str(repo)) as ls:
            yield ls


def test_references_reach_the_executable_component(fresh_haskell_ls: SolidLanguageServer) -> None:
    """
    The function isPositive is defined in the library (src/Helper.hs) and called only from the executable
    (app/Main.hs), a different cabal component that nothing in this module opens before the query.
    """
    references = fresh_haskell_ls.request_references("src/Helper.hs", line=12, column=0)
    found = {(Path(ref["relativePath"]).as_posix(), ref["range"]["start"]["line"]) for ref in references}
    assert ("app/Main.hs", 24) in found, f"Expected the call at app/Main.hs:25 among {sorted(found)}"


def test_references_reach_the_test_suite_through_a_qualified_import(fresh_haskell_ls: SolidLanguageServer) -> None:
    """test/Spec.hs, the test suite's main module, calls isPositive as H.isPositive."""
    references = fresh_haskell_ls.request_references("src/Helper.hs", line=12, column=0)
    found = {(Path(ref["relativePath"]).as_posix(), ref["range"]["start"]["line"]) for ref in references}
    assert ("test/Spec.hs", 5) in found, f"Expected the call at test/Spec.hs:6 among {sorted(found)}"


def _edited_files(edit: dict) -> set[str]:
    uris = set(edit.get("changes") or {}) | {c["textDocument"]["uri"] for c in edit.get("documentChanges") or [] if "edits" in c}
    return {Path(uri).name for uri in uris}


def test_a_complete_rename_across_components_is_returned(fresh_haskell_ls: SolidLanguageServer) -> None:
    """HLS 2.14 and later rename exported names across modules; the edit reaches every component's use."""
    try:
        edit = fresh_haskell_ls.request_rename_symbol_edit("src/Helper.hs", 12, 0, "isStrictlyPositive")
    except SolidLSPException as e:
        if "exported name is unsupported" in str(e.cause or e):
            pytest.skip("this HLS does not rename exported names across modules")
        raise
    assert edit is not None
    assert {"Main.hs", "Spec.hs"} <= _edited_files(dict(edit))


def test_a_rename_missing_an_unloaded_module_is_not_applied(ls_with_unloaded_module: SolidLanguageServer) -> None:
    """
    HLS never loads extra/Unloaded.hs, so its edit leaves the call there; the edit is refused, naming the call.
    An HLS that refuses exported renames itself (before 2.14) refuses this one too.
    """
    with pytest.raises(SolidLSPException) as refusal:
        ls_with_unloaded_module.request_rename_symbol_edit("src/Helper.hs", 12, 0, "isStrictlyPositive")
    message = str(refusal.value.cause or refusal.value)
    assert "exported name is unsupported" in message or "extra/Unloaded.hs:6: main = print (isPositive 1)" in message, message


def test_hls_reported_its_loading_progress(fresh_haskell_ls: SolidLanguageServer) -> None:
    """
    The component loading waits on HLS's work-done progress, which HLS only reports once the client accepts its
    window/workDoneProgress/create request.
    """
    fresh_haskell_ls.request_references("src/Helper.hs", line=12, column=0)
    assert isinstance(fresh_haskell_ls, HaskellLanguageServer)
    assert fresh_haskell_ls._hls_progress_begins > 0
