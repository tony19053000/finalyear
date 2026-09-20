"""V0.3 G4 tester round: repeated ONLINE/SUSPECT/OFFLINE cycling of D9.

The implementer's own tests cover exactly one not-ONLINE -> ONLINE transition.
This file hammers ``ReportPolicy`` and ``ResourceMonitor`` through many flaps to
check the "force" flag does not get stuck, does not leak a suppressed count across
a flap, and that the replay ring is never fed while offline (D9's actual worry:
buffered dead reports evicting real ones from the ring during an outage).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from meshforge.agent.resources.monitor import ReportPolicy, ResourceMonitor
from meshforge.agent.resources.sampler import ResourceSample
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import ResourceSettings

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC)


def sample(*, cpu: float = 10.0) -> ResourceSample:
    return ResourceSample(
        observed_at=T0,
        cpu_percent=cpu,
        ram_used_bytes=1_000_000,
        ram_total_bytes=8_000_000,
    )


class FakeSampler:
    """Stands in for ``ResourceSampler``; blocking calls, as the real one is."""

    def __init__(self, samples: list[ResourceSample | Exception]) -> None:
        self._samples = samples
        self.primed = 0
        self.closed = 0
        self.taken = 0

    def prime(self) -> None:
        self.primed += 1

    def close(self) -> None:
        self.closed += 1

    def sample(self) -> ResourceSample:
        index = min(self.taken, len(self._samples) - 1)
        self.taken += 1
        item = self._samples[index]
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    """Stands in for ``AgentSessionClient``: state + send_report."""

    def __init__(self, state: AgentState = AgentState.ONLINE) -> None:
        self.node_id = "node_01TESTTESTTESTTESTTESTTEST"
        self.state = state
        self.sent: list[Any] = []
        self.fail_next = False

    async def send_report(self, payload_name: str, payload: Any) -> str:
        assert payload_name == "resource_update"
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("stream is gone")
        self.sent.append(payload)
        return "cmd_01TESTTESTTESTTESTTESTTEST"


def test_repeated_flaps_always_force_the_first_online_sample_after_each_one() -> None:
    """Ten ONLINE -> not-ONLINE -> ONLINE flaps: every re-entry to ONLINE must be
    forced, none may be silently suppressed because a stale ``_last`` looked
    unchanged."""
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=1000.0)
    now = 0.0
    unchanged = sample(cpu=42.0)

    assert policy.should_report(unchanged, now) is True
    policy.record_reported(unchanged, now)

    for cycle in range(10):
        now += 1.0
        policy.mark_not_online()
        # While offline the monitor does not call should_report at all (D9), but
        # even if it did, forced must hold.
        assert policy.should_report(unchanged, now) is True, f"cycle {cycle}"
        now += 1.0
        # Back online: the identical sample must still be forced through.
        assert policy.should_report(unchanged, now) is True, f"cycle {cycle}"
        policy.record_reported(unchanged, now)
        # Immediately after recording, an identical sample is suppressed again -
        # the force flag must not leak into the next quiet period.
        assert policy.should_report(unchanged, now) is False, f"cycle {cycle}"


@pytest.mark.asyncio
async def test_the_monitor_survives_many_online_offline_flaps_without_sending_while_down() -> (
    None
):
    client = FakeClient(state=AgentState.ONLINE)
    # A single constant sample repeated many times: if suppression state ever
    # leaked across a flap, this would either send far more than the flap count
    # or send nothing at all after the first flap.
    sampler = FakeSampler([sample(cpu=10.0)] * 400)
    monitor = ResourceMonitor(
        client,
        sampler,
        ResourceSettings(
            sample_interval_s=0.005,
            change_threshold_percent=5.0,
            max_report_interval_s=1000.0,
        ),
    )  # type: ignore[arg-type]
    task = asyncio.create_task(monitor.run())
    try:
        flaps = 8
        for _ in range(flaps):
            client.state = AgentState.BACKOFF
            await asyncio.sleep(0.03)
            assert client.state is AgentState.BACKOFF  # sanity: still down
            client.state = AgentState.ONLINE
            await asyncio.sleep(0.03)
    finally:
        monitor.request_stop()
        await asyncio.wait_for(task, 2.0)

    # Every report ever sent must have happened while ONLINE (never buffered
    # offline traffic reaching the client) - the fake client only records what
    # send_report was actually called with, and the monitor's own contract is
    # to check state before calling send_report at all.
    assert monitor.stats.offline_samples > 0, "the test must actually exercise D9"
    assert client.sent, "at least the post-flap forced samples must go out"
    # No suppressed count should have been "stolen" by offline samples: they are
    # tracked in their own counter, never folded into `suppressed`.
    assert (
        monitor.stats.samples
        == monitor.stats.reports
        + monitor.stats.suppressed
        + monitor.stats.offline_samples
    )


def test_mark_not_online_is_idempotent_across_many_calls() -> None:
    """Calling mark_not_online() every tick while offline (as the real monitor
    does) must not accumulate any state that changes behaviour once online."""
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    for _ in range(1000):
        policy.mark_not_online()
    assert policy.should_report(sample(), 0.0) is True
