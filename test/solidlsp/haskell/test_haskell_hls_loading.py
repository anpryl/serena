"""How long a references request waits for HLS to load the project's components. These tests need no HLS."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import pytest

from solidlsp.language_servers import haskell_language_server
from solidlsp.language_servers.haskell_language_server import HaskellLanguageServer


def _server(component_files: Callable[[], list[str]], monkeypatch: pytest.MonkeyPatch) -> HaskellLanguageServer:
    """A HaskellLanguageServer with no HLS behind it and short waits."""
    ls = HaskellLanguageServer.__new__(HaskellLanguageServer)
    ls.repository_root_path = "/project"
    ls._hls_progress_lock = threading.Lock()
    ls._hls_active_progress = set()
    ls._hls_last_progress = time.monotonic()
    ls._hls_progress_begins = 0
    ls._hls_components_lock = threading.Lock()
    ls._hls_components_loaded = False
    ls._hls_load_attempted = False
    ls._HLS_LOAD_TIMEOUT_SECONDS = 0.6
    ls._HLS_QUERY_WAIT_SECONDS = 0.3
    ls._HLS_QUIET_SECONDS = 0.1
    monkeypatch.setattr(haskell_language_server, "_haskell_component_files", lambda root, is_ignored: component_files())
    return ls


def _record_opened_files(ls: HaskellLanguageServer) -> tuple[list[str], set[str]]:
    """Replaces `open_file`; returns every file opened, in order, and the files open right now."""
    opened: list[str] = []
    open_now: set[str] = set()

    @contextmanager
    def open_file(relative_path: str) -> Iterator[None]:
        opened.append(relative_path)
        open_now.add(relative_path)
        try:
            yield
        finally:
            open_now.discard(relative_path)

    ls.open_file = open_file  # type: ignore
    return opened, open_now


def _timed(call: Callable[[], object]) -> float:
    start = time.monotonic()
    call()
    return time.monotonic() - start


@pytest.mark.parametrize("token", [7, "indexing"])
def test_wait_lasts_while_work_is_reported(token: int | str, monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(list, monkeypatch)
    ls._on_hls_progress({"token": token, "value": {"kind": "begin", "title": "Processing"}})
    start = time.monotonic()
    assert not ls._hls_wait_idle(timeout_seconds=0.5)
    assert time.monotonic() - start >= 0.5
    ls._on_hls_progress({"token": token, "value": {"kind": "end"}})
    assert ls._hls_wait_idle(timeout_seconds=1.0)


def test_wait_lasts_for_the_quiet_period(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(list, monkeypatch)
    ls._HLS_QUIET_SECONDS = 0.4
    ls._on_hls_progress({"token": 1, "value": {"kind": "report"}})
    assert _timed(lambda: ls._hls_wait_idle(timeout_seconds=2.0)) >= 0.4
    ls._on_hls_progress({"token": 1, "value": {"kind": "report"}})
    assert not ls._hls_wait_idle(timeout_seconds=0.1)


def test_loading_keeps_files_open_until_hls_is_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    opened, open_now = _record_opened_files(ls)
    open_when_work_ended: list[set[str]] = []

    def request_symbols(relative_path: str) -> None:
        # HLS answers, then goes on typechecking the rest of the component, reporting it as progress
        ls._on_hls_progress({"token": relative_path, "value": {"kind": "begin"}})

        def finish() -> None:
            open_when_work_ended.append(set(open_now))
            ls._on_hls_progress({"token": relative_path, "value": {"kind": "end"}})

        threading.Timer(0.3, finish).start()

    ls._hls_request_symbols = request_symbols  # type: ignore
    ls._ensure_hls_components_loaded()
    assert ls._hls_components_loaded
    assert opened == ["a.hs", "b.hs"]
    assert open_when_work_ended == [{"a.hs", "b.hs"}] * 2
    assert not open_now
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs"], "a completed load is not repeated"


def test_loading_waits_through_the_pause_before_hls_reports_more_work(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    _, open_now = _record_opened_files(ls)
    ls._hls_last_progress = time.monotonic() - 10  # HLS last reported anything long ago
    open_when_work_ended: list[set[str]] = []

    def finish() -> None:
        open_when_work_ended.append(set(open_now))
        ls._on_hls_progress({"token": "typecheck", "value": {"kind": "end"}})

    def request_symbols(relative_path: str) -> None:
        # HLS answers at once, and only reports the work this starts a moment later
        threading.Timer(0.05, lambda: ls._on_hls_progress({"token": "typecheck", "value": {"kind": "begin"}})).start()
        threading.Timer(0.3, finish).start()

    ls._hls_request_symbols = request_symbols  # type: ignore
    ls._ensure_hls_components_loaded()
    assert open_when_work_ended == [{"a.hs"}]


def test_loading_stops_at_the_deadline_and_the_next_request_resumes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    opened, open_now = _record_opened_files(ls)
    answer_b = threading.Event()

    def request_symbols(relative_path: str) -> None:
        if relative_path == "b.hs":
            answer_b.wait(timeout=10)

    ls._hls_request_symbols = request_symbols  # type: ignore
    ls._HLS_LOAD_TIMEOUT_SECONDS = 1.5
    assert 1.4 <= _timed(ls._ensure_hls_components_loaded) < 2.0
    assert not ls._hls_components_loaded
    assert not open_now
    # a later request resumes the load, waiting no longer than a query does
    assert _timed(ls._ensure_hls_components_loaded) < ls._HLS_QUERY_WAIT_SECONDS + 0.4
    assert not ls._hls_components_loaded
    answer_b.set()
    ls._ensure_hls_components_loaded()
    assert ls._hls_components_loaded
    assert opened == ["a.hs", "b.hs"] * 3


def test_a_failed_discovery_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def component_files() -> list[str]:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("unreadable")
        return []

    ls = _server(component_files, monkeypatch)
    ls._ensure_hls_components_loaded()
    assert not ls._hls_components_loaded
    ls._ensure_hls_components_loaded()
    assert ls._hls_components_loaded
    assert len(calls) == 2
