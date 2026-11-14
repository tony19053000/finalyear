"""V1.0 G13 (D461, ADR-0025): every Windows job process runs with HighQoS.

A child of the session-0 agent service was treated as background work (E-cores,
no clock boost; 3x slower on L4). ``spawn()`` sets HighQoS on the suspended child
before resuming it; failure is logged, never fatal. The Windows-only tests query
the real ``GetProcessInformation`` of a spawned child and of its own child.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
import structlog

from meshforge.agent import process
from meshforge.agent.process import power_throttling, spawn

pytestmark = pytest.mark.unit

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows QoS API")


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)


# ----------------------------------------------------------- mocked, any OS


def test_success_logs_one_applied_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "high_qos", lambda pid: 0)
    with structlog.testing.capture_logs() as logs:
        process._apply_high_qos(42, ["C:/x/python.exe", "a.py"])
    qos = [e for e in logs if e["event"] == "agent.process.qos"]
    assert qos == [
        {
            "event": "agent.process.qos",
            "pid": 42,
            "exe": "python.exe",
            "applied": True,
            "log_level": "info",
        }
    ]


def test_a_win32_error_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "high_qos", lambda pid: 5)  # ACCESS_DENIED
    with structlog.testing.capture_logs() as logs:
        process._apply_high_qos(42, ["python"])
    (line,) = [e for e in logs if e["event"] == "agent.process.qos"]
    assert line["applied"] is False and line["error"] == 5
    assert line["log_level"] == "warning"


def test_a_broken_api_never_fails_the_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(pid: int) -> int:
        raise AttributeError("SetProcessInformation")  # pre-Windows 8

    monkeypatch.setattr(process, "high_qos", boom)
    with structlog.testing.capture_logs() as logs:
        process._apply_high_qos(42, ["python"])
    (line,) = [e for e in logs if e["event"] == "agent.process.qos"]
    assert line["applied"] is False and line["error"] == "AttributeError"


def test_not_applicable_logs_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "high_qos", lambda pid: None)
    with structlog.testing.capture_logs() as logs:
        process._apply_high_qos(42, ["python"])
    assert not [e for e in logs if e["event"] == "agent.process.qos"]


@pytest.mark.asyncio
async def test_qos_is_set_before_the_child_is_resumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    real_resume = process._resume

    def qos(pid: int) -> int:
        order.append("qos")
        return 0

    def resume(pid: int) -> bool:
        order.append("resume")
        return real_resume(pid)

    monkeypatch.setattr(process, "high_qos", qos)
    monkeypatch.setattr(process, "_resume", resume)
    handle = await spawn(
        [sys.executable, "-c", "pass"], env=_env(), die_with_parent=False
    )
    await handle.wait(30)
    await handle.reap()
    assert order == ["qos", "resume"]


@pytest.mark.asyncio
async def test_a_failed_qos_still_runs_the_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "high_qos", lambda pid: 87)
    handle = await spawn(
        [sys.executable, "-c", "print('ran')"], env=_env(), die_with_parent=False
    )
    assert await handle.wait(30) == 0
    await handle.reap()


def test_power_throttling_off_windows_is_none() -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX stub")
    assert power_throttling(1) is None
    assert process.high_qos(1) is None and process.high_qos_self() is None


# ------------------------------------------------------ real, Windows only

_GRANDCHILD = (
    "import json, subprocess, sys, time\n"
    "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
    "print(json.dumps({'grandchild': g.pid}), flush=True)\n"
    "time.sleep(30)\n"
)


@windows_only
@pytest.mark.asyncio
async def test_a_spawned_child_and_its_own_child_report_high_qos() -> None:
    handle = await spawn([sys.executable, "-c", _GRANDCHILD], env=_env())
    try:
        assert handle.proc.stdout is not None
        line = await asyncio.wait_for(handle.proc.stdout.readline(), 30)
        grandchild = int(json.loads(line)["grandchild"])
        # ControlMask = EXECUTION_SPEED (0x1), StateMask = 0: never throttled
        assert power_throttling(handle.pid) == (1, 0)
        assert power_throttling(grandchild) == (1, 0), "child processes inherit it"
    finally:
        handle.kill_now()
        await handle.reap()


@windows_only
def test_high_qos_self_sets_the_agent_process() -> None:
    import os

    assert process.high_qos_self() == 0
    state: Any = power_throttling(os.getpid())
    assert state == (1, 0)


# ------------------------------------------- review additions (tester pass)


@pytest.mark.asyncio
async def test_qos_targets_the_spawned_pid_for_every_spawn_flavour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lower_priority (env builds, benchmarks) and plain spawns both get it."""
    seen: list[int] = []
    monkeypatch.setattr(process, "high_qos", lambda pid: seen.append(pid) or 0)
    pids = []
    for lower in (False, True):
        handle = await spawn(
            [sys.executable, "-c", "pass"],
            env=_env(),
            die_with_parent=False,
            lower_priority=lower,
        )
        pids.append(handle.pid)
        await handle.wait(30)
        await handle.reap()
    assert seen == pids


def test_an_oserror_from_the_api_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(pid: int) -> int:
        raise OSError("invalid handle")

    monkeypatch.setattr(process, "high_qos", boom)
    with structlog.testing.capture_logs() as logs:
        process._apply_high_qos(7, ["python"])
    (line,) = [e for e in logs if e["event"] == "agent.process.qos"]
    assert line["applied"] is False and line["error"] == "OSError"


@windows_only
def test_power_throttling_struct_is_three_ulongs() -> None:
    import ctypes

    assert ctypes.sizeof(process._PowerThrottling) == 12  # type: ignore[attr-defined]


@windows_only
def test_high_qos_on_a_dead_pid_reports_an_error_not_an_exception() -> None:
    error = process.high_qos(0x7FFFFFF0)
    assert isinstance(error, int) and error != 0
    assert power_throttling(0x7FFFFFF0) is None


@windows_only
@pytest.mark.asyncio
async def test_lower_priority_child_with_limits_still_has_high_qos() -> None:
    handle = await spawn(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=_env(),
        lower_priority=True,
        limits=process.ProcessLimits(cpus=1.0),
    )
    try:
        assert power_throttling(handle.pid) == (1, 0)
    finally:
        handle.kill_now()
        await handle.reap()
