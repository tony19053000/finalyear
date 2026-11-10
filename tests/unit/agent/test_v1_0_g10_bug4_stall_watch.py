"""V1.0 G10 BUG-4 (D451), watchdog half: a chunk that makes no progress is
reported, not silently renewed for ever.

Lab L4 chunk 68: the worker sat 17 min with no output and ~0 CPU while the
lane kept renewing its lease; the controller only copied it when the queue ran
empty. Now the lane judges every running chunk: **no output and less than
``cpu_share`` CPU-seconds per second (the whole process tree)** for
``max(floor_s, factor x expected)`` - expected = this lane's own recent
per-item run time x the chunk's items - and reports ``stalled_s`` with the
lease in every ``LeaseRenew``. It only reports: a slow-but-alive chunk is never
killed here (the controller decides: an early speculative copy wins or loses
like any other).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import structlog

from meshforge.agent.execution.stall_watch import (
    ChunkProgress,
    PaceHistory,
    StallPolicy,
    tree_cpu_seconds,
    watch_chunk,
)
from meshforge.agent.settings import AgentSettings, SplitSettings

pytestmark = pytest.mark.unit

POLICY = StallPolicy(enabled=True, floor_s=60.0, factor=4.0, cpu_share=0.02, check_s=5)


def test_threshold_is_the_floor_or_n_times_the_expected_time() -> None:
    assert POLICY.threshold_s(None) == 60.0  # no history yet: the floor
    assert POLICY.threshold_s(10.0) == 60.0  # 4 x 10 < floor
    assert POLICY.threshold_s(25.0) == 100.0  # L4: chunks took ~25 s


def test_settings_carry_the_policy_and_bound_it() -> None:
    s = SplitSettings()
    p = StallPolicy.from_settings(s)
    assert p.enabled and p.floor_s >= 60 and p.factor >= 2
    custom = AgentSettings.load(
        agent={
            "split": {
                "stall_floor_s": 30,
                "stall_factor": 3,
                "stall_cpu_share": 0.05,
                "stall_check_s": 2,
                "stall_watch": False,
            }
        }
    ).agent.split
    p = StallPolicy.from_settings(custom)
    assert (p.enabled, p.floor_s, p.factor, p.cpu_share, p.check_s) == (
        False,
        30,
        3,
        0.05,
        2,
    )
    with pytest.raises(ValueError, match="stall_floor_s"):
        SplitSettings(stall_floor_s=1)


def test_an_idle_chunk_is_stalled_after_the_threshold() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    assert p.sample(30.0, cpu_s=1.0) == 0.0  # starting up: CPU used
    assert p.sample(60.0, cpu_s=1.1) == 0.0  # idle 30 s (< 60 s floor)
    assert p.sample(85.0, cpu_s=1.1) == 0.0
    assert p.sample(95.0, cpu_s=1.12) == pytest.approx(65.0)  # idle since 30 s
    assert p.stalled


def test_output_or_cpu_is_progress() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.sample(10.0, cpu_s=0.0)
    p.output(50.0)
    assert p.sample(100.0, cpu_s=0.0) == 0.0  # output at 50 s: idle 50 s only
    # CPU-bound and silent (a long computation): never stalled
    assert p.sample(500.0, cpu_s=400.0) == 0.0
    assert p.sample(900.0, cpu_s=800.0) == 0.0


def test_cpu_below_the_share_is_not_progress() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.sample(0.0, cpu_s=0.0)
    # 0.5 CPU-s in 100 s = 0.5 %: background noise, not work
    assert p.sample(100.0, cpu_s=0.5) == pytest.approx(100.0)


def test_unknown_cpu_is_never_judged_stalled() -> None:
    """No evidence (the process tree could not be read) is not a stall."""
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    assert p.sample(500.0, cpu_s=None) == 0.0
    assert p.sample(900.0, cpu_s=None) == 0.0


def test_a_stall_that_resumes_is_cleared() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.sample(0.0, cpu_s=0.0)
    assert p.sample(100.0, cpu_s=0.0) > 0
    assert p.sample(110.0, cpu_s=5.0) == 0.0
    assert not p.stalled


def test_disabled_policy_never_reports() -> None:
    off = StallPolicy(enabled=False, floor_s=60, factor=4, cpu_share=0.02, check_s=5)
    p = ChunkProgress(off, started=0.0, expected_s=None)
    p.sample(0.0, cpu_s=0.0)
    assert p.sample(10_000.0, cpu_s=0.0) == 0.0


def test_pace_history_is_per_item_and_robust() -> None:
    pace = PaceHistory()
    assert pace.expected_s(4) is None
    for run_s in (24.0, 25.0, 26.0, 900.0):  # one outlier (the hung chunk)
        pace.add(run_s, items=4)
    assert pace.expected_s(4) == pytest.approx(25.5)  # median, not the mean
    assert pace.expected_s(8) == pytest.approx(51.0)
    pace.add(10.0, items=0)  # nothing to learn from an empty chunk
    assert pace.expected_s(4) == pytest.approx(25.5)


def test_pace_history_is_bounded() -> None:
    pace = PaceHistory(keep=5)
    for i in range(100):
        pace.add(float(i), items=1)
    assert pace.expected_s(1) == 97.0  # median of the last 5 (95..99)


def test_tree_cpu_seconds_reads_a_real_process_tree() -> None:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\nt=time.time()\nwhile time.time()-t<0.4: pass",
        ],
    )
    try:
        proc.wait(timeout=30)
    finally:
        pass
    assert tree_cpu_seconds(proc.pid) is None  # gone: no evidence
    assert (tree_cpu_seconds(os.getpid()) or 0.0) > 0.0


@pytest.mark.asyncio
async def test_the_watch_reports_a_real_idle_process_and_logs_once() -> None:
    """A real child that sleeps (no output, no CPU) - what L4's worker did."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    loop = asyncio.get_running_loop()
    policy = StallPolicy(
        enabled=True, floor_s=0.6, factor=4, cpu_share=0.02, check_s=0.1
    )
    progress = ChunkProgress(policy, started=loop.time(), expected_s=None)
    seen: list[float] = []
    try:
        with structlog.testing.capture_logs() as logs:
            task = asyncio.create_task(
                watch_chunk(
                    progress,
                    policy,
                    clock=loop.time,
                    pid=lambda: proc.pid,
                    report=seen.append,
                    log_fields={"chunk": 68},
                )
            )
            await asyncio.sleep(0.3)  # the interpreter starting: CPU
            await asyncio.sleep(1.8)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        proc.kill()
        await proc.wait()
    assert seen and seen[-1] >= 0.6
    stalled = [e for e in logs if e["event"] == "agent.lane.chunk_stalled"]
    assert len(stalled) == 1  # once per stall, not every tick
    assert stalled[0]["chunk"] == 68
    assert stalled[0]["log_level"] == "warning"


