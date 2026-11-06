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
    ls._resolve_file_uri = lambda relative_path: "file:///project/" + relative_path  # "/project" has no drive on Windows
    ls._hls_progress_lock = threading.Lock()
    ls._hls_active_progress = set()
    ls._hls_last_progress = time.monotonic()
    ls._hls_progress_begins = 0
    ls._hls_components_lock = threading.Lock()
    ls._hls_components_loaded = False
    ls._HLS_LOAD_TIMEOUT_SECONDS = 1.0
    ls._HLS_QUERY_WAIT_SECONDS = 0.8
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


def _record_opened_files(ls: HaskellLanguageServer, unopenable: frozenset[str] = frozenset()) -> tuple[list[str], set[str]]:
    """Replaces `open_file`; returns every file opened, in order, and the files open right now."""
    opened: list[str] = []
    open_now: set[str] = set()

    @contextmanager
    def open_file(relative_path: str) -> Iterator[None]:
        if relative_path in unopenable:
            raise FileNotFoundError(relative_path)
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


def test_wait_ends_when_reported_work_reaches_100_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    # HLS can leave a token open at 100% for good
    ls = _server(list, monkeypatch)
    ls._on_hls_progress({"token": 3, "value": {"kind": "begin", "percentage": 0}})
    ls._on_hls_progress({"token": 3, "value": {"kind": "report", "percentage": 100}})
    assert ls._hls_wait_idle(timeout_seconds=1.0)
    ls._on_hls_progress({"token": 3, "value": {"kind": "report", "percentage": 99}})
    assert not ls._hls_wait_idle(timeout_seconds=0.3)


def test_each_file_is_closed_before_the_next_is_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    # every file opened restarts HLS's work on all the open ones
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    _, open_now = _record_opened_files(ls)
    open_when_requested: list[set[str]] = []
    _answer_requests(ls, lambda relative_path: open_when_requested.append(set(open_now)))
    ls._ensure_hls_components_loaded()
    assert open_when_requested == [{"a.hs"}, {"b.hs"}]


def test_loading_reopens_the_files_until_hls_is_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    ls._HLS_LOAD_TIMEOUT_SECONDS = 3.0
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
    assert opened == ["a.hs", "b.hs"] * 2
    assert open_when_work_ended == [{"a.hs", "b.hs"}] * 2
    assert not open_now
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs"] * 2, "the load is not repeated"


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


def test_components_are_requested_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # HLS answers a references request only once every component it was asked about is set up, so asking
    # about all of them at once would let the first references request outlast its bound
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    _record_opened_files(ls)
    requested: list[str] = []
    requested_before_a_answered: list[str] = []

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if relative_path == "a.hs":
            time.sleep(0.2)
            requested_before_a_answered.extend(requested)

    _answer_requests(ls, answer)
    ls._ensure_hls_components_loaded()
    assert requested_before_a_answered == ["a.hs"]
    assert requested == ["a.hs", "b.hs"]


def test_loading_stops_at_the_deadline_and_is_not_repeated(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs", "c.hs"], monkeypatch)
    opened, open_now = _record_opened_files(ls)
    answer_b = threading.Event()
    requested: list[str] = []

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if relative_path == "b.hs":
            answer_b.wait(timeout=10)

    _answer_requests(ls, answer)
    assert ls._HLS_LOAD_TIMEOUT_SECONDS <= _timed(ls._ensure_hls_components_loaded) < ls._HLS_LOAD_TIMEOUT_SECONDS + 0.5
    assert not open_now
    # at most one component is still being set up, and opening files again would abort HLS's work on it
    assert _timed(ls._ensure_hls_components_loaded) < 0.1
    answer_b.set()
    assert opened == ["a.hs", "b.hs"]
    assert requested == ["a.hs", "b.hs"]


def test_a_failed_request_does_not_stop_the_other_components(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs", "c.hs"], monkeypatch)
    _record_opened_files(ls)
    requested: list[str] = []

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if relative_path == "b.hs":
            raise RuntimeError("HLS could not set up the component")

    _answer_requests(ls, answer)
    ls._ensure_hls_components_loaded()
    assert requested == ["a.hs", "b.hs", "c.hs"]


def test_a_file_that_cannot_be_opened_is_not_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "gone.hs", "b.hs"], monkeypatch)
    opened, _ = _record_opened_files(ls, unopenable=frozenset({"gone.hs"}))
    requested: list[str] = []
    _answer_requests(ls, requested.append)
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs"] * 2
    assert requested == ["a.hs", "b.hs"]


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
    _record_opened_files(ls)
    ls._on_hls_progress({"token": "busy", "value": {"kind": "begin"}})  # every wait runs to its bound
    # without the bounds holding, the first request would wait a further query wait
    first = _timed(lambda: ls._send_references_request("a.hs", 0, 0))
    assert ls._HLS_LOAD_TIMEOUT_SECONDS <= first < ls._HLS_LOAD_TIMEOUT_SECONDS + 0.5
    later = _timed(lambda: ls._send_references_request("a.hs", 0, 0))
    assert ls._HLS_QUERY_WAIT_SECONDS <= later < ls._HLS_QUERY_WAIT_SECONDS + 0.5


def test_slow_answers_and_the_idle_wait_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    _record_opened_files(ls)
    _answer_requests(ls, lambda relative_path: time.sleep(0.8))
    ls._on_hls_progress({"token": "busy", "value": {"kind": "begin"}})
    assert _timed(ls._ensure_hls_components_loaded) < ls._HLS_LOAD_TIMEOUT_SECONDS + 0.5


def test_a_query_after_the_load_waits_for_reported_work(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    _record_opened_files(ls)
    ls._send_references_request("a.hs", 0, 0)
    assert ls._hls_components_loaded
    ls._on_hls_progress({"token": "edit", "value": {"kind": "begin"}})
    assert _timed(lambda: ls._send_references_request("a.hs", 0, 0)) >= ls._HLS_QUERY_WAIT_SECONDS


def test_a_file_gone_before_it_is_reopened_does_not_fail_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    opens: list[str] = []

    @contextmanager
    def open_file(relative_path: str) -> Iterator[None]:
        opens.append(relative_path)
        if relative_path == "b.hs" and opens.count("b.hs") == 2:
            raise FileNotFoundError(relative_path)
        yield

    ls.open_file = open_file  # type: ignore
    ls._send_references_request("a.hs", 0, 0)
    assert opens == ["a.hs", "b.hs", "a.hs", "b.hs"]
