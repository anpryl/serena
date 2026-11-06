"""How long a references request waits for HLS to load the project's components. These tests need no HLS."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from solidlsp.language_servers import haskell_language_server
from solidlsp.language_servers.haskell_language_server import HaskellLanguageServer
from solidlsp.ls import SolidLanguageServer


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
    ls._hls_symbol_requests = {}
    ls._HLS_LOAD_TIMEOUT_SECONDS = 0.6
    ls._HLS_QUERY_WAIT_SECONDS = 0.3
    ls._HLS_QUIET_SECONDS = 0.1
    monkeypatch.setattr(haskell_language_server, "_haskell_component_files", lambda root, is_ignored: component_files())
    monkeypatch.setattr(SolidLanguageServer, "_send_references_request", lambda self, *args: [])
    _answer_requests(ls, lambda relative_path: None)
    return ls


def _answer_requests(ls: HaskellLanguageServer, answer: Callable[[str], object]) -> None:
    """Stands in for HLS's answer to a documentSymbol request; `answer` gets the requested file's path."""

    def document_symbol(params: dict) -> object:
        return answer(params["textDocument"]["uri"].removeprefix("file:///project/"))

    ls.server = SimpleNamespace(send=SimpleNamespace(document_symbol=document_symbol))


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

    def answer(relative_path: str) -> None:
        # HLS answers, then goes on typechecking the rest of the component, reporting it as progress
        ls._on_hls_progress({"token": relative_path, "value": {"kind": "begin"}})

        def finish() -> None:
            open_when_work_ended.append(set(open_now))
            ls._on_hls_progress({"token": relative_path, "value": {"kind": "end"}})

        threading.Timer(0.3, finish).start()

    _answer_requests(ls, answer)
    ls._ensure_hls_components_loaded()
    assert ls._hls_components_loaded
    assert opened == ["a.hs", "b.hs"]
    assert open_when_work_ended == [{"a.hs", "b.hs"}] * 2
    assert not open_now
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs"], "a completed load is not repeated"


def test_loading_waits_through_the_pause_before_hls_reports_more_work(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    ls._HLS_LOAD_TIMEOUT_SECONDS = 3.0
    ls._HLS_QUIET_SECONDS = 0.5  # far longer than the pause, so that a late timer cannot end the wait
    _, open_now = _record_opened_files(ls)
    ls._hls_last_progress = time.monotonic() - 10  # HLS last reported anything long ago
    open_when_work_ended: list[set[str]] = []

    def finish() -> None:
        open_when_work_ended.append(set(open_now))
        ls._on_hls_progress({"token": "typecheck", "value": {"kind": "end"}})

    def answer(relative_path: str) -> None:
        # HLS answers at once, and only reports the work this starts a moment later
        threading.Timer(0.05, lambda: ls._on_hls_progress({"token": "typecheck", "value": {"kind": "begin"}})).start()
        threading.Timer(1.0, finish).start()

    _answer_requests(ls, answer)
    ls._ensure_hls_components_loaded()
    assert open_when_work_ended == [{"a.hs"}]


def test_loading_stops_at_the_deadline_and_the_next_request_resumes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    opened, open_now = _record_opened_files(ls)
    answer_b = threading.Event()
    requested: list[str] = []

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if relative_path == "b.hs":
            answer_b.wait(timeout=10)

    _answer_requests(ls, answer)
    ls._HLS_LOAD_TIMEOUT_SECONDS = 1.5
    assert 1.4 <= _timed(ls._ensure_hls_components_loaded) < 2.0
    assert not ls._hls_components_loaded
    assert not open_now
    # a later request resumes the load, waiting no longer than a query does
    assert _timed(ls._ensure_hls_components_loaded) < ls._HLS_QUERY_WAIT_SECONDS + 0.8
    assert not ls._hls_components_loaded
    answer_b.set()
    ls._ensure_hls_components_loaded()
    assert ls._hls_components_loaded
    assert opened == ["a.hs", "b.hs"] * 3
    assert sorted(requested) == ["a.hs", "b.hs"], "a resumed load does not request a file again"


def test_a_failed_request_leaves_the_load_to_be_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    _record_opened_files(ls)
    requested: list[str] = []
    broken = {"b.hs"}

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if relative_path in broken:
            raise RuntimeError("HLS could not set up the component")

    _answer_requests(ls, answer)
    ls._ensure_hls_components_loaded()
    assert not ls._hls_components_loaded
    broken.clear()  # e.g. the build error was fixed
    ls._ensure_hls_components_loaded()
    assert ls._hls_components_loaded
    assert sorted(requested) == ["a.hs", "b.hs", "b.hs"], "only the failed request is sent again"


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


def test_a_references_request_waits_no_longer_than_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    ls._HLS_LOAD_TIMEOUT_SECONDS, ls._HLS_QUERY_WAIT_SECONDS = 1.0, 0.8
    _record_opened_files(ls)
    ls._on_hls_progress({"token": "busy", "value": {"kind": "begin"}})  # every wait runs to its bound
    # without the bounds holding, each request would wait a further query wait
    first = _timed(lambda: ls._send_references_request("a.hs", 0, 0))
    assert ls._HLS_LOAD_TIMEOUT_SECONDS <= first < ls._HLS_LOAD_TIMEOUT_SECONDS + 0.5
    later = _timed(lambda: ls._send_references_request("a.hs", 0, 0))
    assert ls._HLS_QUERY_WAIT_SECONDS <= later < ls._HLS_QUERY_WAIT_SECONDS + 0.5


def test_a_query_after_the_load_waits_for_reported_work(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    _record_opened_files(ls)
    ls._send_references_request("a.hs", 0, 0)
    assert ls._hls_components_loaded
    ls._on_hls_progress({"token": "edit", "value": {"kind": "begin"}})
    assert _timed(lambda: ls._send_references_request("a.hs", 0, 0)) >= ls._HLS_QUERY_WAIT_SECONDS
