"""V0.9 G1 tester round 1: the Ping/Pong clock exchange through the real
HeartbeatRecorder + write chain (replays, stale pongs, hostile stamps, alert
episodes, old agents, a failing alert write)."""

from __future__ import annotations

import asyncio

import pytest
from test_v09_g1_node_clock import (
    _Clocks,
    _drain,
    _exchange,
    _live,
    _pong,
    _recorder,
    _Registry,
)

from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_a_replayed_pong_is_not_a_second_exchange() -> None:
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    ping = rec.ping(live, clocks.mono)
    clocks.advance(0.004)
    pong = _pong(ping.ping.ping_id, clocks.wall)
    await rec.on_pong_message(live, pong)
    await rec.on_pong_message(live, pong)  # a replay
    await _drain(live)
    assert live.clock.latest is not None and live.clock.latest.exchanges == 1
    assert len(registry.rtt) == 1


@pytest.mark.asyncio
async def test_a_pong_for_a_superseded_ping_is_ignored() -> None:
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    old = rec.ping(live, clocks.mono)
    clocks.advance(6.0)
    rec.ping(live, clocks.mono)  # the old one expired
    await rec.on_pong_message(live, _pong(old.ping.ping_id, clocks.wall))
    await _drain(live)
    assert live.clock.latest is None and registry.rtt == []


@pytest.mark.parametrize(
    "agent_wall_s",
    [-62_135_596_800.0, 0.0, 253_402_300_799.0, 9.2e9],
    ids=["year-1", "epoch", "year-9999", "far-future"],
)
@pytest.mark.asyncio
async def test_extreme_agent_stamps_are_stored_finite_and_alert_at_most_once(
    agent_wall_s: float,
) -> None:
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    for _ in range(12):
        ping = rec.ping(live, clocks.mono)
        clocks.advance(0.004)
        await rec.on_pong_message(live, _pong(ping.ping.ping_id, agent_wall_s))
        clocks.advance(6.0)
    await _drain(live)
    clock = live.clock.latest
    assert clock is not None
    for value in (clock.offset_ms, clock.bound_ms, clock.rtt_ms):
        assert value == value and abs(value) < 1e18  # finite, fits a double column
    assert len(registry.alerts) <= 1


@pytest.mark.asyncio
async def test_a_bad_link_alerts_once_per_episode_and_again_after_recovery() -> None:
    registry, clocks = _Registry(), _Clocks()
    published: list[dict[str, str]] = []
    rec, live = _recorder(registry, clocks), _live()
    rec.on_clock_alert = published.append

    async def phase(n: int, rtt_s: float) -> None:
        for _ in range(n):
            await _exchange(
                rec, live, clocks, skew_s=0.0, out_s=rtt_s / 2, back_s=rtt_s / 2
            )
            clocks.advance(6.0)

    await phase(10, 1.2)  # bound 600 ms > 250 ms
    await asyncio.sleep(0.1)  # (_drain would end the write chain)
    assert len(registry.alerts) == 1 and len(published) == 1
    await phase(40, 0.004)  # the window now holds only good exchanges
    await asyncio.sleep(0.1)
    assert live.clock.uncertain is False
    assert len(registry.alerts) == 1
    await phase(40, 1.2)
    await phase(1, 1.2)
    await _drain(live)
    # good exchanges are still in the window (min-RTT wins) until they age out
    # of 32 exchanges; after 41 bad ones the second episode must have started
    assert len(registry.alerts) == 2, len(registry.alerts)
    assert len(published) == 2


@pytest.mark.asyncio
async def test_an_old_agent_without_resolution_is_not_reported_tighter_than_its_tick() -> (
    None
):
    registry, clocks = _Registry(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    for _ in range(5):
        await _exchange(
            rec, live, clocks, skew_s=0.0, out_s=0.002, back_s=0.002, resolution_us=0
        )
        clocks.advance(6.0)
    assert live.clock.latest is not None
    assert live.clock.latest.bound_ms >= 8.0  # 16 ms assumed tick / 2


@pytest.mark.asyncio
async def test_a_failing_alert_write_does_not_stop_later_rtt_writes() -> None:
    class Failing(_Registry):
        async def record_clock_alert(self, node_id: str, **kwargs: object) -> int:
            raise RuntimeError("database says no")

    registry, clocks = Failing(), _Clocks()
    rec, live = _recorder(registry, clocks), _live()
    for _ in range(8):
        await _exchange(rec, live, clocks, skew_s=0.0, out_s=0.6, back_s=0.6)
        clocks.advance(6.0)
    await asyncio.sleep(0.1)
    before = len(registry.rtt)
    await _exchange(rec, live, clocks, skew_s=0.0, out_s=0.001, back_s=0.001)
    await _drain(live)
    assert len(registry.rtt) == before + 1


def test_the_pong_proto_field_exists_with_a_default_of_zero() -> None:
    """An old agent's Pong deserialises to clock_resolution_us == 0."""
    old = agent_pb2.Pong(ping_id="x")
    assert agent_pb2.Pong.FromString(old.SerializeToString()).clock_resolution_us == 0
