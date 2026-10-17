"""V0.9 G1 item 1 (D213): per-node clock offset from Ping/Pong exchanges.

The controller is the local side, the agent the remote: ``sent`` = the
controller's wall clock at the Ping, ``remote`` = the Pong envelope's
``sent_at``, ``received`` = ``sent`` + the monotonic RTT.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from meshforge.common.ids import new_node_id, new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LeaseTracker, LivenessConfig, NodeState
from meshforge.controller.session.clocks import (
    ALERT_BOUND_MS,
    MIN_EXCHANGES,
    UNKNOWN_RESOLUTION_MS,
    NodeClock,
    resolution_ms,
)
from meshforge.controller.session.heartbeats import HeartbeatRecorder
from meshforge.controller.session.live import LiveSession, SeenIds
from meshforge.controller.session.writer import WriteChain

pytestmark = pytest.mark.unit

WALL0 = 1_792_238_400.0  # 2026-10-17 12:00 UTC, epoch s


class _Registry:
    def __init__(self) -> None:
        self.rtt: list[dict[str, Any]] = []
        self.alerts: list[dict[str, Any]] = []

    async def record_rtt(self, session_id: str, **kwargs: Any) -> None:
        self.rtt.append({"session_id": session_id, **kwargs})

    async def record_clock_alert(self, node_id: str, **kwargs: Any) -> int:
        self.alerts.append({"node_id": node_id, **kwargs})
        return 41 + len(self.alerts)


class _Clocks:
    def __init__(self) -> None:
        self.mono = 100.0
        self.wall = WALL0

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


def _live() -> LiveSession:
    """A session whose write chain runs (call inside the test's loop)."""
    node_id, session_id = str(new_node_id()), str(new_session_id())
    live = LiveSession(
        node_id=node_id,
        session_id=session_id,
        session_epoch=1,
        peer_address="ipv4:127.0.0.1:1",
        tracker=LeaseTracker(LivenessConfig(), lambda: 0.0),
        state=NodeState.ONLINE,
        outbound=asyncio.Queue(),
        seen=SeenIds(4),
        next_ping_at=0.0,
        writer=WriteChain(node_id=node_id, session_id=session_id),
    )
    live.writer.start()
    return live


def _pong(ping_id: str, agent_wall_s: float | None, resolution_us: int = 1) -> Any:
    msg = agent_pb2.AgentMessage(
        pong=agent_pb2.Pong(ping_id=ping_id, clock_resolution_us=resolution_us)
    )
    if agent_wall_s is not None:
        msg.sent_at.FromNanoseconds(round(agent_wall_s * 1e9))
    return msg


async def _exchange(
    rec: HeartbeatRecorder,
    live: LiveSession,
    clocks: _Clocks,
    *,
    skew_s: float,
    out_s: float,
    back_s: float,
    resolution_us: int = 1,
    with_time: bool = True,
) -> None:
    ping = rec.ping(live, clocks.mono)
    clocks.advance(out_s)
    agent_now = clocks.wall + skew_s
    clocks.advance(back_s)
    await rec.on_pong_message(
        live, _pong(ping.ping.ping_id, agent_now if with_time else None, resolution_us)
    )


def _recorder(registry: _Registry, clocks: _Clocks) -> HeartbeatRecorder:
    return HeartbeatRecorder(
        registry,  # type: ignore[arg-type]
        LivenessConfig(),
        clock=lambda: clocks.mono,
        wall_clock=lambda: clocks.wall,
    )


async def _drain(live: LiveSession) -> None:
    """Every queued write reached the fake registry (the chain then ends)."""
    assert await live.writer.drain(2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("skew_s", [0.0, 5.0, -5.0, 3600.0])
async def test_a_skewed_agent_clock_is_measured_within_its_bound(skew_s: float) -> None:
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    for out_s, back_s in ((0.004, 0.006), (0.002, 0.002), (0.010, 0.001)):
        await _exchange(rec, live, clocks, skew_s=skew_s, out_s=out_s, back_s=back_s)
        clocks.advance(6.0)
    clock = live.clock.latest
    assert clock is not None and clock.consistent and clock.exchanges == 3
    assert abs(clock.offset_ms - skew_s * 1000.0) <= clock.bound_ms + 1e-6
    assert clock.bound_ms == pytest.approx(2.0, abs=0.01)  # shortest RTT 4 ms
    await _drain(live)
    stored = registry.rtt[-1]["clock"]
    assert stored == clock  # the estimate rides on the RTT write


@pytest.mark.asyncio
async def test_a_pong_without_an_agent_time_records_only_the_rtt() -> None:
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    await _exchange(
        rec, live, clocks, skew_s=9.0, out_s=0.01, back_s=0.01, with_time=False
    )
    await _drain(live)
    assert live.clock.latest is None
    assert "clock" not in registry.rtt[-1]
    assert live.last_rtt_ms == pytest.approx(20.0)


@pytest.mark.asyncio
async def test_a_controller_wall_step_mid_exchange_cannot_fake_the_round_trip() -> None:
    """``received`` comes from the monotonic RTT, not a second wall read."""
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    ping = rec.ping(live, clocks.mono)
    clocks.mono += 0.002
    agent_now = clocks.wall + 0.002
    clocks.wall -= 3600.0  # the controller's wall clock steps back an hour
    clocks.mono += 0.002
    await rec.on_pong_message(live, _pong(ping.ping.ping_id, agent_now))
    clock = live.clock.latest
    assert clock is not None
    assert abs(clock.offset_ms) <= clock.bound_ms
    assert clock.rtt_ms == pytest.approx(4.0)


def test_the_agent_resolution_widens_the_bound_and_unknown_is_conservative() -> None:
    assert resolution_ms(0) == UNKNOWN_RESOLUTION_MS
    assert resolution_ms(15_625) == pytest.approx(15.625)
    assert resolution_ms(10**9) == 1000.0  # clamped
    fine, coarse = NodeClock(), NodeClock()
    for clock, tick in ((fine, 0.001), (coarse, 15.625)):
        clock.observe(
            sent_wall_ms=0.0, rtt_ms=2.0, agent_ms=1.0, agent_resolution_ms=tick
        )
    assert fine.latest is not None and coarse.latest is not None
    assert coarse.latest.bound_ms - fine.latest.bound_ms == pytest.approx(
        (15.625 - 0.001) / 2
    )


@pytest.mark.asyncio
async def test_a_slow_link_raises_one_alert_per_episode_and_recovers() -> None:
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    published: list[dict[str, str]] = []
    rec.on_clock_alert = published.append
    slow = ALERT_BOUND_MS / 1000.0 + 0.05  # every RTT > 2 x the limit
    for _ in range(MIN_EXCHANGES + 3):
        await _exchange(rec, live, clocks, skew_s=1.0, out_s=slow, back_s=slow)
        clocks.advance(6.0)
    await _drain(live)
    assert live.clock.uncertain
    assert len(registry.alerts) == 1  # one per episode, not one per Pong
    alert = registry.alerts[0]
    assert alert["node_id"] == live.node_id
    assert alert["bound_ms"] > ALERT_BOUND_MS and alert["consistent"] is True
    assert list(alert["reasons"]) == ["bound_over_limit"]
    assert published == [
        {
            "id": "42",
            "kind": "clock_uncertain",
            "node_id": live.node_id,
            "offset_ms": published[0]["offset_ms"],
            "bound_ms": published[0]["bound_ms"],
            "reasons": "bound_over_limit",
        }
    ]
    assert "session_id" not in published[0]  # D195: no secret-looking key
    # Fast exchanges again: the window's best one is tight -> certain.
    await _exchange(rec, live, clocks, skew_s=1.0, out_s=0.001, back_s=0.001)
    assert not live.clock.uncertain
    assert len(registry.alerts) == 1


@pytest.mark.asyncio
async def test_no_alert_before_min_exchanges() -> None:
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    for _ in range(MIN_EXCHANGES - 1):
        await _exchange(rec, live, clocks, skew_s=0.0, out_s=0.4, back_s=0.4)
        clocks.advance(6.0)
    await _drain(live)
    assert live.clock.latest is not None and live.clock.latest.bound_ms > 250
    assert registry.alerts == [] and not live.clock.uncertain


@pytest.mark.asyncio
async def test_a_clock_step_on_the_agent_is_inconsistent_and_alerted() -> None:
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    for i in range(MIN_EXCHANGES + 1):
        skew = 0.0 if i < 2 else 2.0  # the agent's clock jumps 2 s
        await _exchange(rec, live, clocks, skew_s=skew, out_s=0.002, back_s=0.002)
        clocks.advance(6.0)
    await _drain(live)
    clock = live.clock.latest
    assert clock is not None and not clock.consistent
    assert clock.bound_ms >= 1000.0  # covers both offsets, never tight
    assert [a["reasons"] for a in registry.alerts] == [
        ("inconsistent", "bound_over_limit")
    ]


@pytest.mark.asyncio
async def test_the_window_forgets_a_stale_tight_exchange() -> None:
    """D226: the agent's clock drifts 40 ms after one very tight exchange;
    after the window moved on, the estimate tracks the drift."""
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    await _exchange(rec, live, clocks, skew_s=0.0, out_s=0.0001, back_s=0.0001)
    clocks.advance(400.0)  # older than WINDOW_AGE_S by the next exchange
    for _ in range(3):
        await _exchange(rec, live, clocks, skew_s=0.040, out_s=0.05, back_s=0.05)
        clocks.advance(6.0)
    clock = live.clock.latest
    assert clock is not None and clock.exchanges == 3
    assert abs(clock.offset_ms - 40.0) <= clock.bound_ms


def test_the_agent_reports_its_wall_clock_resolution() -> None:
    import time

    pytest.importorskip("psutil", reason="needs the agent extra")
    from meshforge.agent.session.client import wall_clock_resolution_us

    assert wall_clock_resolution_us() >= 1
    expected = time.get_clock_info("time").resolution * 1e6
    assert wall_clock_resolution_us() >= expected


def test_the_proto_pong_is_backward_compatible() -> None:
    old = agent_pb2.Pong(ping_id="cmd_x").SerializeToString()
    parsed = agent_pb2.Pong.FromString(old)
    assert parsed.clock_resolution_us == 0  # "not reported"


def test_node_detail_shows_the_live_estimate_else_the_stored_one() -> None:
    pytest.importorskip("fastapi", reason="needs the controller extra")
    from meshforge.controller.api.shape import node_clock
    from meshforge.controller.registry import SessionRecord

    assert node_clock(None, None) is None
    stored = SessionRecord(
        session_id="s",
        node_id="n",
        session_epoch=1,
        started_at=dt.datetime(2026, 10, 17, tzinfo=dt.UTC),
        ended_at=None,
        end_reason=None,
        peer_address="ipv4:1.2.3.4:5",
        agent_version="0",
        protocol_version=1,
        last_rtt_ms=None,
        rtt_ewma_ms=None,
        clock_offset_ms=5000.1234,
        clock_offset_bound_ms=300.0001,
        clock_consistent=True,
        clock_exchanges=9,
    )
    shown = node_clock(None, stored)
    assert shown is not None
    assert shown.model_dump() == {
        "clock_offset_ms": 5000.123,
        "bound_ms": 300.001,  # rounded up
        "consistent": True,
        "exchanges": 9,
        "uncertain": True,  # 300 ms > 250 ms
        "live": False,
    }
    live = _live_without_loop()
    live.clock.observe(
        sent_wall_ms=0.0, rtt_ms=2.0, agent_ms=11.0, agent_resolution_ms=0.0
    )
    now = node_clock(live, stored)
    assert now is not None and now.live and now.clock_offset_ms == 10.0
    assert now.bound_ms == 1.0 and not now.uncertain


def _live_without_loop() -> LiveSession:
    node_id, session_id = str(new_node_id()), str(new_session_id())
    return LiveSession(
        node_id=node_id,
        session_id=session_id,
        session_epoch=1,
        peer_address="ipv4:127.0.0.1:1",
        tracker=LeaseTracker(LivenessConfig(), lambda: 0.0),
        state=NodeState.ONLINE,
        outbound=asyncio.Queue(),
        seen=SeenIds(4),
        next_ping_at=0.0,
        writer=WriteChain(node_id=node_id, session_id=session_id),
    )


def test_the_bound_uses_the_coarsest_resolution_in_the_window_until_it_leaves() -> None:
    """T-1 (fix r1): one coarse report widens the bound while its exchange is
    in the window, and stops doing so once the window has moved past it."""
    clock = NodeClock()
    wall_ms = WALL0 * 1000.0

    def feed(i: int, res_ms: float) -> None:
        clock.observe(
            sent_wall_ms=wall_ms + i * 6000.0,
            rtt_ms=2.0,
            agent_ms=wall_ms + i * 6000.0 + 1.0,
            agent_resolution_ms=res_ms,
        )

    feed(0, 600.0)
    for i in range(1, 8):
        feed(i, 0.001)
    assert clock.latest is not None and clock.latest.bound_ms >= 300.0
    assert clock.uncertain
    for i in range(8, 8 + 40):  # > 32 exchanges later the coarse one is gone
        feed(i, 0.001)
    assert len(clock.resolutions) == len(clock.window) == 32
    assert clock.latest.bound_ms < 10.0
    assert not clock.uncertain
