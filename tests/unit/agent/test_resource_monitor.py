"""V0.3 G4 (roadmap 0.3.5): the resource monitor's reporting policy and its loop.

The sampler is a fake (it returns the samples the test wants) and the session client
is a fake that records what would go on the wire; the *real* client and NVML are
exercised by the component tests. Time is injected everywhere: no test here sleeps
for more than a tick of 10 ms.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest

from meshforge.agent.hardware.disks import DiskFacts
from meshforge.agent.hardware.gpu import GpuUsage
from meshforge.agent.resources.convert import to_resource_update
from meshforge.agent.resources.monitor import (
    ReportPolicy,
    ResourceMonitor,
)
from meshforge.agent.resources.sampler import ResourceSample
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import ResourceSettings

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 9, 21, 12, 0, tzinfo=dt.UTC)


def sample(
    *,
    cpu: float = 10.0,
    ram_used: int = 1_000_000,
    gpu_util: float | None = None,
    free_bytes: int | None = None,
    at: dt.datetime = T0,
) -> ResourceSample:
    return ResourceSample(
        observed_at=at,
        cpu_percent=cpu,
        ram_used_bytes=ram_used,
        ram_total_bytes=8_000_000,
        gpus=(() if gpu_util is None else (GpuUsage(0, gpu_util, 1_000, 2_000),)),
        disks=(
            ()
            if free_bytes is None
            else (
                DiskFacts(mount="/", fstype="", total_bytes=100, free_bytes=free_bytes),
            )
        ),
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


def settings(**overrides: Any) -> ResourceSettings:
    base: dict[str, Any] = {
        "sample_interval_s": 0.01,
        "change_threshold_percent": 5.0,
        "max_report_interval_s": 10.0,
    }
    base.update(overrides)
    return ResourceSettings(**base)


# --------------------------------------------------------------------- policy


def test_the_first_sample_is_always_reported() -> None:
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    assert policy.should_report(sample(), 100.0) is True


def test_a_sample_below_every_trigger_is_suppressed() -> None:
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    first = sample(cpu=10.0)
    policy.record_reported(first, 100.0)
    assert policy.should_report(sample(cpu=12.0), 101.0) is False
    assert policy.last_reported is first


def test_crossing_the_cpu_threshold_reports_at_once() -> None:
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    policy.record_reported(sample(cpu=10.0), 100.0)
    assert policy.should_report(sample(cpu=15.01), 100.1) is True


def test_a_byte_metric_moves_relatively_not_absolutely() -> None:
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    policy.record_reported(sample(ram_used=1_000_000), 100.0)
    assert policy.should_report(sample(ram_used=1_040_000), 100.1) is False
    assert policy.should_report(sample(ram_used=1_060_000), 100.1) is True


def test_the_max_interval_forces_a_report_when_nothing_changed() -> None:
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    policy.record_reported(sample(), 100.0)
    assert policy.should_report(sample(), 109.9) is False
    assert policy.should_report(sample(), 110.0) is True


def test_a_gpu_or_disk_appearing_is_a_change() -> None:
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    policy.record_reported(sample(), 100.0)
    assert policy.should_report(sample(gpu_util=0.0), 100.1) is True
    policy.record_reported(sample(gpu_util=0.0), 100.0)
    assert policy.should_report(sample(gpu_util=50.0), 100.1) is True


def test_a_disk_free_move_is_relative() -> None:
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    policy.record_reported(sample(free_bytes=1_000_000), 100.0)
    assert policy.should_report(sample(free_bytes=990_000), 100.1) is False
    assert policy.should_report(sample(free_bytes=800_000), 100.1) is True


def test_a_metric_moving_away_from_zero_is_a_change() -> None:
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    policy.record_reported(sample(ram_used=0), 100.0)
    assert policy.should_report(sample(ram_used=1), 100.1) is True


def test_mark_not_online_forces_the_next_report() -> None:
    policy = ReportPolicy(threshold_percent=5.0, max_interval_s=10.0)
    policy.record_reported(sample(), 100.0)
    assert policy.should_report(sample(), 100.1) is False
    policy.mark_not_online()
    assert policy.should_report(sample(), 100.2) is True


# -------------------------------------------------------------------- convert


def test_to_resource_update_clamps_and_caps() -> None:
    wild = ResourceSample(
        observed_at=T0,
        cpu_percent=1000.0,
        ram_used_bytes=5,
        ram_total_bytes=9,
        gpus=tuple(GpuUsage(i, -3.0, 1, 2) for i in range(20)),
        disks=tuple(
            DiskFacts(mount="m" * 300, fstype="", total_bytes=1, free_bytes=1)
            for _ in range(20)
        ),
    )
    update = to_resource_update(wild, max_gpus=16, max_disks=16)
    assert update.cpu_percent == 100.0
    assert update.gpus[0].util_percent == 0.0
    assert len(update.gpus) == 16
    assert len(update.disks) == 16
    assert len(update.disks[0].mount) == 128
    assert update.sequence == 0  # the session client stamps it


def test_to_resource_update_carries_the_observation_time() -> None:
    update = to_resource_update(sample())
    assert update.observed_at.ToDatetime(tzinfo=dt.UTC) == T0


# -------------------------------------------------------------------- monitor


async def run_ticks(monitor: ResourceMonitor, count: int) -> asyncio.Task[None]:
    task = asyncio.create_task(monitor.run())
    for _ in range(200):
        if monitor.stats.samples >= count:
            break
        await asyncio.sleep(0.005)
    monitor.request_stop()
    await asyncio.wait_for(task, 2.0)
    return task


@pytest.mark.asyncio
async def test_the_monitor_reports_the_first_sample_and_suppresses_a_quiet_one() -> (
    None
):
    client = FakeClient()
    sampler = FakeSampler([sample(cpu=10.0), sample(cpu=10.5), sample(cpu=10.5)])
    monitor = ResourceMonitor(client, sampler, settings())  # type: ignore[arg-type]
    await run_ticks(monitor, 3)

    assert sampler.primed == 1
    assert sampler.closed == 1
    assert len(client.sent) == 1
    assert monitor.stats.reports == 1
    assert monitor.stats.suppressed >= 1


@pytest.mark.asyncio
async def test_a_change_beyond_the_threshold_is_reported() -> None:
    client = FakeClient()
    sampler = FakeSampler([sample(cpu=10.0), sample(cpu=90.0), sample(cpu=90.0)])
    monitor = ResourceMonitor(client, sampler, settings())  # type: ignore[arg-type]
    await run_ticks(monitor, 3)
    assert len(client.sent) == 2
    assert [round(u.cpu_percent) for u in client.sent] == [10, 90]


@pytest.mark.asyncio
async def test_nothing_is_sent_while_the_session_is_not_online() -> None:
    client = FakeClient(state=AgentState.BACKOFF)
    sampler = FakeSampler([sample(cpu=10.0)])
    monitor = ResourceMonitor(client, sampler, settings())  # type: ignore[arg-type]
    task = asyncio.create_task(monitor.run())
    for _ in range(200):
        if monitor.stats.offline_samples >= 2:
            break
        await asyncio.sleep(0.005)
    assert client.sent == []

    # D9: the first sample after the session is ONLINE again is forced through,
    # even though it is identical to the ones that were suppressed.
    client.state = AgentState.ONLINE
    for _ in range(200):
        if client.sent:
            break
        await asyncio.sleep(0.005)
    monitor.request_stop()
    await asyncio.wait_for(task, 2.0)
    assert len(client.sent) >= 1
    assert monitor.stats.offline_samples >= 2


@pytest.mark.asyncio
async def test_a_sampler_exception_is_counted_and_the_loop_survives() -> None:
    client = FakeClient()
    sampler = FakeSampler([OSError("/proc went away"), sample(cpu=10.0)])
    monitor = ResourceMonitor(client, sampler, settings())  # type: ignore[arg-type]
    task = asyncio.create_task(monitor.run())
    for _ in range(200):
        if client.sent:
            break
        await asyncio.sleep(0.005)
    monitor.request_stop()
    await asyncio.wait_for(task, 2.0)
    assert monitor.stats.sampler_errors == 1
    assert len(client.sent) >= 1


@pytest.mark.asyncio
async def test_a_failed_send_is_counted_and_not_recorded_as_reported() -> None:
    client = FakeClient()
    client.fail_next = True
    sampler = FakeSampler([sample(cpu=10.0), sample(cpu=10.0)])
    monitor = ResourceMonitor(client, sampler, settings())  # type: ignore[arg-type]
    await run_ticks(monitor, 2)
    assert monitor.stats.send_errors == 1
    # The failed sample was never recorded, so the next one is still forced.
    assert len(client.sent) >= 1


@pytest.mark.asyncio
async def test_run_refuses_to_start_twice() -> None:
    client = FakeClient()
    monitor = ResourceMonitor(client, FakeSampler([sample()]), settings())  # type: ignore[arg-type]
    task = asyncio.create_task(monitor.run())
    await asyncio.sleep(0.02)
    with pytest.raises(RuntimeError, match="already running"):
        await monitor.run()
    monitor.request_stop()
    await asyncio.wait_for(task, 2.0)


def test_settings_reject_a_max_interval_below_the_sample_interval() -> None:
    with pytest.raises(ValueError, match="max_report_interval_s"):
        ResourceSettings(sample_interval_s=10.0, max_report_interval_s=1.0)
