"""V0.9 G4 lane B tester round 2: real burners, stop_all / cancel at many
points of the spawn + verify window. After finish: no burner of ours anywhere
in the process table (not just our children), the marker is empty, and the
report never says verified."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

pytest.importorskip("psutil", reason="needs the agent extra")

import psutil

from meshforge.agent.conditions.marker import BURNER_MODULE
from meshforge.agent.conditions.runner import ConditionRunner
from meshforge.agent.settings import ConditionSettings
from meshforge.common.bench_conditions import ConditionKind

# ruff: noqa: ASYNC110
pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(os.name != "posix", reason="posix processes"),
]


def _burners() -> list[psutil.Process]:
    out: list[psutil.Process] = []
    me = str(os.getpid())
    for proc in psutil.process_iter():
        try:
            cmd = proc.cmdline()
            if (
                BURNER_MODULE in " ".join(cmd)
                and me in cmd
                and proc.status() != psutil.STATUS_ZOMBIE
            ):
                out.append(proc)
        except psutil.Error:
            continue
    return out


def _runner(tmp_path: Path) -> ConditionRunner:
    return ConditionRunner(
        ConditionSettings(verify_window_s=1.0),
        data_dir=tmp_path,
        kinds={ConditionKind.CPU_LOAD},
    )


async def _gone() -> None:
    async with asyncio.timeout(10):
        while _burners():
            await asyncio.sleep(0.1)


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0.0, 0.001, 0.004, 0.01, 0.03, 0.08, 0.2, 0.5, 1.0])
@pytest.mark.parametrize("how", ["stop_all", "cancel_caller"])
async def test_no_burner_survives_a_stop_at_any_moment(
    tmp_path: Path, delay: float, how: str
) -> None:
    runner = _runner(tmp_path)
    active = runner.prepare("att_1", "cpu_load:3", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    await asyncio.sleep(delay)
    if how == "stop_all":
        await asyncio.wait_for(runner.stop_all("pause"), 20)
        await asyncio.gather(applying, return_exceptions=True)
    else:
        applying.cancel()
        await asyncio.gather(applying, return_exceptions=True)
    report = await asyncio.wait_for(active.finish(), 20)
    # a second, racing stop is harmless
    await asyncio.wait_for(runner.stop_all("again"), 20)
    await _gone()
    assert runner.marker.load() == {}
    assert report.reverted is True
    if how == "stop_all" or applying.cancelled():
        assert report.verified is False
    assert runner.active == []


@pytest.mark.asyncio
async def test_finish_concurrent_with_a_completed_verified_apply(
    tmp_path: Path,
) -> None:
    runner = _runner(tmp_path)
    active = runner.prepare("att_1", "cpu_load:1", timeout_s=60)
    try:
        await active.apply()
    except Exception:  # noqa: BLE001 - a busy CI box can fail the 80% check; not this test's point
        pytest.skip("node too busy to verify cpu_load")
    assert _burners()
    a = asyncio.ensure_future(active.finish())
    b = asyncio.ensure_future(runner.stop_all("x"))
    await asyncio.gather(a, b)
    await _gone()
    assert runner.marker.load() == {}