@pytest.mark.asyncio
async def test_the_watch_waits_for_the_process_to_exist() -> None:
    """Env phase / not started yet: nothing is judged, nothing reported."""
    loop = asyncio.get_running_loop()
    policy = StallPolicy(
        enabled=True, floor_s=0.1, factor=4, cpu_share=0.02, check_s=0.05
    )
    progress = ChunkProgress(policy, started=loop.time(), expected_s=None)
    seen: list[float] = []
    task = asyncio.create_task(
        watch_chunk(
            progress, policy, clock=loop.time, pid=lambda: None, report=seen.append
        )
    )
    time.sleep(0)  # noqa: ASYNC251
    await asyncio.sleep(0.4)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert seen == [] or set(seen) == {0.0}


# ------------------------------------------------- the lane, end to end (fake host)


@pytest.mark.asyncio
async def test_a_hung_chunk_is_reported_stalled_in_the_lease_renewal(
    tmp_path: Path,
) -> None:
    """A real child that never makes progress (L4's chunk 68), the real lane
    loop: the next LeaseRenew names the chunk's lease with ``stalled_s``; the
    chunk is not killed by the lane; the entry is dropped when it ends."""
    from types import SimpleNamespace
    from typing import Any

    from test_v1_0_g10_lane_env_failure import _attempt, _grant, _Host, _plan

    from meshforge.agent.execution.runtime import RunResult
    from meshforge.common.errors import FailureType
    from meshforge.common.proto.meshforge.v1 import agent_pb2

    host = _Host(tmp_path, [_grant(0)])
    host._settings = SimpleNamespace(  # type: ignore[attr-defined]
        agent=SimpleNamespace(
            split=SimpleNamespace(
                stall_watch=True,
                stall_floor_s=0.5,
                stall_factor=4.0,
                stall_cpu_share=0.02,
                stall_check_s=0.1,
            )
        )
    )
    renews: list[agent_pb2.LeaseRenew] = []

    async def send_now(name: str, payload: Any) -> bool:
        if name == "lease_renew":
            renews.append(payload)
        return True

    host._send_now = send_now
    attempt = _attempt(tmp_path)
    attempt.lane.spec.lease_timeout_s = 3.0  # renew every second
    killed_by_lane: list[bool] = []

    class HungRuntime:
        async def run(self, spec: Any, on_output: Any, *, observer: Any) -> RunResult:
            del spec, on_output
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-c", "import time; time.sleep(60)"
            )
            observer.enter_env()
            observer.enter_run()
            observer.ident = str(child.pid)
            try:
                for _ in range(100):
                    if any(ref.stalled_s > 0 for r in renews for ref in r.leases):
                        break
                    await asyncio.sleep(0.1)
                killed_by_lane.append(child.returncode is not None)
            finally:
                child.kill()
                await child.wait()
            observer.finish()
            return RunResult(exit_code=1, failure_type=FailureType.USER_ERROR)

    await asyncio.wait_for(
        host._run_lane(
            attempt, _plan(attempt.workdir), None, HungRuntime(), None, _noop
        ),  # type: ignore[arg-type]
        30,
    )
    stalled = [ref for r in renews for ref in r.leases if ref.stalled_s > 0]
    assert stalled, [list(r.leases) for r in renews]
    assert stalled[0].item_id == 100 and stalled[0].lease_seq == 1
    assert killed_by_lane == [False]  # reported, never killed by the lane
    assert attempt.lane.stalled == {}  # dropped when the chunk ended
    assert len(host.chunk_results) == 1  # the chunk still reported its end


async def _noop(stream: object, data: bytes) -> None:
    del stream, data
