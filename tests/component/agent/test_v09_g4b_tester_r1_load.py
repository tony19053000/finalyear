"""V0.9 G4 lane B tester round 1: real burner processes under failure injection.

A load that dies while the job runs must be reported unverified (the record
becomes ``condition_failed``), whatever kills it, and the revert must still
leave no process and no marker entry behind.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import psutil
import pytest

from meshforge.agent.conditions.base import ConditionFailedError
from meshforge.agent.conditions.burn import main as burn_main
from meshforge.agent.conditions.runner import ConditionRunner
from meshforge.agent.settings import ConditionSettings
from meshforge.common.bench_conditions import ConditionKind

# ruff: noqa: PERF401, ASYNC109, ASYNC110
pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(os.name != "posix", reason="posix signals"),
]


def _runner(tmp_path: Path) -> ConditionRunner:
    return ConditionRunner(
        ConditionSettings(verify_window_s=1.0),
        data_dir=tmp_path,
        kinds={ConditionKind.CPU_LOAD},
    )


def _burners() -> list[psutil.Process]:
    out = []
    for proc in psutil.process_iter(["cmdline"]):
        if "meshforge.agent.conditions.burn" in " ".join(proc.info["cmdline"] or []):
            out.append(proc)
    return out


async def _gone(timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while _burners():
            await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_a_burner_killed_externally_mid_run_is_unverified_and_cleaned(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    active = runner.prepare("att_1", "cpu_load:2", timeout_s=60)
    await active.apply()
    victims = _burners()
    assert len(victims) == 2
    victims[0].send_signal(signal.SIGKILL)
    await asyncio.sleep(0.5)
    report = await active.finish()
    assert report.verified is False
    assert "ended before the attempt" in report.reason
    assert report.reverted is True
    await _gone()
    assert runner.marker.load() == {}


@pytest.mark.asyncio
async def test_all_burners_dead_before_verification_fails_the_condition(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    active = runner.prepare("att_1", "cpu_load:1", timeout_s=60)

    async def assassin() -> None:
        for _ in range(100):
            for proc in _burners():
                proc.kill()
            await asyncio.sleep(0.02)

    killer = asyncio.ensure_future(assassin())
    with pytest.raises(ConditionFailedError):
        await active.apply()
    killer.cancel()
    report = await active.finish()
    assert report.verified is False and report.reverted is True
    await _gone()


@pytest.mark.asyncio
async def test_a_stopped_burner_cannot_fake_the_load(tmp_path: Path) -> None:
    """SIGSTOP leaves the process alive but idle: the measured CPU time decides."""
    runner = _runner(tmp_path)
    active = runner.prepare("att_1", "cpu_load:1", timeout_s=60)

    async def freezer() -> None:
        for _ in range(100):
            for proc in _burners():
                proc.suspend()
            await asyncio.sleep(0.02)

    task = asyncio.ensure_future(freezer())
    with pytest.raises(ConditionFailedError, match="busy cores"):
        await active.apply()
    task.cancel()
    for proc in _burners():
        proc.resume()
    report = await active.finish()
    assert report.verified is False
    await _gone()


@pytest.mark.asyncio
async def test_close_while_applying_leaves_no_process(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    active = runner.prepare("att_1", "cpu_load:2", timeout_s=60)
    task = asyncio.ensure_future(active.apply())
    await asyncio.sleep(0.4)  # burners are up, verification window not over
    await runner.close()
    with pytest.raises((ConditionFailedError, asyncio.CancelledError)):
        await task
    await _gone()


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT TR1-B3 (minor): ActiveCondition.apply()'s finally re-writes the "
    "marker after a concurrent finish() (stop_all/close) removed it, leaving a stale "
    "entry (for netem the next start would delete an unrelated qdisc on that interface)",
)
@pytest.mark.asyncio
async def test_close_while_applying_leaves_no_marker_entry(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    active = runner.prepare("att_1", "cpu_load:2", timeout_s=60)
    task = asyncio.ensure_future(active.apply())
    await asyncio.sleep(0.4)
    await runner.close()
    await asyncio.gather(task, return_exceptions=True)
    await _gone()
    assert runner.marker.load() == {}


def test_burner_main_refuses_a_missing_parent_by_exiting() -> None:
    """A burner whose parent pid is already gone ends at once (no orphan)."""
    import subprocess
    import sys
    import time

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.agent.conditions.burn",
            "cpu",
            "--deadline-s",
            "60",
            "--parent",
            str(dead.pid),
        ],
        timeout=20,
        check=False,
    )
    assert time.monotonic() - started < 15
    assert proc.returncode in (0, 1, 2)
    assert burn_main is not None
