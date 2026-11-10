"""Independent G9-C (D435) acceptance/edge tests written from the requirement."""

from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from meshforge.agent.main import app
from meshforge.agent.resources.cpu import CpuMeter, CpuUnknown, busy_percent
from meshforge.agent.resources.monitor import MonitorStats
from meshforge.agent.resources.sampler import ResourceSampler

pytestmark = pytest.mark.unit

Win = namedtuple("Win", "user system idle interrupt dpc")


def _spin(seconds: float) -> None:
    end = time.process_time() + seconds
    while time.process_time() < end:
        pass


def _fresh_thread_read(meter: CpuMeter) -> Any:
    box: list[Any] = []
    t = threading.Thread(target=lambda: box.append(meter.read()))
    t.start()
    t.join()
    return box[0]


def test_fresh_thread_samples_see_a_real_burn_not_zero() -> None:
    """Thread-independent baseline: every read on a NEW thread, real psutil."""
    psutil = pytest.importorskip("psutil")
    meter = CpuMeter(psutil.cpu_times)
    _fresh_thread_read(meter)  # baseline
    n = os.cpu_count() or 1
    procs = [multiprocessing.Process(target=_spin, args=(1.5,)) for _ in range(n)]
    for p in procs:
        p.start()
    try:
        time.sleep(0.4)
        _fresh_thread_read(meter)  # rebase inside the burn
        time.sleep(0.8)
        reading = _fresh_thread_read(meter)
    finally:
        for p in procs:
            p.join()
    assert reading.percent is not None and reading.percent > 50.0, reading


def test_psutil_style_per_thread_state_is_not_used() -> None:
    clock = [0.0]
    seq = iter([Win(0, 0, 0, 0, 0), Win(50, 0, 50, 0, 0)])
    meter = CpuMeter(lambda: next(seq), clock=lambda: clock[0])
    assert _fresh_thread_read(meter).reason is CpuUnknown.NO_BASELINE
    clock[0] = 1.0
    assert _fresh_thread_read(meter).percent == pytest.approx(50.0)


def test_all_counters_equal_is_no_time_not_zero() -> None:
    t = Win(5, 5, 5, 5, 5)
    r = busy_percent(t, t)
    assert r.percent is None and r.reason is CpuUnknown.NO_TIME


def test_raising_read_keeps_baseline_and_never_reports_zero() -> None:
    clock = [0.0]
    items: list[Any] = [Win(0, 0, 0, 0, 0), OSError("x"), Win(10, 0, 10, 0, 0)]
    it = iter(items)

    def read() -> Any:
        v = next(it)
        if isinstance(v, BaseException):
            raise v
        return v

    m = CpuMeter(read, clock=lambda: clock[0])
    m.read()
    clock[0] = 1
    bad = m.read()
    assert bad.percent is None and bad.reason is CpuUnknown.READ_FAILED
    clock[0] = 2
    assert m.read().percent == pytest.approx(50.0)


def test_sampler_stale_boundary_exactly_30s_then_unknown() -> None:
    now = [0.0]
    seq: list[Any] = [Win(0, 0, 0, 0, 0), Win(10, 0, 10, 0, 0)]
    calls = iter(seq)

    def read() -> Any:
        try:
            return next(calls)
        except StopIteration:
            raise OSError("gone") from None

    s = ResourceSampler(
        mounts=[], cpu_times=read, clock=lambda: now[0], min_cpu_window_s=0.0
    )
    s.prime()
    now[0] = 1.0
    assert s.sample().cpu_percent == pytest.approx(50.0)
    now[0] = 31.0  # exactly 30 s after the good read
    stale = s.sample()
    assert stale.cpu_percent == pytest.approx(50.0)
    assert any("stale" in d for d in stale.degraded)
    now[0] = 31.5
    unknown = s.sample()
    assert unknown.cpu_percent is None
    assert any("unknown" in d for d in unknown.degraded)


def test_monitor_stats_has_cpu_unknown_counter() -> None:
    assert MonitorStats().cpu_unknown == 0


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("MESHFORGE_INSTALL_MODE", "user")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_doctor_cpu_real_machine_json_one_second(isolated: Path) -> None:
    pytest.importorskip("psutil")
    done = CliRunner().invoke(app, ["doctor", "cpu", "--seconds", "2", "--json"])
    assert done.exit_code == 0, done.output
    lines = [json.loads(x) for x in done.stdout.splitlines()]
    assert "caps" in lines[0] and len(lines) == 3
    for s in lines[1:]:
        assert s["cpu_percent"] is not None and 0 <= s["cpu_percent"] <= 100


@pytest.mark.parametrize("bad", ["0", "3601", "abc"])
def test_doctor_cpu_rejects_bad_seconds(isolated: Path, bad: str) -> None:
    done = CliRunner().invoke(app, ["doctor", "cpu", "--seconds", bad])
    assert done.exit_code == 2


@pytest.mark.parametrize("bad", ["0", "-1", "5000"])
def test_doctor_cpu_rejects_bad_job_cpus(isolated: Path, bad: str) -> None:
    done = CliRunner().invoke(app, ["doctor", "cpu", "--job-cpus", bad])
    assert done.exit_code == 2


def test_doctor_cpu_names_an_invalid_live_policy_file(isolated: Path) -> None:
    from meshforge.agent.main import _LOCAL_ONLY, _load_settings
    from meshforge.agent.policy import policy_path

    settings = _load_settings(None, **_LOCAL_ONLY)
    path = policy_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json or toml ][", encoding="utf-8")
    done = CliRunner().invoke(app, ["doctor", "cpu", "--seconds", "1", "--json"])
    assert done.exit_code in (0, 1), done.output
    head = json.loads(done.stdout.splitlines()[0])
    assert "INVALID" in head["policy_source"]
