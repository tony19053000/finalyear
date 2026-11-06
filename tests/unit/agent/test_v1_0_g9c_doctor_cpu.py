"""V1.0 G9-C: ``meshforge-agent doctor cpu`` (lab re-run of V0.8 E2, bug #8).

The CPU cap uses the same rule the agent plans a run with; the sampling loop is
driven by fake counters and a fake clock (no real waits; ``wait`` advances the
clock). The CLI test replaces the sampler's sources, never reads this machine.
"""

from __future__ import annotations

import json
import os
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from meshforge.agent import doctor_cpu
from meshforge.agent.doctor_cpu import (
    CpuSample,
    CpuSources,
    cpu_cap,
    render_cap,
    render_sample,
    run_samples,
)
from meshforge.agent.execution.plan import job_cpus
from meshforge.agent.main import app
from meshforge.common.owner_policy import OwnerPolicy

pytestmark = pytest.mark.unit

Win = namedtuple("Win", "user system idle interrupt dpc")


# ------------------------------------------------------------- the CPU cap


def test_no_owner_limit_and_no_request_is_no_cap_on_windows() -> None:
    """Hypothesis A for bug #8: with no max_cpu_percent, no default_cpus and no
    resources.cpus a Windows job gets no CPU-rate cap at all."""
    cap = cpu_cap(OwnerPolicy(), threads=12, default_cpus=None, platform="win32")
    assert cap.cpus is None
    assert cap.machine_percent is None
    assert cap.enforcement.startswith("none")


def test_owner_max_cpu_percent_caps_the_whole_job_on_windows() -> None:
    cap = cpu_cap(
        OwnerPolicy(max_cpu_percent=50), threads=12, default_cpus=None, platform="win32"
    )
    assert cap.cpus == pytest.approx(6.0)
    assert cap.machine_percent == pytest.approx(50.0)
    assert "Job Object" in cap.enforcement


def test_a_request_below_the_owner_cap_wins() -> None:
    cap = cpu_cap(
        OwnerPolicy(max_cpu_percent=50),
        threads=12,
        default_cpus=None,
        asked_cpus=2.0,
        platform="win32",
    )
    assert cap.cpus == pytest.approx(2.0)
    assert cap.machine_percent == pytest.approx(16.67)  # CpuRate 1667 / 100


def test_default_cpus_applies_without_a_request() -> None:
    cap = cpu_cap(OwnerPolicy(), threads=8, default_cpus=4.0, platform="linux")
    assert cap.cpus == pytest.approx(4.0)
    assert cap.machine_percent == pytest.approx(50.0)
    assert "cgroup" in cap.enforcement


def test_job_cpus_is_the_planner_rule() -> None:
    policy = OwnerPolicy(max_cpu_percent=25)
    assert job_cpus(None, None, None, 12) is None
    assert job_cpus(None, None, policy, 12) == pytest.approx(3.0)
    assert job_cpus(8.0, None, policy, 12) == pytest.approx(3.0)
    assert job_cpus(1.0, 4.0, policy, 12) == pytest.approx(1.0)
    assert job_cpus(None, 2.0, OwnerPolicy(), None) == pytest.approx(2.0)


def test_render_cap_names_the_source_and_the_share() -> None:
    cap = cpu_cap(
        OwnerPolicy(max_cpu_percent=50), threads=12, default_cpus=None, platform="win32"
    )
    text = "\n".join(render_cap(cap, policy_source="live"))
    assert "owner max_cpu_percent: 50% of 12 threads  (live)" in text
    assert "6.00 cores = 50.0% of this machine" in text


# ------------------------------------------------------------ the sampling


