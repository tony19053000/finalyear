"""V1.0 G9-C (D435, lab bug #8): CPU % from our own ``cpu_times()`` deltas.

``cpu_times`` is a fake named tuple sequence and the clock is injected; nothing
here reads this machine's CPU except the Windows-only real-burn test at the end
(skipped elsewhere; it is the only test that waits on real time).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import threading
from collections import namedtuple
from collections.abc import Iterator
from typing import Any

import pytest

from meshforge.agent.hardware.gpu import NvmlSession
from meshforge.agent.resources.convert import to_resource_update
from meshforge.agent.resources.cpu import (
    CpuMeter,
    CpuUnknown,
    busy_percent,
    core_percents,
)
from meshforge.agent.resources.monitor import ResourceMonitor
from meshforge.agent.resources.sampler import ResourceSample, ResourceSampler
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import ResourceSettings

pytestmark = pytest.mark.unit

Linux = namedtuple(
    "Linux",
    "user nice system idle iowait irq softirq steal guest guest_nice",
)
Win = namedtuple("Win", "user system idle interrupt dpc")


def lin(user: float, idle: float, *, iowait: float = 0.0, guest: float = 0.0) -> Any:
    return Linux(user, 0.0, 0.0, idle, iowait, 0.0, 0.0, 0.0, guest, 0.0)


def win(user: float, idle: float) -> Any:
    return Win(user, 0.0, idle, 0.0, 0.0)


class Script:
    """A ``cpu_times`` stand-in returning (or raising) items in order."""

    def __init__(self, items: list[Any]) -> None:
        self._items: Iterator[Any] = iter(items)

    def __call__(self) -> Any:
        item = next(self._items)
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class NoGpu:
    """Stands in for ``NvmlSession`` on a machine without NVIDIA GPUs."""

    reason = ""

    def usage(self) -> list[Any]:
        return []

    def close(self) -> None:
        return None


def no_gpu() -> NvmlSession:
    return NoGpu()  # type: ignore[return-value]


def sampler(
    items: list[Any],
    clock: Clock,
    *,
    max_stale_s: float = 30.0,
    min_window_s: float = 0.0,
) -> Any:
    return ResourceSampler(
        mounts=[],
        nvml=no_gpu(),
        cpu_times=Script(items),
        clock=clock,
        max_stale_s=max_stale_s,
        min_cpu_window_s=min_window_s,
    )


# ------------------------------------------------------------------ pure math


def test_busy_share_from_deltas_linux_and_windows() -> None:
    assert busy_percent(lin(10, 90), lin(85, 115)).percent == pytest.approx(75.0)
    assert busy_percent(win(10, 90), win(40, 160)).percent == pytest.approx(30.0)


def test_iowait_is_idle_and_guest_is_not_counted_twice() -> None:
    reading = busy_percent(
        lin(0, 0, iowait=0, guest=0), lin(50, 25, iowait=25, guest=50)
    )
    assert reading.percent == pytest.approx(50.0)


def test_a_small_backwards_field_is_clamped_not_negative() -> None:
    # Linux iowait can go down a little: the window is still valid.
    reading = busy_percent(lin(0, 50, iowait=10), lin(100, 150, iowait=9))
    assert reading.percent == pytest.approx(50.0)


def test_a_reset_or_wrapped_counter_is_unknown_never_zero() -> None:
    reading = busy_percent(lin(1e9, 1e9), lin(5, 5))
    assert reading.percent is None
    assert reading.reason is CpuUnknown.BACKWARDS


def test_no_elapsed_time_is_unknown_never_zero() -> None:
    reading = busy_percent(win(10, 90), win(10, 90))
    assert reading.percent is None
    assert reading.reason is CpuUnknown.NO_TIME


def test_full_burn_is_100_and_idle_is_0() -> None:
    assert busy_percent(win(0, 0), win(100, 0)).percent == 100.0
    assert busy_percent(win(0, 0), win(0, 100)).percent == 0.0


def test_core_percents_per_core() -> None:
    before = [win(0, 0), win(0, 0), win(5, 5)]
    after = [win(100, 0), win(25, 75), win(5, 5)]
    assert core_percents(before, after) == [100.0, 25.0, None]


# ---------------------------------------------------------------------- meter


def test_meter_first_read_is_the_baseline_only() -> None:
    meter = CpuMeter(Script([win(0, 0), win(50, 50)]))
    first = meter.read()
    assert first.percent is None
    assert first.reason is CpuUnknown.NO_BASELINE
    assert meter.read().percent == pytest.approx(50.0)


def test_meter_failed_read_is_typed_and_keeps_the_baseline() -> None:
    meter = CpuMeter(Script([win(0, 0), OSError("busy"), win(80, 20)]))
    meter.read()
    failed = meter.read()
    assert failed.percent is None
    assert failed.reason is CpuUnknown.READ_FAILED
    assert failed.detail == "OSError"
    assert meter.read().percent == pytest.approx(80.0)


def test_meter_no_time_keeps_the_longer_window() -> None:
    meter = CpuMeter(Script([win(0, 0), win(0, 0), win(30, 70)]))
    meter.read()
    assert meter.read().reason is CpuUnknown.NO_TIME
    assert meter.read().percent == pytest.approx(30.0)


def test_meter_short_window_keeps_the_baseline_and_does_not_read() -> None:
    clock = Clock()
    meter = CpuMeter(Script([win(0, 0), win(70, 30)]), clock=clock, min_window_s=0.25)
    meter.read()
    clock.now = 0.1
    short = meter.read()  # the script has one item left: it was not consumed
    assert short.percent is None
    assert short.reason is CpuUnknown.SHORT_WINDOW
    clock.now = 0.25
    assert meter.read().percent == pytest.approx(70.0)


def test_meter_rebases_after_backwards() -> None:
    meter = CpuMeter(Script([win(100, 100), win(1, 1), win(41, 61)]))
    meter.read()
    assert meter.read().reason is CpuUnknown.BACKWARDS
    assert meter.read().percent == pytest.approx(40.0)


def test_meter_not_a_named_tuple_is_a_failed_read() -> None:
    meter = CpuMeter(Script([(1.0, 2.0)]))
    reading = meter.read()
    assert reading.reason is CpuUnknown.READ_FAILED
    assert reading.detail == "TypeError"


def test_meter_is_per_meter_not_per_thread() -> None:
    """The lab-#8 root cause: every sample runs on a new thread. psutil's own
    ``cpu_percent`` keys its baseline by thread id; the meter must not."""
    meter = CpuMeter(Script([win(0, 0), win(60, 40), win(160, 40)]))
    results: list[float | None] = []

    def one() -> None:
        results.append(meter.read().percent)

    for _ in range(3):
        thread = threading.Thread(target=one)
        thread.start()
        thread.join()
    assert results == [None, pytest.approx(60.0), pytest.approx(100.0)]


# -------------------------------------------------------------------- sampler


def test_sampler_reports_the_delta_since_its_previous_sample() -> None:
    clock = Clock()
    s = sampler([win(0, 0), win(25, 75), win(125, 75)], clock)
    s.prime()
    assert s.sample().cpu_percent == pytest.approx(25.0)
    assert s.sample().cpu_percent == pytest.approx(100.0)


def test_sampler_failed_read_reuses_last_good_flagged_never_zero() -> None:
    clock = Clock()
    s = sampler([win(0, 0), win(90, 10), OSError("x"), win(90, 10)], clock)
    s.prime()
    assert s.sample().cpu_percent == pytest.approx(90.0)
    clock.now = 2.0
    failed = s.sample()
    assert failed.cpu_percent == pytest.approx(90.0)
    assert "cpu: stale (read_failed OSError)" in failed.degraded
    clock.now = 4.0
    same_tick = s.sample()  # no time since the baseline: stale, not 0
    assert same_tick.cpu_percent == pytest.approx(90.0)
    assert "cpu: stale (no_time)" in same_tick.degraded


def test_sampler_stale_value_expires_to_unknown() -> None:
    clock = Clock()
    s = sampler(
        [win(0, 0), win(50, 50), OSError("x"), OSError("x")], clock, max_stale_s=5.0
    )
    s.prime()
    assert s.sample().cpu_percent == pytest.approx(50.0)
    clock.now = 5.0
    assert s.sample().cpu_percent == pytest.approx(50.0)
    clock.now = 5.5
    expired = s.sample()
    assert expired.cpu_percent is None
    assert "cpu: unknown (read_failed OSError)" in expired.degraded


def test_sampler_without_any_good_read_is_unknown_not_zero() -> None:
    s = sampler([OSError("x"), RuntimeError("y")], Clock())
    s.prime()
    first = s.sample()
    assert first.cpu_percent is None
    assert "cpu: unknown (read_failed RuntimeError)" in first.degraded


def test_sampler_first_sample_right_after_prime_is_unknown_not_noise() -> None:
    clock = Clock()
    s = sampler([win(0, 0), win(50, 50)], clock, min_window_s=0.25)
    s.prime()
    first = s.sample()
    assert first.cpu_percent is None
    assert "cpu: unknown (short_window)" in first.degraded
    clock.now = 2.0
    assert s.sample().cpu_percent == pytest.approx(50.0)


def test_sampler_samples_without_prime_take_a_baseline_first() -> None:
    s = sampler([win(0, 0), win(20, 80)], Clock())
    assert s.sample().cpu_percent == pytest.approx(20.0)


# --------------------------------------------------------- convert + monitor


def test_convert_refuses_unknown_cpu() -> None:
    import datetime as dt

    sample = ResourceSample(observed_at=dt.datetime.now(dt.UTC), cpu_percent=None)
    with pytest.raises(ValueError, match="unknown"):
        to_resource_update(sample)


class _Client:
    node_id = "node_01TESTTESTTESTTESTTESTTEST"
    state = AgentState.ONLINE

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_report(self, payload_name: str, payload: Any) -> str:
        self.sent.append(payload)
        return "cmd_01TESTTESTTESTTESTTESTTEST"


class _Samples:
    def __init__(self, cpus: list[float | None]) -> None:
        import datetime as dt

        self._items = iter(
            ResourceSample(observed_at=dt.datetime.now(dt.UTC), cpu_percent=c)
            for c in cpus
        )
        self.done = asyncio.Event()

    def prime(self) -> None:
        return None

    def close(self) -> None:
        return None

    def sample(self) -> ResourceSample:
        try:
            return next(self._items)
        except StopIteration:
            self.done.set()
            raise RuntimeError("no more samples") from None


@pytest.mark.asyncio
async def test_monitor_never_sends_unknown_cpu_and_forces_the_next_known() -> None:
    client = _Client()
    fake = _Samples([None, None, 40.0])
    monitor = ResourceMonitor(
        client,
        fake,  # type: ignore[arg-type]
        ResourceSettings(sample_interval_s=0.01, max_report_interval_s=10.0),
    )
    task = asyncio.create_task(monitor.run())
    try:
        await asyncio.wait_for(fake.done.wait(), 5.0)
    finally:
        monitor.request_stop()
        await asyncio.wait_for(task, 5.0)
    assert monitor.stats.cpu_unknown == 2
    assert [round(u.cpu_percent) for u in client.sent] == [40]


# ------------------------------------------------- real burn (Windows CI only)


_BURN = (
    "import time\n"
    "end = time.process_time() + 1.5\n"
    "while time.process_time() < end:\n"
    "    pass\n"
)


@pytest.mark.skipif(sys.platform != "win32", reason="lab bug #8 is Windows-only")
def test_windows_real_burn_raises_system_cpu_above_idle() -> None:
    """Every read on a fresh thread, as the agent's DeadlineThread does: the
    machine-wide CPU during an all-cores burn is above idle (and high)."""
    meter_sampler = ResourceSampler(mounts=[], nvml=no_gpu())

    def on_new_thread() -> float | None:
        box: list[float | None] = []
        thread = threading.Thread(
            target=lambda: box.append(meter_sampler.sample().cpu_percent)
        )
        thread.start()
        thread.join()
        return box[0]

    on_new_thread()  # baseline
    threading.Event().wait(0.5)  # idle window (real time: this test only)
    idle = on_new_thread()
    burners = [
        subprocess.Popen([sys.executable, "-c", _BURN])
        for _ in range(os.cpu_count() or 1)
    ]
    try:
        for proc in burners:
            proc.wait(timeout=60)
    finally:
        for proc in burners:
            if proc.poll() is None:
                proc.kill()
    busy = on_new_thread()
    assert idle is not None
    assert busy is not None
    assert busy > idle
    assert busy > 50.0
