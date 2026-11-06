"""How long a references request waits for HLS to load the project's components. These tests need no HLS."""

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
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
    ls._hls_unvisited = []
    ls._hls_pending = None
    ls._hls_given_up = None
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


def _record_opened_files(
    ls: HaskellLanguageServer, unopenable: frozenset[str] = frozenset(), after_open: Callable[[list[str]], object] = lambda opened: None
) -> tuple[list[str], set[str]]:
    """Replaces `open_file`; returns every file opened, in order, and the files open right now."""
    opened: list[str] = []
    open_now: set[str] = set()

    @contextmanager
    def open_file(relative_path: str) -> Iterator[None]:
        if relative_path in unopenable:
            raise FileNotFoundError(relative_path)
        opened.append(relative_path)
        open_now.add(relative_path)
        after_open(opened)
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


def test_the_quiet_period_starts_when_work_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(list, monkeypatch)
    ls._HLS_QUIET_SECONDS = 0.4
    for finished in ({"kind": "end"}, {"kind": "report", "percentage": 100}):
        ls._on_hls_progress({"token": 1, "value": {"kind": "begin"}})
        time.sleep(0.5)  # longer than the quiet period, so only the work ending can start it
        ls._on_hls_progress({"token": 1, "value": finished})
        assert _timed(lambda: ls._hls_wait_idle(timeout_seconds=2.0)) >= 0.4


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
    open_when_work_ended: list[set[str]] = []

    def after_open(opened: list[str]) -> None:
        if len(opened) == 4:  # both files reopened: HLS goes on typechecking, reporting it as progress
            ls._on_hls_progress({"token": "typecheck", "value": {"kind": "begin"}})

            def finish() -> None:
                open_when_work_ended.append(set(open_now))
                ls._on_hls_progress({"token": "typecheck", "value": {"kind": "end"}})

            threading.Timer(0.3, finish).start()

    opened, open_now = _record_opened_files(ls, after_open=after_open)
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs"] * 2
    assert open_when_work_ended == [{"a.hs", "b.hs"}]
    assert not open_now
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs"] * 2, "the load is not repeated"


