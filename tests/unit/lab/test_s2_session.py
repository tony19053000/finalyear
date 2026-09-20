"""Unit tests for the S2 session-call bookkeeping in ``lab/spikes/_s2/client.py``
(roadmap 0.2.6, reviewer BLOCKING 2).

``alive_s`` / ``idle_survived_s`` of a dropped session must be taken from the
moment the stream was *seen* to die, never from when the final ping gave up
after a full sleep plus timeout; and when nothing ever reported the death they
must be ``None`` (unknown), not a made-up number. ``_SessionCall`` is driven
here with a fake channel so no socket, no gRPC server and no clock-dependent
sleep is involved; the harness summary is checked to skip ``None`` honestly.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import grpc
import pytest

from spikes._harness.reps import CaseResult
from spikes._s2 import client as probe
from spikes._s2.protocol import FailureType, ProbeError, encode

# --------------------------------------------------------------- idle_outcome


def test_idle_outcome_unknown_death_time_is_none_not_a_number() -> None:
    assert probe.idle_outcome(None, started=0.0, idle_start=5.0, idle_s=30.0) == (
        None,
        None,
    )


def test_idle_outcome_death_inside_the_gap_measures_from_the_death() -> None:
    alive, idle = probe.idle_outcome(17.0, started=2.0, idle_start=5.0, idle_s=30.0)
    assert alive == pytest.approx(15.0)
    assert idle == pytest.approx(12.0)


def test_idle_outcome_is_clamped_to_the_gap() -> None:
    _, idle = probe.idle_outcome(100.0, started=0.0, idle_start=5.0, idle_s=30.0)
    assert idle == 30.0
    _, idle = probe.idle_outcome(4.0, started=0.0, idle_start=5.0, idle_s=30.0)
    assert idle == 0.0


def test_idle_outcome_never_negative() -> None:
    alive, idle = probe.idle_outcome(1.0, started=2.0, idle_start=5.0, idle_s=30.0)
    assert alive == 0.0 and idle == 0.0


# --------------------------------------------------------- _SessionCall clock
class _FakeRpcError(grpc.RpcError):
    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNAVAILABLE

    def details(self) -> str:
        return "Socket closed"

    def debug_error_string(self) -> str:
        return ""


class _FakeResponses:
    """What ``stream_stream(...)(requests, timeout=...)`` returns: an iterator of
    reply bodies that can be told to die, plus ``cancel()``."""

    def __init__(self) -> None:
        self.items: list[bytes] = []
        self.release = threading.Event()
        self.fail_with: BaseException | None = None
        self.cancelled = False

    def __iter__(self) -> Iterator[bytes]:
        yield from self.items
        self.release.wait(timeout=5.0)
        if self.fail_with is not None:
            raise self.fail_with

    def cancel(self) -> None:
        self.cancelled = True
        self.release.set()


class _FakeChannel:
    def __init__(self, responses: _FakeResponses) -> None:
        self.responses = responses
        self.timeout: float | None = None

    def stream_stream(self, method: str, **_kw: Any) -> Any:
        def call(requests: Iterator[bytes], timeout: float) -> _FakeResponses:
            self.timeout = timeout
            return self.responses

        return call


def _wait(predicate: Any, timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), "condition not met in time"


def test_failed_at_is_none_while_the_stream_is_alive() -> None:
    responses = _FakeResponses()
    responses.items = [encode({"op": "hello_ack"})]
    call = probe._SessionCall(_FakeChannel(responses), budget_s=10.0)  # type: ignore[arg-type]
    try:
        call.wait_ack("hello_ack", 5.0)
        assert call.failed_at is None
        assert call.error is None
    finally:
        responses.release.set()
        call.close()


def test_rpc_error_in_the_reader_stamps_failed_at_at_the_error() -> None:
    responses = _FakeResponses()
    call = probe._SessionCall(_FakeChannel(responses), budget_s=10.0)  # type: ignore[arg-type]
    try:
        before = time.perf_counter()
        responses.fail_with = _FakeRpcError()
        responses.release.set()
        _wait(lambda: call.failed_at is not None)
        after = time.perf_counter()
        assert call.error is not None
        assert call.failed_at is not None
        assert before <= call.failed_at <= after
        with pytest.raises(ProbeError):
            call.wait_pong(1, 5.0)
    finally:
        call.close()


def test_clean_end_of_stream_stamps_failed_at_without_an_error() -> None:
    """The server ending the stream cleanly (its hold cap) is a death too:
    ``wait_pong`` reports ``stream_dropped`` and the time is the end of the
    iterator, not the end of the waiter's timeout."""
    responses = _FakeResponses()
    call = probe._SessionCall(_FakeChannel(responses), budget_s=10.0)  # type: ignore[arg-type]
    try:
        responses.release.set()  # iterator ends: no error raised
        _wait(lambda: call.failed_at is not None)
        stamped = call.failed_at
        assert call.error is None
        waited = time.perf_counter()
        with pytest.raises(ProbeError) as info:
            call.wait_pong(7, 2.0)
        assert info.value.failure_type == FailureType.STREAM_DROPPED
        # the stamp did not move while the waiter was (not) waiting
        assert call.failed_at == stamped
        assert stamped is not None and stamped <= waited
    finally:
        call.close()


def test_failed_at_is_stamped_once_and_never_overwritten() -> None:
    responses = _FakeResponses()
    call = probe._SessionCall(_FakeChannel(responses), budget_s=10.0)  # type: ignore[arg-type]
    try:
        call._fail(ProbeError(FailureType.TIMEOUT, "first"))
        first = call.failed_at
        time.sleep(0.01)
        call._fail(ProbeError(FailureType.TIMEOUT, "second"))
        assert call.failed_at == first
        assert call.error is not None and str(call.error) == "first"
    finally:
        responses.release.set()
        call.close()


def test_a_plain_timeout_leaves_failed_at_unknown() -> None:
    """No error, iterator still open: the ping just never got a reply. The
    stream never said it died, so there is no death time to report."""
    responses = _FakeResponses()
    call = probe._SessionCall(_FakeChannel(responses), budget_s=10.0)  # type: ignore[arg-type]
    try:
        with pytest.raises(ProbeError) as info:
            call.wait_pong(1, 0.05)
        assert info.value.failure_type == FailureType.TIMEOUT
        assert call.failed_at is None
        assert probe.idle_outcome(
            call.failed_at, started=call.started, idle_start=0.0, idle_s=1.0
        ) == (None, None)
    finally:
        responses.release.set()
        call.close()


# ------------------------------------------------- harness handles None samples
def test_summary_skips_none_metric_values_and_reports_the_smaller_n() -> None:
    case = CaseResult(
        case_id="grpc-bidi-idle3s-kaoff",
        label="bidi",
        metrics={"alive_s": "s", "idle_survived_s": "s", "survived": "0/1"},
        requested_reps=5,
        samples=[
            {"survived": 1, "alive_s": 4.0, "idle_survived_s": 3.0},
            {"survived": 0, "alive_s": None, "idle_survived_s": None},
            {"survived": 0, "alive_s": 1.5, "idle_survived_s": 0.5},
        ],
    )
    summary = case.to_json()["summary"]
    assert summary["survived"]["n"] == 3
    assert summary["alive_s"]["n"] == 2
    assert summary["idle_survived_s"]["n"] == 2
    assert summary["idle_survived_s"]["mean"] == pytest.approx(1.75)
