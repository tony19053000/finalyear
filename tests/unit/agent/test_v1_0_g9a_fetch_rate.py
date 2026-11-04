"""V1.0 G9-A item 4 (D431): the process-wide fetch rate cap, bucket math.

Fake clock + fake sleep (no real waits): refill, burst cap, debt shared by
concurrent takers on one loop (the aggregate rate is the cap), one sleep per
charged piece, and the ``[agent.fetch] max_rate_mbit`` setting bounds. The
real-socket slowdown is ``tests/component/agent/test_v1_0_g9a_fetch_rate_loopback.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from meshforge.agent.cas.rate import MIN_BURST_BYTES, FetchRateLimiter
from meshforge.agent.settings import FetchSettings

pytestmark = pytest.mark.unit


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)


def _bucket(
    t: FakeTime, rate: float = 1000.0, burst: float = 100.0
) -> FetchRateLimiter:
    return FetchRateLimiter(rate, burst_bytes=burst, clock=t.clock, sleep=t.sleep)


def test_within_the_burst_nothing_waits_then_debt_is_paid_at_the_rate() -> None:
    t = FakeTime()
    bucket = _bucket(t)
    assert bucket.reserve(60) == 0.0
    assert bucket.reserve(40) == 0.0  # exactly the burst
    assert bucket.reserve(500) == pytest.approx(0.5)  # 500 B at 1000 B/s
    t.now = 0.5  # paid back to zero
    assert bucket.reserve(100) == pytest.approx(0.1)
    assert bucket.charged_bytes == 700
    assert bucket.waited_s == pytest.approx(0.6)


def test_an_idle_bucket_never_holds_more_than_its_burst() -> None:
    t = FakeTime()
    bucket = _bucket(t)
    t.now = 3600.0  # an hour idle
    assert bucket.reserve(100) == 0.0
    assert bucket.reserve(1000) == pytest.approx(1.0)  # no hour of credit
    assert bucket.reserve(0) == 0.0 and bucket.reserve(-5) == 0.0


def test_concurrent_takers_queue_behind_each_others_debt() -> None:
    """Four workers each charge 1000 B at the same instant: the n-th waits
    for all the bytes before it - together they get exactly the rate."""
    t = FakeTime()
    bucket = _bucket(t, rate=1000.0, burst=100.0)
    waits = [bucket.reserve(1000) for _ in range(4)]
    assert waits == pytest.approx([0.9, 1.9, 2.9, 3.9])
    # 4000 B in 3.9 s + the 100 B burst = 1000 B/s exactly
    assert (4000 - 100) / waits[-1] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_take_sleeps_once_per_charged_piece_and_only_when_in_debt() -> None:
    t = FakeTime()
    bucket = _bucket(t)
    await bucket.take(50)
    assert t.sleeps == []
    await bucket.take(1050)
    assert t.sleeps == [pytest.approx(1.0)]
    # many concurrent takers on one loop: each sleeps once, waits grow
    t.sleeps.clear()
    t.now = 10.0
    await asyncio.gather(*(bucket.take(1000) for _ in range(3)))
    assert t.sleeps == pytest.approx([0.9, 1.9, 2.9])


def test_the_default_burst_and_the_mbit_unit() -> None:
    t = FakeTime()
    bucket = FetchRateLimiter.from_mbit(8.0, clock=t.clock, sleep=t.sleep)
    assert bucket.rate == 1_000_000.0  # 8 Mbit/s = 1 MB/s
    assert bucket.burst == 250_000.0  # 0.25 s of the rate
    slow = FetchRateLimiter(1000.0, clock=t.clock, sleep=t.sleep)
    assert slow.burst == MIN_BURST_BYTES
    with pytest.raises(ValueError, match="> 0"):
        FetchRateLimiter(0)


def test_the_setting_is_off_or_a_sane_cap() -> None:
    assert FetchSettings().max_rate_mbit == 0
    assert FetchSettings(max_rate_mbit=1).max_rate_mbit == 1
    assert FetchSettings(max_rate_mbit=40.5).max_rate_mbit == 40.5
    for bad in (0.5, -1, 100_001):
        with pytest.raises(ValidationError):
            FetchSettings(max_rate_mbit=bad)


def test_the_executor_has_one_cap_for_every_fetch(tmp_path: Path) -> None:
    from meshforge.agent.execution.executor import Executor
    from meshforge.agent.execution.offer import ExecutionSetup
    from meshforge.agent.execution.runtime import RuntimeAvailability
    from meshforge.agent.execution.selection import execution_offer
    from meshforge.agent.settings import AgentSettings

    def build(rate: float) -> Executor:
        settings = AgentSettings.load(
            data_dir=tmp_path / f"data-{rate}",
            agent={
                "controller_address": "127.0.0.1:1",
                "fetch": {"max_rate_mbit": rate},
            },
        )
        offer = execution_offer(
            enabled=False,
            allow_unsandboxed=False,
            docker=None,
            subprocess=RuntimeAvailability("subprocess", False),
        )

        async def report(_name: str, _payload: object) -> str:
            return "m"

        return Executor(
            settings,
            ExecutionSetup(offer=offer, runtimes={}),
            node_id="node_x",
            report=report,
            controller_host=lambda: "127.0.0.1",
        )

    capped = build(10.0)
    assert capped._fetch_rate is not None
    assert capped._fetch_rate.rate == 1_250_000.0
    assert capped._fetch_kw()["rate"] is capped._fetch_rate  # bundles too
    off = build(0.0)
    assert off._fetch_rate is None and "rate" not in off._fetch_kw()