class FakeMachine:
    """Counters that advance with a fake clock: ``busy`` share on every core."""

    def __init__(self, busy: float, cores: int = 4) -> None:
        self.now = 0.0
        self.busy = busy
        self.cores = cores
        self.fail_at: set[int] = set()
        self.reads = 0
        self.waited: list[float] = []

    def clock(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.waited.append(seconds)
        self.now += seconds

    def _core(self) -> Any:
        return Win(self.now * self.busy, 0.0, self.now * (1 - self.busy), 0.0, 0.0)

    def times(self) -> Any:
        self.reads += 1
        if self.reads in self.fail_at:
            raise OSError("counters unavailable")
        core = self._core()
        return Win(*(v * self.cores for v in core))

    def per_core(self) -> list[Any]:
        return [self._core() for _ in range(self.cores)]

    def sources(self, old: float | None = 3.0) -> CpuSources:
        return CpuSources(
            cpu_times=self.times,
            per_core_times=self.per_core,
            old_percent=lambda: old,
            clock=self.clock,
            wait=self.wait,
        )


def test_one_line_per_second_with_the_machine_share() -> None:
    machine = FakeMachine(busy=0.9)
    out: list[CpuSample] = []
    unknown = run_samples(3, out.append, sources=machine.sources())
    assert unknown == 0
    assert [s.cpu_percent for s in out] == [90.0, 90.0, 90.0]
    assert [s.core_max for s in out] == [90.0, 90.0, 90.0]
    assert [s.old_percent for s in out] == [3.0, 3.0, 3.0]
    assert [round(s.t_s) for s in out] == [1, 2, 3]
    assert machine.waited == [1.0, 1.0, 1.0]


def test_a_failed_read_is_shown_unknown_never_zero() -> None:
    machine = FakeMachine(busy=0.5)
    machine.fail_at = {3}  # baseline = 1, first sample = 2, second sample = 3
    out: list[CpuSample] = []
    unknown = run_samples(3, out.append, sources=machine.sources())
    assert unknown == 1
    assert out[1].cpu_percent is None
    assert out[1].note == "cpu unknown: read_failed OSError"
    assert "cpu=    ?%" in render_sample(out[1])
    assert out[2].cpu_percent == pytest.approx(50.0)


def test_failing_old_method_and_per_core_do_not_stop_sampling() -> None:
    machine = FakeMachine(busy=0.25)

    def boom() -> Any:
        raise RuntimeError("no per-core")

    def old_boom() -> float | None:
        raise RuntimeError("psutil gone")

    sources = machine.sources()
    sources.per_core_times = boom
    sources.old_percent = old_boom
    out: list[CpuSample] = []
    assert run_samples(2, out.append, sources=sources) == 0
    assert [s.cpu_percent for s in out] == [25.0, 25.0]
    assert out[0].core_min is None
    assert out[0].old_percent is None


# ------------------------------------------------------------------- CLI


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("MESHFORGE_INSTALL_MODE", "user")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def _fake_run(machine: FakeMachine, monkeypatch: pytest.MonkeyPatch) -> None:
    real = doctor_cpu.run_samples

    def fake(seconds: int, emit: Any, **_: Any) -> int:
        return real(seconds, emit, sources=machine.sources())

    monkeypatch.setattr(doctor_cpu, "run_samples", fake)
    monkeypatch.setattr(doctor_cpu, "default_threads", lambda: 12)


def test_cli_json_shows_the_live_owner_cap_and_samples(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    done = runner.invoke(app, ["policy", "set", "max_cpu_percent=50"])
    assert done.exit_code == 0, done.output
    _fake_run(FakeMachine(busy=0.75), monkeypatch)
    done = runner.invoke(
        app, ["doctor", "cpu", "--seconds", "2", "--job-cpus", "3", "--json"]
    )
    assert done.exit_code == 0, done.output
    lines = [json.loads(line) for line in done.stdout.splitlines()]
    head, samples = lines[0], lines[1:]
    assert head["policy_source"] == "live"
    assert [c["cpus"] for c in head["caps"]] == [6.0, 3.0]
    assert [s["cpu_percent"] for s in samples] == [75.0, 75.0]


def test_cli_text_and_exit_1_when_nothing_could_be_read(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = FakeMachine(busy=0.5)
    machine.fail_at = set(range(1, 100))
    _fake_run(machine, monkeypatch)
    done = CliRunner().invoke(app, ["doctor", "cpu", "--seconds", "2"])
    assert done.exit_code == 1, done.output
    assert "a job with no resources.cpus gets: no CPU cap" in done.stdout
    assert done.stdout.count("cpu unknown") == 2


def test_plain_doctor_still_runs_without_a_subcommand(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meshforge.agent import doctor as doctor_module

    real = doctor_module.gather
    monkeypatch.setattr(
        doctor_module, "gather", lambda s: real(s, root=None, with_checks=False)
    )
    done = CliRunner().invoke(app, ["doctor", "--json"])
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["ok"] is True


# --------------------------------------------- a base install (no psutil)


def test_old_method_without_psutil_is_none_and_nothing_escapes_the_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed read inside the fresh thread is None, never an unhandled thread
    exception (pytest turns those into failures; the CLI printed a traceback)."""
    monkeypatch.setitem(__import__("sys").modules, "psutil", None)
    assert doctor_cpu._old_method() is None


def test_real_sampling_without_psutil_is_a_setup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "psutil", None)
    emitted: list[CpuSample] = []
    with pytest.raises(doctor_cpu.SamplingUnavailableError):
        run_samples(1, emitted.append)
    assert emitted == []


def test_cli_without_psutil_prints_the_cap_then_names_the_extra(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "psutil", None)
    done = CliRunner().invoke(app, ["doctor", "cpu", "--seconds", "1", "--json"])
    assert done.exit_code == 1, done.output
    lines = done.stdout.splitlines()
    assert len(lines) == 1 and "caps" in json.loads(lines[0])
    assert "needs the agent extra" in done.stderr
    assert "psutil" in done.stderr
