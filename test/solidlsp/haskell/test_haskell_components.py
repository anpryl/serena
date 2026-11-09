"""
Cross-component references for Haskell.

This module starts its own language server, with an empty HLS cache. HLS keeps its reference index under
$XDG_CACHE_HOME, and an index written while other tests had the executable open would let these references
reach it even without the component loading under test.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from solidlsp.language_servers.haskell_language_server import HaskellLanguageServer
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_exceptions import SolidLSPException
from test.conftest import language_server_tests_enabled, start_ls_context

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


def test_renaming_an_exported_name_is_refused(fresh_haskell_ls: SolidLanguageServer) -> None:
    """
    The function isPositive is exported and used in app/Main.hs. HLS after 2.13 would rename it across modules, from
    references that can be incomplete; Serena turns that off, so HLS refuses, as 2.13 did.
    """
    with pytest.raises(SolidLSPException, match="rename"):
        fresh_haskell_ls.request_rename_symbol_edit("src/Helper.hs", 12, 0, "isStrictlyPositive")


def test_hls_reported_its_loading_progress(fresh_haskell_ls: SolidLanguageServer) -> None:
    """
    The component loading waits on HLS's work-done progress, which HLS only reports once the client accepts its
    window/workDoneProgress/create request.
    """
    fresh_haskell_ls.request_references("src/Helper.hs", line=12, column=0)
    assert isinstance(fresh_haskell_ls, HaskellLanguageServer)
    assert fresh_haskell_ls._hls_progress_begins > 0
