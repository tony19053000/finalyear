"""The per-laptop resource sampler behind ``soak.py run`` (D410 rules 2-6).

Every ``interval_s`` one **tick** record is appended to
``<run dir>/samples.<label>.jsonl``: for each process target its identity
``(pid, create_time)`` and counters (RSS, FDs on Linux / handles on Windows,
threads, CPU seconds; PostgreSQL summed over the postmaster and its backends);
every ``dirs_every_s`` the byte size of each watched folder (agent cache, logs)
and the free space of its file system; every ``db_every_s`` (controller laptop
only) the database probe (:mod:`.db`). Changes of a target's identity are
**events** in the same file (``process_found`` / ``process_restarted`` /
``process_missing``) - the crash rule reads them.

**Resumable.** Nothing lives only in memory: on start the file is read back,
the last known identity of every target is restored, and a ``sampler_resumed``
event records the gap. Kill the sampler (or reboot the laptop), run the same
command again, and it continues until the plan's end time - the wall clock, so
time the sampler was down is a visible gap, never silently "observed".
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

from _fabric.clock import Clock, iso
from _fabric.rss import (
    ProcInfo,
    ProcMetrics,
    RssError,
    is_agent_command,
    is_controller_command,
    pick_leaf,
    pick_postmaster,
)
from _soak import jsonl
from _soak.stats import parse_ts

ROLES: Final = ("controller", "agent", "postgres")
SAMPLES_SCHEMA: Final = "meshforge.soak-samples/1"


@dataclass(frozen=True, slots=True)
class ProcTarget:
    """One process to follow. ``match``: a substring of its command line that
    tells it apart when several of its role run here (the dry run's three
    agents on one machine; a laptop that also runs a test PostgreSQL)."""

    name: str
    role: str
    host: str
    match: str | None = None

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}: {self.role!r}")

    @classmethod
    def parse(cls, text: str, default_host: str) -> ProcTarget:
        """``NAME=ROLE[@HOST][:MATCH]``, e.g. ``agent=agent``,
        ``a1=agent@A1:agent1.toml``, ``pg=postgres:/var/lib/meshforge/db``."""
        name, sep, rest = text.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"--target wants NAME=ROLE[@HOST][:MATCH], got {text!r}")
        spec, _, match = rest.partition(":")
        role, _, host = spec.partition("@")
        return cls(
            name=name.strip(),
            role=role.strip(),
            host=host.strip() or default_host,
            match=match or None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "host": self.host,
            "match": self.match,
        }


@dataclass(frozen=True, slots=True)
class DirTarget:
    """A folder whose total size is watched (D410 rule 6); ``cap_bytes`` set =
    it must stay at or under that size (the agent cache's configured cap)."""

    name: str
    path: Path
    cap_bytes: int | None = None

    @classmethod
    def parse(cls, text: str) -> DirTarget:
        """``NAME=PATH[@CAP_MB]`` (``@`` because Windows paths contain ``:``)."""
        name, sep, rest = text.partition("=")
        if not sep or not name.strip() or not rest:
            raise ValueError(f"--dir wants NAME=PATH[@CAP_MB], got {text!r}")
        path, at, cap = rest.rpartition("@") if "@" in rest else (rest, "", "")
        cap_bytes = int(float(cap) * 1024 * 1024) if at and cap else None
        return cls(name=name.strip(), path=Path(path), cap_bytes=cap_bytes)

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": str(self.path), "cap_bytes": self.cap_bytes}


@dataclass(frozen=True, slots=True)
class SamplerConfig:
    label: str
    end_at: datetime
    interval_s: float = 60.0
    dirs_every_s: float = 300.0
    db_every_s: float = 300.0
    procs: tuple[ProcTarget, ...] = ()
    dirs: tuple[DirTarget, ...] = ()

    def __post_init__(self) -> None:
        if self.interval_s <= 0 or self.dirs_every_s <= 0 or self.db_every_s <= 0:
            raise ValueError("intervals must be positive")
        names = [p.name for p in self.procs] + [d.name for d in self.dirs]
        if len(set(names)) != len(names):
            raise ValueError(f"target names must be unique: {names}")


class ProcSource(Protocol):
    def processes(self) -> list[ProcInfo]: ...
    def metrics(
        self, ident: tuple[int, float], *, tree: bool = False
    ) -> ProcMetrics | None: ...


DbProbe = Callable[[], dict[str, Any]]
DirSizer = Callable[[Path], int | None]
DiskFree = Callable[[Path], tuple[int, int] | None]


def dir_bytes(path: Path) -> int | None:
    """Total bytes of the regular files under ``path`` (symlinks not followed);
    ``None`` if it does not exist or cannot be read."""
    if not path.is_dir():
        return None
    total = 0
    stack = [path]
    try:
        while stack:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue  # a file removed while we walked
    except OSError:
        return None
    return total


def disk_free(path: Path) -> tuple[int, int] | None:
    """``(total, free)`` bytes of the file system holding ``path``."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None
    return int(usage.total), int(usage.free)


def _matcher(target: ProcTarget) -> Callable[[ProcInfo], bool]:
    role_ok: Callable[[str, Sequence[str]], bool] = (
        is_agent_command if target.role == "agent" else is_controller_command
    )

    def match(p: ProcInfo) -> bool:
        if target.match and target.match not in " ".join(p.cmdline):
            return False
        return role_ok(p.name, p.cmdline)

    return match


def locate(target: ProcTarget, procs: list[ProcInfo]) -> ProcInfo:
    """The process ``target`` names now; :class:`RssError` none / ambiguous."""
    if target.role == "postgres":
        needle = target.match
        pool = (
            procs
            if not needle
            else _with_children(procs, lambda p: needle in " ".join(p.cmdline))
        )
        return pick_postmaster(pool)
    return pick_leaf(procs, _matcher(target), target.role)


def _with_children(
    procs: list[ProcInfo], root_match: Callable[[ProcInfo], bool]
) -> list[ProcInfo]:
    """The processes matching ``root_match`` and their descendants."""
    children: dict[int, list[ProcInfo]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p)
    out: list[ProcInfo] = []
    stack = [p for p in procs if root_match(p)]
    seen: set[int] = set()
    while stack:
        p = stack.pop()
        if p.pid in seen:
            continue
        seen.add(p.pid)
        out.append(p)
        stack.extend(children.get(p.pid, []))
    return out


@dataclass
class _Followed:
    ident: tuple[int, float] | None = None
    missing: bool = False


@dataclass
class Sampler:
    """One sampling run (new or resumed) over ``<run dir>/samples.<label>.jsonl``."""

    cfg: SamplerConfig
    path: Path
    clock: Clock
    source: ProcSource
    db_probe: DbProbe | None = None
    sizer: DirSizer = dir_bytes
    free: DiskFree = disk_free
    say: Callable[[str], None] = lambda _text: None
    _followed: dict[str, _Followed] = field(default_factory=dict)
    _next_dirs: float = 0.0
    _next_db: float = 0.0
    ticks: int = 0

    # ------------------------------------------------------------ records
    def _append(self, record: dict[str, Any]) -> None:
        jsonl.append(self.path, record)

    def _event(self, kind: str, target: str | None, **extra: Any) -> None:
        record: dict[str, Any] = {
            "type": "event",
            "at": iso(self.clock.wall()),
            "label": self.cfg.label,
            "kind": kind,
            "target": target,
            **extra,
        }
        self._append(record)
        self.say(f"{record['at']} {kind} {target or ''} {extra or ''}".rstrip())

    # ------------------------------------------------------------- start
    def start(self) -> None:
        loaded = jsonl.load(self.path)
        header = next((r for r in loaded.records if r.get("type") == "header"), None)
        if header is None:
            self._append(
                {
                    "type": "header",
                    "schema": SAMPLES_SCHEMA,
                    "label": self.cfg.label,
                    "started_at": iso(self.clock.wall()),
                    "end_at": iso(self.cfg.end_at),
                    "interval_s": self.cfg.interval_s,
                    "procs": [p.as_dict() for p in self.cfg.procs],
                    "dirs": [d.as_dict() for d in self.cfg.dirs],
                    "os": os.name,
                }
            )
            return
        if header.get("label") != self.cfg.label:
            raise ValueError(
                f"{self.path.name} belongs to {header.get('label')!r}, not {self.cfg.label!r}"
            )
        last_at: str | None = None
        for record in loaded.records:
            if record.get("type") == "tick":
                last_at = record.get("at")
                for name, proc in (record.get("procs") or {}).items():
                    if isinstance(proc, dict) and proc.get("pid") is not None:
                        self._followed[name] = _Followed(
                            (int(proc["pid"]), float(proc["create_time"]))
                        )
            elif (
                record.get("type") == "event"
                and record.get("kind") == "process_missing"
            ):
                self._followed.setdefault(
                    str(record.get("target")), _Followed()
                ).missing = True
        self._event(
            "sampler_resumed",
            None,
            last_tick_at=last_at,
            torn_tail=loaded.torn_tail,
        )

    # -------------------------------------------------------------- tick
    def tick(self) -> dict[str, Any]:
        procs_rec: dict[str, Any] = {}
        listing: list[ProcInfo] | None = None
        for target in self.cfg.procs:
            followed = self._followed.setdefault(target.name, _Followed())
            tree = target.role == "postgres"
            metrics = None
            if followed.ident is not None:
                metrics = self._metrics(target, followed.ident, tree)
            if metrics is None:
                if listing is None:
                    listing = self.source.processes()
                metrics = self._relocate(target, followed, listing, tree)
            if metrics is None or followed.ident is None:
                procs_rec[target.name] = None
                continue
            procs_rec[target.name] = {
                "pid": followed.ident[0],
                "create_time": followed.ident[1],
                "rss_bytes": metrics.rss_bytes,
                "fds": metrics.fds,
                "handles": metrics.handles,
                "threads": metrics.threads,
                "cpu_s": round(metrics.cpu_s, 3),
                "procs": metrics.procs,
            }
        record: dict[str, Any] = {
            "type": "tick",
            "at": iso(self.clock.wall()),
            "label": self.cfg.label,
            "procs": procs_rec,
        }
        now = self.clock.monotonic()
        if self.cfg.dirs and now >= self._next_dirs:
            record["dirs"] = self._dirs()
            self._next_dirs = now + self.cfg.dirs_every_s
        if self.db_probe is not None and now >= self._next_db:
            record["db"] = self.db_probe()
            self._next_db = now + self.cfg.db_every_s
        self._append(record)
        self.ticks += 1
        return record

    def _metrics(
        self, target: ProcTarget, ident: tuple[int, float], tree: bool
    ) -> ProcMetrics | None:
        try:
            return self.source.metrics(ident, tree=tree)
        except RssError as exc:
            self._event("metrics_error", target.name, failure_type=exc.failure_type)
            return None

    def _relocate(
        self,
        target: ProcTarget,
        followed: _Followed,
        listing: list[ProcInfo],
        tree: bool,
    ) -> ProcMetrics | None:
        try:
            found = locate(target, listing)
        except RssError as exc:
            if not followed.missing:
                followed.missing = True
                self._event(
                    "process_missing",
                    target.name,
                    role=target.role,
                    host=target.host,
                    failure_type=exc.failure_type,
                    last_pid=followed.ident[0] if followed.ident else None,
                )
            return None
        if followed.ident != found.ident:
            kind = "process_found" if followed.ident is None else "process_restarted"
            self._event(
                kind,
                target.name,
                role=target.role,
                host=target.host,
                pid=found.pid,
                create_time=found.create_time,
                old_pid=followed.ident[0] if followed.ident else None,
            )
            followed.ident = found.ident
        followed.missing = False
        return self._metrics(target, found.ident, tree)

    def _dirs(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for d in self.cfg.dirs:
            fs = self.free(d.path)
            out[d.name] = {
                "bytes": self.sizer(d.path),
                "cap_bytes": d.cap_bytes,
                "fs_total": fs[0] if fs else None,
                "fs_free": fs[1] if fs else None,
            }
        return out

    # --------------------------------------------------------------- loop
    def run(self) -> int:
        """Tick until the plan's end (wall clock); returns the ticks taken."""
        self.start()
        next_at = self.clock.monotonic()
        while self.clock.wall() < self.cfg.end_at:
            self.tick()
            next_at += self.cfg.interval_s
            now = self.clock.monotonic()
            if next_at < now:  # we fell behind (laptop suspended): no burst
                next_at = now
            remaining = (self.cfg.end_at - self.clock.wall()).total_seconds()
            self.clock.sleep(max(0.0, min(next_at - now, remaining)))
        self._event("sampler_done", None)
        return self.ticks


def end_from(start: datetime, hours: float) -> datetime:
    return start + timedelta(hours=hours)


def read_samples(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """All records of one samples file (header, ticks, events) + torn flag."""
    loaded = jsonl.load(path)
    return loaded.records, loaded.torn_tail


def tick_time(record: Mapping[str, Any]) -> datetime | None:
    return parse_ts(record.get("at"))
