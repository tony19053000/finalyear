"""V1.0 G10 BUG-4 (D451) tester pass, agent half: written from the spec
(docs/briefs/progress/V1.0-G10-lab-fixes.md, BUG-4 a-c), not from the code.

Stall thresholds (floor vs factor x expected), output / CPU resets, boundary
values, the watch loop with an injected clock / CPU reader, ``direct_python``
fallbacks and ``quiet_error_mode`` off Windows.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from meshforge.agent.execution.stall_watch import (
    ChunkProgress,
    PaceHistory,
    StallPolicy,
    watch_chunk,
)
from meshforge.agent.execution.venv_runtime import PYVENV_LAUNCHER_VAR, direct_python
from meshforge.agent.win_error_mode import QUIET_FLAGS, quiet_error_mode

pytestmark = pytest.mark.unit

POLICY = StallPolicy(
    enabled=True, floor_s=300.0, factor=4.0, cpu_share=0.02, check_s=10
)


# ------------------------------------------------------------- thresholds


def test_threshold_boundary_floor_equals_factor_times_expected() -> None:
    assert POLICY.threshold_s(75.0) == 300.0  # 4 x 75 == floor
    assert POLICY.threshold_s(75.1) == pytest.approx(300.4)
    assert POLICY.threshold_s(0.0) == 300.0  # zero expected: floor


def test_stall_starts_exactly_at_the_threshold_not_before() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.sample(0.0, 0.0)
    assert p.sample(299.9, 0.0) == 0.0
    assert p.sample(300.0, 0.0) == pytest.approx(300.0)


def test_slow_lane_threshold_scales_with_expected_not_floor() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=200.0)  # 800 s threshold
    p.sample(0.0, 0.0)
    assert p.sample(799.0, 0.0) == 0.0
    assert p.sample(800.0, 0.0) == pytest.approx(800.0)


def test_output_resets_the_idle_clock_for_the_whole_threshold() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.sample(0.0, 0.0)
    p.output(250.0)
    assert p.sample(549.0, 0.0) == 0.0  # 299 s since the byte
    assert p.sample(550.0, 0.0) == pytest.approx(300.0)
    p.output(560.0)  # one more byte clears the stall
    assert p.sample(570.0, 0.0) == 0.0 and not p.stalled


def test_cpu_share_boundary_is_strictly_greater() -> None:
    """Exactly cpu_share CPU-s per wall-s is NOT progress; just above is."""
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.sample(0.0, 0.0)
    assert p.sample(300.0, 6.0) == pytest.approx(300.0)  # 0.02 exactly
    q = ChunkProgress(POLICY, started=0.0, expected_s=None)
    q.sample(0.0, 0.0)
    assert q.sample(300.0, 6.1) == 0.0


def test_cpu_counter_going_backwards_is_not_progress_and_not_a_crash() -> None:
    """A process tree that lost a child can report less CPU than before."""
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.sample(1.0, 50.0)
    assert p.sample(400.0, 10.0) == pytest.approx(399.0)  # idle since t=1


def test_same_timestamp_twice_does_not_divide_by_zero() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.sample(10.0, 1.0)
    p.sample(10.0, 1.0)


def test_unreadable_cpu_restarts_the_idle_clock_and_rebaselines() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.sample(0.0, 0.0)
    assert p.sample(290.0, None) == 0.0
    # idle clock restarted at 290: 299 s later still fine, 300 s later stalled
    assert p.sample(589.0, 0.0) == 0.0
    assert p.sample(590.0, 0.0) == pytest.approx(300.0)


def test_begin_restarts_the_idle_clock_for_a_late_process() -> None:
    p = ChunkProgress(POLICY, started=0.0, expected_s=None)
    p.begin(1000.0)  # env build took 1000 s: not the chunk's idle time
    assert p.sample(1010.0, 0.0) == 0.0


def test_a_hung_chunk_after_fast_chunks_is_stalled_long_before_17_minutes() -> None:
    """L4: chunks took ~25 s; the hung one must be flagged within minutes."""
    pace = PaceHistory()
    for _ in range(5):
        pace.add(25.0, items=1)
    exp = pace.expected_s(1)
    assert exp == 25.0
    p = ChunkProgress(POLICY, started=0.0, expected_s=exp)
    p.sample(0.0, 0.0)
    assert p.sample(300.0, 0.4) > 0  # 1.6 s CPU (L4) is not progress
    assert 300.0 < 17 * 60


@pytest.mark.parametrize(("run_s", "items"), [(5.0, 0), (5.0, -2), (-1.0, 3)])
def test_pace_ignores_nonsense_samples(run_s: float, items: int) -> None:
    pace = PaceHistory()
    pace.add(run_s, items=items)
    assert pace.expected_s(1) is None
    assert pace.expected_s(0) is None


def test_pace_scales_with_the_chunks_item_count() -> None:
    pace = PaceHistory()
    pace.add(100.0, items=10)
    assert pace.expected_s(40) == pytest.approx(400.0)


# ------------------------------------------------------------- watch loop


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _drive(
    cpu_values: list[float | None],
    *,
    policy: StallPolicy,
    pid_after: int = 0,
) -> tuple[list[float], list[dict[str, object]]]:
    """Run watch_chunk with a fake sleep (advances the fake clock by check_s)."""
    clock = _Clock()
    reports: list[float] = []
    ticks = 0
    real_sleep = asyncio.sleep

    async def fake_sleep(s: float) -> None:
        nonlocal ticks
        clock.now += s
        ticks += 1
        await real_sleep(0)

    readings = iter(cpu_values)
    extra = [0.0]
    done = asyncio.Event()

    def cpu(_pid: int) -> float | None:
        try:
            return next(readings)
        except StopIteration:
            done.set()
            extra[0] += 100.0  # keeps progressing: no second stall
            return 50.0 + extra[0]

    def pid() -> int | None:
        return None if ticks <= pid_after else 4242

    progress = ChunkProgress(policy, started=0.0, expected_s=None)
    import meshforge.agent.execution.stall_watch as sw

    orig = sw.asyncio.sleep
    sw.asyncio.sleep = fake_sleep  # type: ignore[assignment]
    try:
        with structlog.testing.capture_logs() as logs:
            task = asyncio.create_task(
                watch_chunk(
                    progress,
                    policy,
                    clock=clock,
                    pid=pid,
                    report=reports.append,
                    cpu=cpu,
                )
            )
            await asyncio.wait_for(done.wait(), 10)
            await real_sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        sw.asyncio.sleep = orig  # type: ignore[assignment]
    return reports, logs


@pytest.mark.asyncio
async def test_watch_reports_every_tick_and_logs_stall_once_then_resume_once() -> None:
    pol = StallPolicy(enabled=True, floor_s=30.0, factor=4, cpu_share=0.02, check_s=10)
    # idle for 5 ticks (stalled from tick 3), then CPU jumps (resumes)
    cpu = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 50.0, 50.0]
    reports, logs = await _drive(cpu, policy=pol)
    assert any(r > 0 for r in reports) and reports[0] == 0.0
    events = [e["event"] for e in logs]
    assert events.count("agent.lane.chunk_stalled") == 1
    assert events.count("agent.lane.chunk_resumed") == 1
    assert events.index("agent.lane.chunk_stalled") < events.index(
        "agent.lane.chunk_resumed"
    )


@pytest.mark.asyncio
async def test_watch_judges_nothing_before_the_process_exists() -> None:
    pol = StallPolicy(enabled=True, floor_s=30.0, factor=4, cpu_share=0.02, check_s=10)
    reports, _ = await _drive([0.0] * 4, policy=pol, pid_after=50)
    # pid() is None for 50 ticks: cpu() is only consumed afterwards, and the idle
    # clock starts at the first tick that has a pid, not at 'started'
    assert reports and reports[0] == 0.0


@pytest.mark.asyncio
async def test_disabled_watch_returns_at_once() -> None:
    off = StallPolicy(enabled=False, floor_s=30.0, factor=4, cpu_share=0.02, check_s=10)
    p = ChunkProgress(off, started=0.0, expected_s=None)
    await asyncio.wait_for(
        watch_chunk(p, off, clock=lambda: 0.0, pid=lambda: 1, report=lambda _s: None),
        2,
    )


# ------------------------------------------------------- direct_python


def _venv(tmp_path: Path, cfg: bytes | None, *, base: bool = True) -> Path:
    venv = tmp_path / "v e nv-é"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"l")
    if cfg is not None:
        (venv / "pyvenv.cfg").write_bytes(cfg)
    if base:
        (tmp_path / "bäse dir").mkdir(exist_ok=True)
        (tmp_path / "bäse dir" / "python.exe").write_bytes(b"i")
    return venv


def test_unicode_and_spaces_in_paths(tmp_path: Path) -> None:
    venv = _venv(tmp_path, f"home = {tmp_path / 'bäse dir'}\n".encode())
    got = direct_python(venv, platform="win32")
    assert got is not None
    assert got[0] == tmp_path / "bäse dir" / "python.exe"
    assert got[1][PYVENV_LAUNCHER_VAR] == str(venv / "Scripts" / "python.exe")


@pytest.mark.parametrize(
    "cfg",
    [
        b"",
        b"home\n",
        b"home =\n",
        b"home =    \n",
        b"\xff\xfe\x00garbage\x80",
        b"# home = x\n",
    ],
    ids=["empty", "no_equals", "empty_value", "blank_value", "binary", "commented"],
)
def test_corrupt_or_empty_pyvenv_cfg_keeps_the_launcher(
    tmp_path: Path, cfg: bytes
) -> None:
    venv = _venv(tmp_path, cfg)
    assert direct_python(venv, platform="win32") is None


def test_home_pointing_at_a_directory_named_python_exe_keeps_the_launcher(
    tmp_path: Path,
) -> None:
    venv = _venv(tmp_path, f"home = {tmp_path / 'x'}\n".encode(), base=False)
    (tmp_path / "x" / "python.exe").mkdir(parents=True)  # a dir, not a file
    assert direct_python(venv, platform="win32") is None


def test_nonexistent_venv_dir_is_not_an_error(tmp_path: Path) -> None:
    assert direct_python(tmp_path / "nope", platform="win32") is None


@pytest.mark.parametrize("platform", ["linux", "darwin", "cygwin"])
def test_never_direct_off_windows(tmp_path: Path, platform: str) -> None:
    venv = _venv(tmp_path, f"home = {tmp_path / 'bäse dir'}\n".encode())
    assert direct_python(venv, platform=platform) is None


def test_first_home_key_wins_and_other_keys_are_ignored(tmp_path: Path) -> None:
    venv = _venv(
        tmp_path,
        f"executable = C:\\nope\\python.exe\nhome = {tmp_path / 'bäse dir'}\n".encode(),
    )
    got = direct_python(venv, platform="win32")
    assert got is not None and got[0].parent.name == "bäse dir"


# ------------------------------------------------------- quiet_error_mode


def test_error_mode_default_platform_is_this_host_and_a_no_op_on_posix() -> None:
    import sys

    if sys.platform == "win32":
        pytest.skip("Windows: covered by the component test")
    assert quiet_error_mode() is None


def test_error_mode_is_idempotent() -> None:
    class K:
        mode = 0

        def GetErrorMode(self) -> int:  # noqa: N802
            return self.mode

        def SetErrorMode(self, m: int) -> int:  # noqa: N802
            self.mode = m
            return 0

    k = K()
    a = quiet_error_mode(platform="win32", kernel32=k)
    b = quiet_error_mode(platform="win32", kernel32=k)
    assert a == b == QUIET_FLAGS


def test_error_mode_setter_failure_and_missing_api_are_never_fatal() -> None:
    class SetFails:
        def GetErrorMode(self) -> int:  # noqa: N802
            return 0

        def SetErrorMode(self, _m: int) -> int:  # noqa: N802
            raise OSError("denied")

    assert quiet_error_mode(platform="win32", kernel32=SetFails()) is None
    assert quiet_error_mode(platform="win32", kernel32=object()) is None