def test_loading_waits_through_the_pause_before_hls_reports_more_work(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    ls._HLS_LOAD_TIMEOUT_SECONDS = 3.0
    ls._HLS_QUIET_SECONDS = 0.5  # far longer than the pause, so that a late timer cannot end the wait
    ls._hls_last_progress = time.monotonic() - 10  # HLS last reported anything long ago
    open_when_work_ended: list[set[str]] = []

    def finish() -> None:
        open_when_work_ended.append(set(open_now))
        ls._on_hls_progress({"token": "typecheck", "value": {"kind": "end"}})

    def after_open(opened: list[str]) -> None:
        if len(opened) == 2:  # reopened: HLS reports the work this starts only a moment later
            threading.Timer(0.05, lambda: ls._on_hls_progress({"token": "typecheck", "value": {"kind": "begin"}})).start()
            threading.Timer(1.0, finish).start()

    _, open_now = _record_opened_files(ls, after_open=after_open)
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


def test_a_failed_request_does_not_stop_the_other_components(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
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
    assert "HLS could not load b.hs" in caplog.text


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
    # the first request: the load, then the query wait for HLS's indexing, which the deadline cut short
    first = _timed(lambda: ls._send_references_request("a.hs", 0, 0))
    bound = ls._HLS_LOAD_TIMEOUT_SECONDS + ls._HLS_QUERY_WAIT_SECONDS
    assert bound <= first < bound + 0.5
    later = _timed(lambda: ls._send_references_request("a.hs", 0, 0))
    assert ls._HLS_QUERY_WAIT_SECONDS <= later < ls._HLS_QUERY_WAIT_SECONDS + 0.5


def test_a_load_that_finished_adds_no_query_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    _record_opened_files(ls)
    assert _timed(lambda: ls._send_references_request("a.hs", 0, 0)) < ls._HLS_QUERY_WAIT_SECONDS


def test_slow_answers_and_the_idle_wait_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    _record_opened_files(ls)
    _answer_requests(ls, lambda relative_path: time.sleep(0.8))
    ls._on_hls_progress({"token": "busy", "value": {"kind": "begin"}})
    # the deadline, plus the query wait spent with the files open; not the whole bound again after the answer
    assert _timed(ls._ensure_hls_components_loaded) < ls._HLS_LOAD_TIMEOUT_SECONDS + ls._HLS_QUERY_WAIT_SECONDS + 0.4


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


def test_a_request_given_up_on_ends_the_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # HLS is still setting the component up, so asking about another would queue a second setup
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    opened, _ = _record_opened_files(ls)

    def answer(relative_path: str) -> None:
        raise TimeoutError("Request timed out")

    _answer_requests(ls, answer)
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs"]


def test_reopening_stops_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs", "c.hs"], monkeypatch)
    # reopening a outlasts the whole deadline, so b and c must not be reopened however fast phase 1 was
    opened, _ = _record_opened_files(ls, after_open=lambda opened: time.sleep(1.2) if len(opened) == 4 else None)
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs", "c.hs", "a.hs"]


def test_files_stay_open_through_the_query_wait_after_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    # closing them at the deadline would restart the work being waited for, and HLS would not report it
    ls = _server(lambda: ["a.hs"], monkeypatch)
    open_when_work_ended: list[set[str]] = []

    def finish() -> None:
        open_when_work_ended.append(set(open_now))
        ls._on_hls_progress({"token": "splice", "value": {"kind": "end"}})

    def after_open(opened: list[str]) -> None:
        if len(opened) == 2:  # reopened: work that outlasts the 1.0 s deadline but not the query wait after it
            ls._on_hls_progress({"token": "splice", "value": {"kind": "begin"}})
            threading.Timer(1.3, finish).start()

    _, open_now = _record_opened_files(ls, after_open=after_open)
    elapsed = _timed(lambda: ls._send_references_request("a.hs", 0, 0))
    assert open_when_work_ended == [{"a.hs"}]
    assert elapsed < ls._HLS_LOAD_TIMEOUT_SECONDS + ls._HLS_QUERY_WAIT_SECONDS + 0.5


def test_the_first_query_waits_for_indexing_after_the_load(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    _record_opened_files(ls)
    busy_when_sent: list[bool] = []
    monkeypatch.setattr(
        SolidLanguageServer, "_send_references_request", lambda self, *args: busy_when_sent.append(bool(self._hls_active_progress)) or []
    )

    def answer(relative_path: str) -> None:  # indexing that outlasts the load's deadline, not the query wait
        ls._on_hls_progress({"token": "index", "value": {"kind": "begin"}})
        end = {"token": "index", "value": {"kind": "end"}}
        threading.Timer(ls._HLS_LOAD_TIMEOUT_SECONDS + 0.3, lambda: ls._on_hls_progress(end)).start()

    _answer_requests(ls, answer)
    ls._send_references_request("a.hs", 0, 0)
    assert busy_when_sent == [False]


def test_the_query_wait_is_its_own_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    ls._HLS_QUERY_WAIT_SECONDS = 0.3
    _record_opened_files(ls)
    ls._send_references_request("a.hs", 0, 0)
    ls._on_hls_progress({"token": "busy", "value": {"kind": "begin"}})
    assert 0.3 <= _timed(lambda: ls._send_references_request("a.hs", 0, 0)) < 0.8


def test_files_are_not_reopened_while_a_component_is_still_being_set_up(monkeypatch: pytest.MonkeyPatch) -> None:
    # reopening them would restart HLS's work on it
    ls = _server(lambda: ["a.hs"], monkeypatch)
    opened, _ = _record_opened_files(ls)
    answered = threading.Event()
    _answer_requests(ls, lambda relative_path: answered.wait(timeout=10))
    ls._ensure_hls_components_loaded()
    answered.set()
    assert opened == ["a.hs"]


def test_slow_answers_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    _record_opened_files(ls)
    answered = threading.Event()
    _answer_requests(ls, lambda relative_path: time.sleep(0.8) if relative_path == "a.hs" else answered.wait(timeout=10))
    try:
        assert _timed(ls._ensure_hls_components_loaded) < ls._HLS_LOAD_TIMEOUT_SECONDS + 0.5
    finally:
        answered.set()


def test_the_load_runs_once_for_concurrent_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    def component_files() -> list[str]:
        time.sleep(0.2)
        return ["a.hs"]

    ls = _server(component_files, monkeypatch)
    opened, _ = _record_opened_files(ls)
    threads = [threading.Thread(target=ls._ensure_hls_components_loaded) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert opened == ["a.hs"] * 2


def test_a_file_that_cannot_be_opened_is_not_tried_again(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "gone.hs", "b.hs"], monkeypatch)
    _record_opened_files(ls, unopenable=frozenset({"gone.hs"}))
    attempts: list[str] = []
    recording_open = ls.open_file

    def open_file(relative_path: str) -> AbstractContextManager[None]:
        attempts.append(relative_path)
        return recording_open(relative_path)

    ls.open_file = open_file  # type: ignore
    ls._ensure_hls_components_loaded()
    assert attempts == ["a.hs", "gone.hs", "b.hs", "a.hs", "b.hs"]


def test_a_file_gone_before_it_is_reopened_leaves_the_others_open(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["gone.hs", "b.hs"], monkeypatch)
    ls._HLS_LOAD_TIMEOUT_SECONDS = 3.0
    opens: list[str] = []
    open_now: set[str] = set()
    open_when_work_ended: list[set[str]] = []

    def finish() -> None:
        open_when_work_ended.append(set(open_now))
        ls._on_hls_progress({"token": "b", "value": {"kind": "end"}})

    @contextmanager
    def open_file(relative_path: str) -> Iterator[None]:
        opens.append(relative_path)
        if relative_path == "gone.hs" and opens.count("gone.hs") == 2:
            raise FileNotFoundError(relative_path)
        open_now.add(relative_path)
        if relative_path == "b.hs" and opens.count("b.hs") == 2:  # reopened after the gone file failed
            ls._on_hls_progress({"token": "b", "value": {"kind": "begin"}})
            threading.Timer(0.3, finish).start()
        try:
            yield
        finally:
            open_now.discard(relative_path)

    ls.open_file = open_file  # type: ignore
    ls._send_references_request("gone.hs", 0, 0)
    assert opens == ["gone.hs", "b.hs", "gone.hs", "b.hs"]
    assert open_when_work_ended == [{"b.hs"}]


def test_a_slow_component_does_not_keep_the_later_ones_unloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs", "c.hs"], monkeypatch)
    opened, _ = _record_opened_files(ls)
    answer_b = threading.Event()
    requested: list[str] = []

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if relative_path == "b.hs":
            answer_b.wait(timeout=10)

    _answer_requests(ls, answer)
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs"]
    # while b's component is still being set up, asking about c would queue a second setup
    assert not ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs"]
    answer_b.set()
    assert ls._hls_pending is not None
    ls._hls_pending.join(timeout=5)
    assert ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs", "c.hs", "c.hs"], "only the file never opened, then reopened until idle"
    assert requested == ["a.hs", "b.hs", "c.hs"]
    assert not ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs", "c.hs", "c.hs"]


def test_a_request_given_up_on_is_asked_again_when_the_load_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    opened, _ = _record_opened_files(ls)
    requested: list[str] = []

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if requested == ["a.hs"]:
            raise TimeoutError("Request timed out")

    _answer_requests(ls, answer)
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs"]
    ls._ensure_hls_components_loaded()
    assert requested == ["a.hs", "a.hs", "b.hs"]
    # a is asked about with its file left closed: reopening it would restart its component's setup
    assert opened == ["a.hs", "b.hs", "b.hs"]


def test_a_resumed_load_waits_no_longer_than_a_query(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs", "c.hs"], monkeypatch)
    ls._HLS_LOAD_TIMEOUT_SECONDS = 2.0  # far from the query wait, which bounds a resumed load
    _record_opened_files(ls)
    released = threading.Event()
    _answer_requests(ls, lambda relative_path: released.wait(timeout=10) if relative_path != "a.hs" else None)
    try:
        ls._send_references_request("a.hs", 0, 0)
        released.set()  # b answers; c, asked about on the next request, is slow too
        assert ls._hls_pending is not None
        ls._hls_pending.join(timeout=5)
        released.clear()
        elapsed = _timed(lambda: ls._send_references_request("a.hs", 0, 0))
        assert ls._HLS_QUERY_WAIT_SECONDS <= elapsed < ls._HLS_QUERY_WAIT_SECONDS + 0.5
    finally:
        released.set()


def test_the_others_wait_while_a_request_asked_again_is_still_unanswered(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    opened, _ = _record_opened_files(ls)
    setup_done = threading.Event()
    requested: list[str] = []

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if requested == ["a.hs"]:
            raise TimeoutError("Request timed out")
        if relative_path == "a.hs":
            setup_done.wait(timeout=10)

    _answer_requests(ls, answer)
    try:
        ls._ensure_hls_components_loaded()
        assert ls._ensure_hls_components_loaded()  # asks about a again, which waits out the query bound
        assert opened == ["a.hs"], "b must wait for a's setup"
    finally:
        setup_done.set()
    assert ls._hls_pending is not None
    ls._hls_pending.join(timeout=5)
    ls._ensure_hls_components_loaded()
    assert opened == ["a.hs", "b.hs", "b.hs"]


def test_a_timeout_after_the_deadline_is_asked_again_before_the_next_component(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs", "b.hs"], monkeypatch)
    opened, _ = _record_opened_files(ls)
    give_up = threading.Event()
    requested: list[str] = []

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if requested == ["a.hs"]:
            give_up.wait(timeout=10)  # still running at the deadline; the client gives up on it later
            raise TimeoutError("Request timed out")

    _answer_requests(ls, answer)
    ls._ensure_hls_components_loaded()
    give_up.set()
    assert ls._hls_pending is not None
    ls._hls_pending.join(timeout=5)
    ls._ensure_hls_components_loaded()
    assert requested == ["a.hs", "a.hs", "b.hs"], "a is asked again, with its file closed, before b is opened"
    assert opened == ["a.hs", "b.hs", "b.hs"]


def test_a_resumed_load_waits_for_reported_work(monkeypatch: pytest.MonkeyPatch) -> None:
    ls = _server(lambda: ["a.hs"], monkeypatch)
    _record_opened_files(ls)
    busy_when_sent: list[bool] = []
    monkeypatch.setattr(
        SolidLanguageServer, "_send_references_request", lambda self, *args: busy_when_sent.append(bool(self._hls_active_progress)) or []
    )
    requested: list[str] = []

    def answer(relative_path: str) -> None:
        requested.append(relative_path)
        if len(requested) == 1:
            raise TimeoutError("Request timed out")
        ls._on_hls_progress({"token": "index", "value": {"kind": "begin"}})  # the answer to the second ask
        threading.Timer(0.3, lambda: ls._on_hls_progress({"token": "index", "value": {"kind": "end"}})).start()

    _answer_requests(ls, answer)
    ls._send_references_request("a.hs", 0, 0)
    ls._send_references_request("a.hs", 0, 0)
    assert requested == ["a.hs", "a.hs"]
    assert busy_when_sent == [False, False]
