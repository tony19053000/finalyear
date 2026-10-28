"""V1.0 G1 (roadmap 1.0.2): the soak sampler, its files and process matching.

The sampler must (a) follow each process by identity and report restarts,
(b) survive being killed - a re-run resumes from its own file without a false
restart - and (c) never lose more than the line being written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from _fabric.rss import (
    ProcInfo,
    ProcMetrics,
    RssError,
    is_agent_command,
    is_controller_command,
    pick_agent,
    pick_postmaster,
)
from _soak import jsonl
from _soak.sampler import (
    DirTarget,
    ProcTarget,
    Sampler,
    SamplerConfig,
    dir_bytes,
    locate,
)

pytestmark = pytest.mark.unit
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    t: float = 0.0

    def monotonic(self) -> float:
        return self.t

    def wall(self) -> datetime:
        return T0 + timedelta(seconds=self.t)

    def sleep(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class FakeSource:
    procs: list[ProcInfo] = field(default_factory=list)
    rss: dict[tuple[int, float], int] = field(default_factory=dict)
    denied: set[int] = field(default_factory=set)

    def processes(self) -> list[ProcInfo]:
        return list(self.procs)

    def metrics(
        self, ident: tuple[int, float], *, tree: bool = False
    ) -> ProcMetrics | None:
        if ident[0] in self.denied:
            raise RssError("access_denied", "no")
        if ident not in self.rss:
            return None
        return ProcMetrics(self.rss[ident], 30, None, 9, 1.5, 3 if tree else 1)

    def start(
        self,
        pid: int,
        cmd: tuple[str, ...],
        ct: float,
        ppid: int = 1,
        name: str = "python",
    ) -> None:
        self.procs.append(ProcInfo(pid, ppid, name, cmd, ct))
        self.rss[(pid, ct)] = 100 * 1024 * 1024

    def stop(self, pid: int) -> None:
        gone = [p for p in self.procs if p.pid == pid]
        self.procs = [p for p in self.procs if p.pid != pid]
        for p in gone:
            self.rss.pop(p.ident, None)


AGENT = ("python", "-m", "meshforge.agent", "run", "--config", "/x/agent1.toml")
CONTROLLER = ("python", "-m", "meshforge.controller", "--config", "/x/controller.toml")


def _cfg(**kw: Any) -> SamplerConfig:
    values: dict[str, Any] = {
        "label": "L2",
        "end_at": T0 + timedelta(seconds=300),
        "interval_s": 60.0,
        "procs": (ProcTarget("agent", "agent", "L2"),),
    }
    values.update(kw)
    return SamplerConfig(**values)


def _events(path: Path) -> list[tuple[str, Any]]:
    return [
        (r["kind"], r.get("pid"))
        for r in jsonl.load(path).records
        if r.get("type") == "event"
    ]


def test_restart_is_an_event_and_resume_is_not(tmp_path: Path) -> None:
    path = tmp_path / "samples.l2.jsonl"
    clock, source = FakeClock(), FakeSource()
    source.start(10, AGENT, 1.0)
    sampler = Sampler(_cfg(), path, clock, source)
    sampler.start()
    sampler.tick()
    source.stop(10)
    sampler.tick()  # gone: missing
    source.start(11, AGENT, 2.0)
    sampler.tick()  # back with a new identity: restarted
    assert _events(path) == [
        ("process_found", 10),
        ("process_missing", None),
        ("process_restarted", 11),
    ]
    # the sampler is killed and started again: same process, no new restart
    again = Sampler(_cfg(), path, clock, source)
    again.start()
    again.tick()
    kinds = [k for k, _ in _events(path)]
    assert kinds[-1] == "sampler_resumed"
    assert kinds.count("process_restarted") == 1
    ticks = [r for r in jsonl.load(path).records if r.get("type") == "tick"]
    assert [t["procs"]["agent"] and t["procs"]["agent"]["pid"] for t in ticks] == [
        10,
        None,
        11,
        11,
    ]


def test_run_stops_at_the_plan_end_and_samples_db_and_dirs(tmp_path: Path) -> None:
    clock, source = FakeClock(), FakeSource()
    source.start(10, AGENT, 1.0)
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "a.bin").write_bytes(b"x" * 1000)
    probes: list[int] = []

    def probe() -> dict[str, Any]:
        probes.append(1)
        return {"ok": True, "db_bytes": 1}

    cfg = _cfg(dirs=(DirTarget("cache", tmp_path / "cache", 2000),), db_every_s=120.0)
    sampler = Sampler(cfg, tmp_path / "s.jsonl", clock, source, db_probe=probe)
    assert sampler.run() == 5  # t = 0, 60, 120, 180, 240; end at 300
    ticks = [
        r for r in jsonl.load(tmp_path / "s.jsonl").records if r.get("type") == "tick"
    ]
    assert ticks[0]["dirs"]["cache"]["bytes"] == 1000
    assert ticks[0]["dirs"]["cache"]["cap_bytes"] == 2000
    assert len(probes) == 3  # 0, 120, 240


def test_access_denied_is_recorded_not_fatal(tmp_path: Path) -> None:
    clock, source = FakeClock(), FakeSource()
    source.start(10, AGENT, 1.0)
    source.denied.add(10)
    sampler = Sampler(_cfg(), tmp_path / "s.jsonl", clock, source)
    sampler.start()
    tick = sampler.tick()
    assert tick["procs"]["agent"] is None
    assert ("metrics_error", None) in _events(tmp_path / "s.jsonl")


def test_samples_file_of_another_label_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    Sampler(_cfg(), path, FakeClock(), FakeSource()).start()
    with pytest.raises(ValueError, match="belongs to"):
        Sampler(_cfg(label="L3"), path, FakeClock(), FakeSource()).start()


# ---------------------------------------------------------------- jsonl
def test_torn_last_line_is_skipped_and_repaired(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    jsonl.append(path, {"a": 1})
    with path.open("ab") as fh:
        fh.write(b'{"a": 2, "b"')  # power cut mid-write
    loaded = jsonl.load(path)
    assert loaded.records == [{"a": 1}]
    assert loaded.torn_tail
    jsonl.append(path, {"a": 3})  # the next run appends after the fragment
    loaded = jsonl.load(path)
    assert loaded.records == [{"a": 1}, {"a": 3}]
    assert loaded.torn_tail


def test_corrupt_line_in_the_middle_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text('{"a": 1}\n[1, 2]\n{"a": 2}\n', encoding="utf-8")
    with pytest.raises(jsonl.JsonlError, match=":2:"):
        jsonl.load(path)


# ------------------------------------------------------- target parsing
def test_target_and_dir_parsing() -> None:
    t = ProcTarget.parse("a1=agent@A1:agent1.toml", "L1")
    assert (t.name, t.role, t.host, t.match) == ("a1", "agent", "A1", "agent1.toml")
    assert ProcTarget.parse("pg=postgres", "L1").host == "L1"
    with pytest.raises(ValueError, match="role"):
        ProcTarget.parse("x=worker", "L1")
    d = DirTarget.parse(r"cache=C:\ProgramData\MeshForge\cache@512")
    assert d.path == Path(r"C:\ProgramData\MeshForge\cache")
    assert d.cap_bytes == 512 * 1024 * 1024
    assert DirTarget.parse("logs=/var/log/x").cap_bytes is None
    with pytest.raises(ValueError, match="unique"):
        SamplerConfig("L1", T0, procs=(ProcTarget("a", "agent", "L1"),),
                      dirs=(DirTarget("a", Path("/x")),))  # fmt: skip


def test_dir_bytes_walks_without_following_links(tmp_path: Path) -> None:
    (tmp_path / "d" / "e").mkdir(parents=True)
    (tmp_path / "d" / "f").write_bytes(b"12345")
    (tmp_path / "d" / "e" / "g").write_bytes(b"123")
    assert dir_bytes(tmp_path / "d") == 8
    assert dir_bytes(tmp_path / "missing") is None


# --------------------------------------------------- process recognition
def test_controller_server_vs_admin_commands() -> None:
    assert is_controller_command("python", CONTROLLER)
    assert is_controller_command(
        "meshforge-controller",
        ("/opt/meshforge/controller/current/bin/meshforge-controller",),
    )
    assert not is_controller_command("python", ("meshforge-controller", "db", "status"))
    assert not is_controller_command("uv", ("uv", "run", "meshforge-controller"))
    assert not is_controller_command("python", AGENT)


def test_windows_service_agent_child_is_the_agent() -> None:
    child = (
        "C:\\MF\\python.exe",
        "-I",
        "-m",
        "meshforge.agent.winservice",
        "child",
        "--",
        "run",
    )
    host = (
        "C:\\MF\\python.exe",
        "-I",
        "-m",
        "meshforge.agent.winservice",
        "host",
        "--config",
        "c",
    )
    assert is_agent_command("python.exe", child)
    assert not is_agent_command("python.exe", host)
    procs = [
        ProcInfo(5, 1, "python.exe", host, 1.0),
        ProcInfo(6, 5, "python.exe", child, 2.0),
    ]
    assert pick_agent(procs).pid == 6


def test_postmaster_is_the_root_of_the_postgres_tree() -> None:
    procs = [
        ProcInfo(1, 0, "systemd", (), 0.0),
        ProcInfo(20, 1, "python", CONTROLLER, 1.0),
        ProcInfo(
            21, 20, "postgres", ("postgres", "-D", "/var/lib/meshforge/db/data"), 2.0
        ),
        ProcInfo(22, 21, "postgres", ("postgres: checkpointer",), 3.0),
    ]
    assert pick_postmaster(procs).pid == 21
    other = ProcInfo(30, 1, "postgres", ("postgres", "-D", "/other"), 4.0)
    with pytest.raises(RssError, match="--target") as err:
        pick_postmaster([*procs, other])
    assert err.value.failure_type == "postgres_ambiguous"
    target = ProcTarget("pg", "postgres", "L1", match="/var/lib/meshforge/db")
    assert locate(target, [*procs, other]).pid == 21


def test_two_agents_need_a_match() -> None:
    other = ("python", "-m", "meshforge.agent", "run", "--config", "/x/agent2.toml")
    procs = [
        ProcInfo(10, 1, "python", AGENT, 1.0),
        ProcInfo(11, 1, "python", other, 1.0),
    ]
    with pytest.raises(RssError) as err:
        locate(ProcTarget("a", "agent", "L1"), procs)
    assert err.value.failure_type == "agent_ambiguous"
    assert locate(ProcTarget("a", "agent", "L1", "agent2.toml"), procs).pid == 11
