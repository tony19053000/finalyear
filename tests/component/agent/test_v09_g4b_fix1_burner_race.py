"""V0.9 G4 lane B fix round 1: ``stop_all`` racing a burner start (real processes).

``finish()`` must cancel and **wait for** a running ``apply()`` before it
reverts, and ``apply()`` must refuse once finishing began - otherwise a burner
started after the revert keeps the laptop busy while the report says
``reverted = true``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="needs the agent extra")

import psutil

from meshforge.agent.conditions.base import ConditionFailedError
from meshforge.agent.conditions.marker import BURNER_MODULE
from meshforge.agent.conditions.runner import ConditionRunner
from meshforge.agent.settings import ConditionSettings
from meshforge.common.bench_conditions import ConditionKind

# ruff: noqa: ASYNC109, ASYNC110
pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(os.name != "posix", reason="posix processes"),
]


def _runner(tmp_path: Path) -> ConditionRunner:
    return ConditionRunner(
        ConditionSettings(verify_window_s=1.0),
        data_dir=tmp_path,
        kinds={ConditionKind.CPU_LOAD},
    )


def _my_burners() -> list[psutil.Process]:
    out: list[psutil.Process] = []
    for child in psutil.Process().children(recursive=True):
        try:
            if BURNER_MODULE in " ".join(child.cmdline()):
                out.append(child)
        except psutil.Error:
            continue
    return out


async def _no_burners(timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while _my_burners():
            await asyncio.sleep(0.1)


@pytest.mark.security
@pytest.mark.asyncio
async def test_stop_all_while_a_burner_is_starting_leaves_none_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real: Callable[..., Any] = asyncio.create_subprocess_exec
    calls = 0
    in_spawn = asyncio.Event()
    gate = asyncio.Event()

    async def gated(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:  # the second burner: held until released
            in_spawn.set()
            await gate.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated)
    runner = _runner(tmp_path)
    active = runner.prepare("att_1", "cpu_load:2", timeout_s=60)
    applying = asyncio.ensure_future(active.apply())
    await asyncio.wait_for(in_spawn.wait(), 10)
    assert len(_my_burners()) == 1
    entry = runner.marker.load()["att_1"]
    assert len(entry["pids"]) == 1  # the first burner was recorded at once

    assert await asyncio.wait_for(runner.stop_all("owner_paused"), 15) == 1
    gate.set()  # too late: the apply was cancelled and awaited
    with pytest.raises(ConditionFailedError, match="stopped while applying"):
        await applying
    await asyncio.sleep(0.5)
    await _no_burners()
    assert calls == 2  # the second burner never started
    assert runner.marker.load() == {}
    report = await active.finish()
    assert report.reverted is True
    assert report.verified is False
    assert "owner_paused" in report.reason


@pytest.mark.asyncio
async def test_a_burner_is_never_started_after_stop_all(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    active = runner.prepare("att_1", "cpu_load:1", timeout_s=60)
    await runner.stop_all("owner_paused")
    with pytest.raises(ConditionFailedError, match="stopped before it was applied"):
        await active.apply()
    await asyncio.sleep(0.3)
    assert _my_burners() == []
    assert runner.marker.load() == {}
