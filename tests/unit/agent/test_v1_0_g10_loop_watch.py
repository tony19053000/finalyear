"""V1.0 G10 (D444): the agent names its own event-loop stalls.

Lab L3 (2026-11-10, 0.7 GiB free RAM): the controller expired the session
``lease_expired`` twice during a long job and once the stream ended
``INTERNAL "Internal error from Core"``. These tests pin the evidence the agent
now logs so the next run tells "this laptop did not run" from a link fault:
``agent.loop.stalled`` lines, and ``loop_lag_max_s`` / ``write_failed`` /
``core_error`` on ``agent.session.stream_ended``.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import grpc
import pytest
import structlog

from meshforge.agent.identity import load_or_create
from meshforge.agent.loop_watch import LoopWatch, available_memory_mb
from meshforge.agent.session.client import (
    CORE_ERROR_MAX_CHARS,
    CORE_INTERNAL_DETAIL,
    STALL_WINDOW_S,
    AgentSessionClient,
    StreamEnd,
    write_failed,
)
from meshforge.agent.session.policy import Backoff, BackoffPolicy, StreamEndAction
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSettings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


def _watch(**kw: object) -> LoopWatch:
    args: dict[str, object] = {"clock": lambda: 0.0, "memory_mb": lambda: 512}
    args.update(kw)
    return LoopWatch(**args)  # type: ignore[arg-type]


# ------------------------------------------------------------------ LoopWatch


def test_an_on_time_wake_up_is_not_a_stall() -> None:
    watch = _watch(threshold_s=1.0)
    with structlog.testing.capture_logs() as logs:
        assert watch.observe(10.0, 10.0) is None
        assert watch.observe(10.0, 11.0) is None  # exactly the threshold: not late
    assert watch.stalls == 0
    assert not logs


def test_a_late_wake_up_is_logged_with_lag_and_free_memory() -> None:
    watch = _watch(threshold_s=1.0, node_id="node_x")
    with structlog.testing.capture_logs() as logs:
        assert watch.observe(10.0, 17.5) == pytest.approx(7.5)
    (line,) = logs
    assert line["event"] == "agent.loop.stalled"
    assert line["log_level"] == "warning"
    assert line["lag_s"] == 7.5
    assert line["available_mb"] == 512
    assert line["node_id"] == "node_x"
    assert line["suppressed"] == 0
    assert watch.stalls == 1
    assert watch.worst_s == pytest.approx(7.5)


def test_stalls_in_a_burst_are_counted_into_the_next_line() -> None:
    watch = _watch(threshold_s=0.5, log_every_s=10.0)
    with structlog.testing.capture_logs() as logs:
        watch.observe(0.0, 1.0)  # logged
        watch.observe(2.0, 3.0)  # suppressed
        watch.observe(4.0, 7.0)  # suppressed, the worst
        watch.observe(12.0, 13.0)  # logged, names the two before
    assert [e["event"] for e in logs] == ["agent.loop.stalled"] * 2
    assert logs[1]["suppressed"] == 2
    assert logs[1]["worst_suppressed_s"] == 3.0
    assert watch.stalls == 4


def test_max_lag_covers_only_the_window() -> None:
    watch = _watch(threshold_s=0.5)
    watch.observe(0.0, 5.0)  # a 5 s stall that ended at t=5
    watch.observe(20.0, 21.0)  # a 1 s stall that ended at t=21
    assert watch.max_lag_s(30.0, now=30.0) == pytest.approx(5.0)
    assert watch.max_lag_s(10.0, now=30.0) == pytest.approx(1.0)
    assert watch.max_lag_s(5.0, now=30.0) == 0.0


def test_max_lag_uses_the_clock_by_default() -> None:
    now = [100.0]
    watch = _watch(threshold_s=0.5, clock=lambda: now[0])
    watch.observe(90.0, 98.0)
    assert watch.max_lag_s(5.0) == pytest.approx(8.0)
    now[0] = 200.0
    assert watch.max_lag_s(5.0) == 0.0


@pytest.mark.parametrize(
    "kw",
    [{"interval_s": 0.0}, {"threshold_s": 0.0}, {"log_every_s": -1.0}],
)
def test_bad_settings_are_refused(kw: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="must be"):
        _watch(**kw)


def test_available_memory_is_a_positive_number_or_none() -> None:
    mb = available_memory_mb()
    assert mb is None or mb > 0


@pytest.mark.asyncio
async def test_a_blocked_event_loop_is_caught_by_the_running_watch() -> None:
    """Real loop, real block: what a paged-out or blocked agent looks like."""
    loop = asyncio.get_running_loop()
    watch = LoopWatch(
        clock=loop.time, interval_s=0.02, threshold_s=0.15, memory_mb=lambda: None
    )
    task = asyncio.create_task(watch.run())
    try:
        await asyncio.sleep(0.1)  # a few on-time ticks first
        assert watch.stalls == 0
        time.sleep(0.4)  # noqa: ASYNC251 - the stall under test
        await asyncio.sleep(0.1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert watch.stalls >= 1
    assert watch.worst_s >= 0.2
    assert watch.max_lag_s(5.0) >= 0.2


# ------------------------------------------------- stream_ended carries the cause


@pytest.fixture
def client(tmp_path: Path) -> AgentSessionClient:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data", agent={"controller_address": "127.0.0.1:1"}
    )
    c = AgentSessionClient(settings, load_or_create(settings.identity_dir))
    c._backoff = Backoff(BackoffPolicy(0.1, 1.0, 60.0), clock=lambda: 0.0)
    c.state = AgentState.ONLINE
    c.session_id = "sess_x"
    return c


def test_write_failed_is_only_the_local_core_status() -> None:
    action = StreamEndAction.RECONNECT
    code = grpc.StatusCode
    assert write_failed(StreamEnd(code.INTERNAL, CORE_INTERNAL_DETAIL, action))
    assert not write_failed(StreamEnd(code.INTERNAL, "boom", action))
    assert not write_failed(StreamEnd(code.ABORTED, "lease_expired", action))
    assert not write_failed(StreamEnd(None, "", action))


@pytest.mark.asyncio
async def test_a_masked_lease_end_is_flagged_with_the_loop_lag(
    client: AgentSessionClient,
) -> None:
    asked: list[float] = []

    def lag(window: float) -> float:
        asked.append(window)
        return 7.25

    client.loop_lag = lag
    end = client._stream_end(
        grpc.StatusCode.INTERNAL, CORE_INTERNAL_DETAIL, core_error="Failed batch"
    )
    assert end.action is StreamEndAction.RECONNECT  # still a plain reconnect
    with structlog.testing.capture_logs() as logs:
        await client._finish_stream(end)
    (line,) = [e for e in logs if e["event"] == "agent.session.stream_ended"]
    assert line["write_failed"] is True
    assert line["core_error"] == "Failed batch"
    assert line["loop_lag_max_s"] == 7.25
    assert asked == [STALL_WINDOW_S]
    assert client.state is AgentState.BACKOFF


@pytest.mark.asyncio
async def test_an_ordinary_end_logs_no_extra_keys_when_unwired(
    client: AgentSessionClient,
) -> None:
    end = client._stream_end(grpc.StatusCode.ABORTED, "lease_expired")
    with structlog.testing.capture_logs() as logs:
        await client._finish_stream(end)
    (line,) = [e for e in logs if e["event"] == "agent.session.stream_ended"]
    assert line["details"] == "lease_expired"
    for key in ("write_failed", "core_error", "loop_lag_max_s"):
        assert key not in line


class _FailedCall:
    def __init__(self, debug: str) -> None:
        self._debug = debug

    async def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNAVAILABLE

    async def details(self) -> str:
        return "Socket closed"

    async def debug_error_string(self) -> str:
        return self._debug


@pytest.mark.asyncio
async def test_status_of_keeps_a_bounded_core_error(
    client: AgentSessionClient,
) -> None:
    end = await client._status_of(_FailedCall("x" * 5000))
    assert end.code is grpc.StatusCode.UNAVAILABLE
    assert len(end.core_error) == CORE_ERROR_MAX_CHARS
