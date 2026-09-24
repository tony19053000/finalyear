"""``lab/agent_rss.py``: finding the agent process on Linux and Windows command
lines, growth maths, restart detection, checkpoint resume - and one real child
process found, sampled and killed through psutil (skipped without the agent extra)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import agent_rss as cli
from _fabric import checkpoint
from _fabric.rss import (
    KIND,
    MIB,
    ProcInfo,
    PsutilSource,
    RssError,
    is_agent_command,
    pick_agent,
    summarize,
)
from _fabric.rss_run import RssConfig, RssSampler, new_state
from _fabric.testing import FakeClock, ScriptedOperator

WIN_VENV = r"C:\Users\x\meshforge\.venv\Scripts"


@pytest.mark.parametrize(
    ("name", "cmdline", "expected"),
    [
        # Linux, `uv run meshforge-agent run`: the python child is the agent ...
        (
            "meshforge-agent",
            ["/r/.venv/bin/python3", "/r/.venv/bin/meshforge-agent", "run"],
            True,
        ),
        # ... and the uv wrapper above it is not
        ("uv", ["uv", "run", "meshforge-agent", "run"], False),
        (
            "uv",
            ["/usr/bin/uv", "run", "meshforge-agent", "run", "--controller", "h:1"],
            False,
        ),
        # Windows: launcher exe and the python.exe it starts are both candidates
        ("meshforge-agent.exe", [rf"{WIN_VENV}\meshforge-agent.exe", "run"], True),
        (
            "python.exe",
            [rf"{WIN_VENV}\python.exe", rf"{WIN_VENV}\meshforge-agent.exe", "run"],
            True,
        ),
        (
            "uv.exe",
            [r"C:\Users\x\.local\bin\uv.exe", "run", "meshforge-agent", "run"],
            False,
        ),
        ("python", ["python", "-m", "meshforge.agent", "run"], True),
        ("MESHFORGE-AGENT.EXE", [r"X:\MESHFORGE-AGENT.EXE", "RUN"], True),
        # other agent subcommands and other programs are not the running agent
        ("meshforge-agent", ["/r/bin/meshforge-agent", "identity", "show"], False),
        ("meshforge-agent", ["/r/bin/meshforge-agent", "hardware", "show"], False),
        ("python", ["python", "lab/agent_rss.py", "run"], False),
        ("meshforge-controller", ["/r/bin/meshforge-controller"], False),
        ("bash", [], False),
    ],
)
def test_agent_command_lines(name: str, cmdline: list[str], expected: bool) -> None:
    assert is_agent_command(name, cmdline) is expected


def _p(pid: int, ppid: int, name: str, *cmd: str) -> ProcInfo:
    return ProcInfo(pid, ppid, name, tuple(cmd), float(pid))


def test_the_leaf_of_the_uv_tree_is_the_agent_on_windows() -> None:
    procs = [
        _p(10, 1, "uv.exe", "uv.exe", "run", "meshforge-agent", "run"),
        _p(11, 10, "meshforge-agent.exe", r"C:\v\meshforge-agent.exe", "run"),
        _p(
            12, 11, "python.exe", r"C:\v\python.exe", r"C:\v\meshforge-agent.exe", "run"
        ),
        _p(13, 1, "explorer.exe", "explorer.exe"),
    ]
    assert pick_agent(procs).pid == 12


def test_the_leaf_of_the_uv_tree_is_the_agent_on_linux() -> None:
    procs = [
        _p(20, 1, "uv", "uv", "run", "meshforge-agent", "run"),
        _p(21, 20, "meshforge-agent", "/v/bin/python", "/v/bin/meshforge-agent", "run"),
    ]
    assert pick_agent(procs).pid == 21


def test_no_agent_and_two_agents_are_typed_errors() -> None:
    with pytest.raises(RssError) as none:
        pick_agent([_p(1, 0, "init", "init")])
    assert none.value.failure_type == "agent_not_found"
    two = [
        _p(30, 1, "python", "python", "-m", "meshforge.agent", "run"),
        _p(31, 1, "python", "python", "-m", "meshforge.agent", "run"),
    ]
    with pytest.raises(RssError) as many:
        pick_agent(two)
    assert many.value.failure_type == "agent_ambiguous"
    assert "30" in str(many.value) and "31" in str(many.value)


def test_a_parent_cycle_in_a_hostile_process_table_terminates() -> None:
    procs = [
        _p(40, 41, "python", "python", "-m", "meshforge.agent", "run"),
        _p(41, 40, "python", "python", "-m", "meshforge.agent", "run"),
    ]
    with pytest.raises(RssError):
        pick_agent(procs)  # both are ancestors of each other: no leaf, but no hang


def _state(
    samples: list[tuple[float, int, int]], warmup_min: float = 10.0
) -> dict[str, Any]:
    state = new_state(
        RssConfig(warmup_min=warmup_min), datetime(2026, 9, 24, tzinfo=UTC), "L2"
    )
    state["samples"] = [
        {
            "at": f"t{e}",
            "elapsed_s": e,
            "pid": pid,
            "create_time": float(pid),
            "rss_bytes": rss,
        }
        for e, pid, rss in samples
    ]
    state["elapsed_s"] = samples[-1][0] if samples else 0.0
    return state


def test_growth_is_measured_from_the_post_warmup_baseline() -> None:
    samples = [(0.0, 7, 50 * MIB), (300.0, 7, 90 * MIB), (600.0, 7, 100 * MIB)]
    samples += [(600.0 + 60 * i, 7, (100 + i) * MIB) for i in range(1, 6)]
    s = summarize(_state(samples))
    assert s["first_mib"] == 50.0
    assert s["baseline_mib"] == 100.0  # the first sample >= 10 min in
    assert s["end_mib"] == 105.0 and s["max_mib"] == 105.0
    assert s["growth_pct"] == 5.0
    assert s["restarts"] == 0


def test_a_restart_splits_the_series_and_is_counted() -> None:
    samples = [(60.0 * i, 7, 100 * MIB) for i in range(20)]
    samples += [(1200.0 + 60 * i, 8, 60 * MIB) for i in range(3)]
    s = summarize(_state(samples, warmup_min=0))
    assert s["restarts"] == 1
    assert s["pids"] == [7, 8]
    assert s["main_pid"] == 7  # growth is judged on the longest-lived process
    assert s["growth_pct"] == 0.0


def test_an_empty_run_summarises_to_unknowns() -> None:
    s = summarize(_state([]))
    assert s["growth_pct"] is None and s["samples"] == 0


@dataclass
class FakeSource:
    procs: list[ProcInfo] = field(default_factory=list)
    rss_by_pid: dict[int, int] = field(default_factory=dict)

    def processes(self) -> list[ProcInfo]:
        return list(self.procs)

    def rss(self, ident: tuple[int, float]) -> int | None:
        if not any(p.ident == ident for p in self.procs):
            return None
        return self.rss_by_pid.get(ident[0])


def _agent(pid: int) -> ProcInfo:
    return _p(pid, 1, "python", "python", "-m", "meshforge.agent", "run")


def test_sampler_follows_an_agent_that_dies_and_comes_back_as_a_new_process() -> None:
    clock = FakeClock()
    source = FakeSource([_agent(100)], {100: 80 * MIB})
    cfg = RssConfig(hours=10 / 60, interval_s=60.0, warmup_min=0)
    state = new_state(cfg, clock.wall(), "L3")
    saves: list[int] = []
    op = ScriptedOperator(clock, [])

    def die() -> None:
        source.procs = []

    def reborn() -> None:
        source.procs = [_agent(200)]
        source.rss_by_pid[200] = 70 * MIB

    clock.after(150.0, die)
    clock.after(330.0, reborn)
    RssSampler(
        source, clock, op, cfg, state, lambda s: saves.append(len(s["samples"]))
    ).run()
    kinds = [e["kind"] for e in state["events"]]
    assert kinds == ["agent_found", "agent_not_found", "agent_restarted"]
    assert {s["pid"] for s in state["samples"]} == {100, 200}
    assert summarize(state)["restarts"] == 1
    assert state["done"] is True
    assert len(saves) == 11 + 1  # one save per sample tick, plus the final one


def test_cli_resumes_from_its_checkpoint_after_an_interrupt(tmp_path: Path) -> None:
    source = FakeSource([_agent(100)], {100: 80 * MIB})
    ckpt = tmp_path / "rss.json"
    argv = ["--reports-dir", str(tmp_path), "run", "--hours", str(10 / 60), "--name", "L2",
            "--warmup-min", "0", "--checkpoint", str(ckpt)]  # fmt: skip
    clock = FakeClock()

    def ctrl_c() -> None:
        raise KeyboardInterrupt

    clock.after(250.0, ctrl_c)
    op = ScriptedOperator(clock, [])
    assert (
        cli.main(argv, clock=clock, operator=op, source=source) == cli.EXIT_INTERRUPTED
    )
    saved = checkpoint.load(ckpt, KIND)
    assert saved is not None and len(saved["samples"]) == 5 and not saved["done"]
    source.rss_by_pid[100] = 84 * MIB  # +5 %
    later = FakeClock(start_wall=datetime(2026, 9, 24, 12, 0, tzinfo=UTC))
    op2 = ScriptedOperator(later, [])
    assert cli.main(argv, clock=later, operator=op2, source=source) == cli.EXIT_OK
    state = checkpoint.load(ckpt, KIND)
    assert state is not None and state["done"] and len(state["gaps"]) == 1
    assert summarize(state)["growth_pct"] == 5.0
    assert summarize(state)["restarts"] == 0  # same (pid, create_time) after the resume
    report = (tmp_path / "2026-09-24-V0.3-agent-rss.md").read_text(encoding="utf-8")
    assert "| L2 | 100 |" in report


def test_cli_exit_codes_for_growth_and_for_no_agent(tmp_path: Path) -> None:
    growing = FakeSource([_agent(100)], {100: 80 * MIB})
    clock = FakeClock()
    clock.after(200.0, lambda: growing.rss_by_pid.__setitem__(100, 100 * MIB))
    argv = ["--reports-dir", str(tmp_path), "run", "--hours", str(5 / 60), "--warmup-min", "0",
            "--checkpoint", str(tmp_path / "a.json"), "--name", "L4"]  # fmt: skip
    op = ScriptedOperator(clock, [])
    assert (
        cli.main(argv, clock=clock, operator=op, source=growing) == cli.EXIT_INCOMPLETE
    )
    empty = FakeSource()
    argv = [
        "--reports-dir",
        str(tmp_path),
        "run",
        "--checkpoint",
        str(tmp_path / "b.json"),
    ]
    assert cli.main(argv, clock=clock, operator=op, source=empty) == cli.EXIT_NO_AGENT
    assert not (tmp_path / "b.json").exists()


def test_missing_psutil_is_a_clear_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(
        sys.modules, "psutil", None
    )  # makes `import psutil` raise ImportError
    assert cli.main(["--reports-dir", str(tmp_path), "run"]) == cli.EXIT_NO_PSUTIL
    assert "uv sync --extra agent" in capsys.readouterr().err


def test_a_real_agent_like_process_is_found_sampled_and_lost_when_killed() -> None:
    pytest.importorskip("psutil", reason="agent_rss needs the agent extra")
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "meshforge-agent", "run"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        source = PsutilSource(
            within=os.getpid()
        )  # ignore any real agent on this machine
        deadline = time.monotonic() + 10.0
        found = None
        while found is None and time.monotonic() < deadline:
            try:
                found = pick_agent(source.processes())
            except RssError:
                time.sleep(0.02)
        assert found is not None and found.pid == child.pid
        rss = source.rss(found.ident)
        assert rss is not None and rss > 0
        assert (
            source.rss((found.pid, found.create_time + 5.0)) is None
        )  # reused-pid guard
        child.kill()
        child.wait(timeout=10)
        assert source.rss(found.ident) is None
        with pytest.raises(RssError):
            pick_agent(source.processes())
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
