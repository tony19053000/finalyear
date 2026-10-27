"""Find the running ``meshforge-agent`` process and sample its resident memory
(V0.3 success criterion 5, part 2: agent RSS growth < 10 % over the 24 h soak).

**How the agent process is found (Linux and Windows).** Every documented way of
starting the agent (docs/OPERATIONS.md: by hand, the systemd user unit, the Task
Scheduler task) runs ``uv run meshforge-agent run``, which makes a small tree:

* Linux: ``uv run meshforge-agent run`` -> ``python .venv/bin/meshforge-agent run``
  (the kernel shows that process as ``meshforge-agent``);
* Windows: ``uv.exe run meshforge-agent run`` -> ``meshforge-agent.exe run`` (uv's
  launcher, a few MB) -> ``python.exe ... meshforge-agent.exe run`` (the agent).

A process is a *candidate* when one of its command-line arguments is
``meshforge-agent`` (any directory, ``.exe``/``-script.py`` stripped, any case) or
``-m meshforge.agent``, followed later by ``run`` - and it is not ``uv`` itself.
Of the candidates, the one that has **no candidate descendant** is the agent: the
Python interpreter at the bottom of the tree, never the ``uv`` wrapper or the
Windows launcher. More than one such process means two agents are running (the
agent refuses a second one on the same identity, exit 5, so this is a second
checkout); the tool then stops and asks for ``--pid``.

A process is identified by ``(pid, create_time)``, so a reused pid is never taken
for the same process; a different identity is an agent **restart**.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

MIB: Final = 1024 * 1024
KIND: Final = "agent-rss"
_SUFFIX_RE: Final = re.compile(r"(\.exe|-script\.pyw?|\.pyw?)$", re.IGNORECASE)
_WRAPPERS: Final = frozenset({"uv", "uvx"})


class RssError(Exception):
    def __init__(self, failure_type: str, message: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type


class PsutilMissing(RssError):  # noqa: N818 (reads as a state)
    def __init__(self) -> None:
        super().__init__(
            "psutil_missing",
            "psutil is not installed here - it comes with the agent extra: "
            "run `uv sync --extra agent` (every agent laptop has it)",
        )


@dataclass(frozen=True, slots=True)
class ProcInfo:
    pid: int
    ppid: int
    name: str
    cmdline: tuple[str, ...]
    create_time: float

    @property
    def ident(self) -> tuple[int, float]:
        return (self.pid, self.create_time)


def _base(arg: str) -> str:
    """``C:\\x\\meshforge-agent.exe`` / ``/x/meshforge-agent`` -> ``meshforge-agent``."""
    last = re.split(r"[\\/]", arg)[-1]
    return _SUFFIX_RE.sub("", last).casefold()


def is_agent_command(name: str, cmdline: Sequence[str]) -> bool:
    if _base(name) in _WRAPPERS or (cmdline and _base(cmdline[0]) in _WRAPPERS):
        return False
    bases = [_base(a) for a in cmdline]
    for i, arg in enumerate(bases):
        entry = arg == "meshforge-agent" or (
            arg == "-m" and i + 1 < len(bases) and cmdline[i + 1] == "meshforge.agent"
        )
        if entry and "run" in bases[i + 1 :]:
            return True
        # V1.0 G1: the Windows service's supervised agent child
        # (``python -I -m meshforge.agent.winservice child -- run``, D386).
        if (
            arg == "-m"
            and i + 2 < len(bases)
            and cmdline[i + 1] == "meshforge.agent.winservice"
            and bases[i + 2] == "child"
        ):
            return True
    return False


CONTROLLER_ADMIN_COMMANDS: Final = frozenset(
    {"token", "fabric", "node", "user", "ca", "setup", "db", "--version", "--help"}
)
"""``meshforge-controller <sub>`` admin commands: short-lived, never the server."""


def is_controller_command(name: str, cmdline: Sequence[str]) -> bool:
    """V1.0 G1: the controller *server* (``meshforge-controller`` / ``python -m
    meshforge.controller`` with no admin subcommand; the uv wrapper excluded)."""
    if _base(name) in _WRAPPERS or (cmdline and _base(cmdline[0]) in _WRAPPERS):
        return False
    bases = [_base(a) for a in cmdline]
    for i, arg in enumerate(bases):
        entry = arg == "meshforge-controller" or (
            arg == "-m"
            and i + 1 < len(bases)
            and cmdline[i + 1] == "meshforge.controller"
        )
        if entry:
            rest = cmdline[i + 2 if arg == "-m" else i + 1 :]
            return not any(a in CONTROLLER_ADMIN_COMMANDS for a in rest)
    return False


def is_postgres_process(name: str, cmdline: Sequence[str]) -> bool:
    """A PostgreSQL server process (postmaster or backend; Linux and Windows)."""
    del cmdline
    return _base(name) in ("postgres", "postmaster")


def pick_leaf(
    procs: Iterable[ProcInfo], match: Callable[[ProcInfo], bool], what: str
) -> ProcInfo:
    """The one matching process with no matching descendant (the interpreter at
    the bottom of a ``uv run`` / launcher tree, :func:`pick_agent`'s rule);
    :class:`RssError` ``<what>_not_found`` / ``<what>_ambiguous``."""
    by_pid = {p.pid: p for p in procs}
    candidates = {p.pid: p for p in by_pid.values() if match(p)}
    if not candidates:
        raise RssError(f"{what}_not_found", f"no running {what} process found")
    ancestors: set[int] = set()
    for proc in candidates.values():
        seen: set[int] = set()
        parent = by_pid.get(proc.ppid)
        while parent is not None and parent.pid not in seen:
            seen.add(parent.pid)
            if parent.pid in candidates:
                ancestors.add(parent.pid)
            parent = by_pid.get(parent.ppid)
    leaves = [p for pid, p in sorted(candidates.items()) if pid not in ancestors]
    if len(leaves) != 1:
        pids = ", ".join(str(p.pid) for p in leaves)
        raise RssError(
            f"{what}_ambiguous",
            f"{len(leaves)} {what} processes are running (pids {pids}); "
            "name the one to sample with --target",
        )
    return leaves[0]


def pick_postmaster(procs: Iterable[ProcInfo]) -> ProcInfo:
    """The PostgreSQL postmaster: a postgres process whose parent is not one
    (its backends are its children). :class:`RssError` when none / several."""
    by_pid = {p.pid: p for p in procs}
    roots = [
        p
        for pid, p in sorted(by_pid.items())
        if is_postgres_process(p.name, p.cmdline)
        and not (
            p.ppid in by_pid
            and is_postgres_process(by_pid[p.ppid].name, by_pid[p.ppid].cmdline)
        )
    ]
    if not roots:
        raise RssError("postgres_not_found", "no running PostgreSQL server found")
    if len(roots) != 1:
        pids = ", ".join(str(p.pid) for p in roots)
        raise RssError(
            "postgres_ambiguous",
            f"{len(roots)} PostgreSQL servers are running (pids {pids}); "
            "name the one to sample with --target",
        )
    return roots[0]


def pick_agent(procs: Iterable[ProcInfo]) -> ProcInfo:
    """The agent among ``procs``; :class:`RssError` when none or ambiguous."""
    try:
        return pick_leaf(procs, lambda p: is_agent_command(p.name, p.cmdline), "agent")
    except RssError as exc:
        if exc.failure_type == "agent_not_found":
            raise RssError(
                "agent_not_found", "no running meshforge-agent process found"
            ) from None
        raise RssError(
            "agent_ambiguous",
            str(exc).replace("name the one to sample with --target", "pass --pid"),
        ) from None


@dataclass(frozen=True, slots=True)
class ProcMetrics:
    """One process's (or tree's) counters; ``None`` = not readable here."""

    rss_bytes: int
    fds: int | None
    handles: int | None
    threads: int
    cpu_s: float
    procs: int = 1

    def plus(self, other: ProcMetrics) -> ProcMetrics:
        def add(a: int | None, b: int | None) -> int | None:
            return None if a is None or b is None else a + b

        return ProcMetrics(
            rss_bytes=self.rss_bytes + other.rss_bytes,
            fds=add(self.fds, other.fds),
            handles=add(self.handles, other.handles),
            threads=self.threads + other.threads,
            cpu_s=self.cpu_s + other.cpu_s,
            procs=self.procs + other.procs,
        )


def _counter(proc: Any, method: str, ps: Any) -> int | None:
    """``num_fds`` (POSIX) / ``num_handles`` (Windows): ``None`` when this OS
    has no such counter or we may not read it (AccessDenied)."""
    fn = getattr(proc, method, None)
    if fn is None:
        return None
    try:
        return int(fn())
    except (ps.AccessDenied, ps.ZombieProcess):
        return None


class ProcessSource(Protocol):
    def processes(self) -> list[ProcInfo]: ...
    def rss(self, ident: tuple[int, float]) -> int | None:
        """Resident bytes of that exact process, ``None`` if it is gone."""


class PsutilSource:
    """The real process table. ``within``: only that pid and its descendants
    (tests use it so a real agent running on the developer's machine is ignored)."""

    def __init__(self, *, within: int | None = None) -> None:
        try:
            import psutil  # type: ignore[import-untyped]  # agent extra, no stubs
        except ImportError:
            raise PsutilMissing from None
        self._psutil: Any = psutil
        self._within = within

    def processes(self) -> list[ProcInfo]:
        ps = self._psutil
        allowed: set[int] | None = None
        if self._within is not None:
            try:
                root = ps.Process(self._within)
                allowed = {root.pid} | {c.pid for c in root.children(recursive=True)}
            except ps.Error:
                return []
        out: list[ProcInfo] = []
        for proc in ps.process_iter(["pid", "ppid", "name", "cmdline", "create_time"]):
            info = proc.info
            if allowed is not None and info["pid"] not in allowed:
                continue
            out.append(
                ProcInfo(
                    pid=int(info["pid"]),
                    ppid=int(info.get("ppid") or 0),
                    name=str(info.get("name") or ""),
                    cmdline=tuple(str(a) for a in (info.get("cmdline") or ())),
                    create_time=float(info.get("create_time") or 0.0),
                )
            )
        return out

    def rss(self, ident: tuple[int, float]) -> int | None:
        ps = self._psutil
        try:
            proc = ps.Process(ident[0])
            if abs(float(proc.create_time()) - ident[1]) > 0.01:
                return None  # the pid was reused by another process
            return int(proc.memory_info().rss)
        except ps.Error:
            return None

    def metrics(
        self, ident: tuple[int, float], *, tree: bool = False
    ) -> ProcMetrics | None:
        """V1.0 G1: RSS, open files (Linux ``/proc/<pid>/fd`` via psutil
        ``num_fds``) or handles (Windows ``num_handles``), threads and CPU
        seconds of that exact process - summed over it and its descendants when
        ``tree`` (PostgreSQL: postmaster + backends). ``None`` if it is gone. A
        counter the OS will not show us (another user's process without root)
        is ``None``, never a guess."""
        ps = self._psutil
        try:
            root = ps.Process(ident[0])
            if abs(float(root.create_time()) - ident[1]) > 0.01:
                return None
            procs = [root, *root.children(recursive=True)] if tree else [root]
        except ps.Error:
            return None
        total = ProcMetrics(
            rss_bytes=0, fds=0, handles=0, threads=0, cpu_s=0.0, procs=0
        )
        for proc in procs:
            try:
                with proc.oneshot():
                    rss = int(proc.memory_info().rss)
                    threads = int(proc.num_threads())
                    times = proc.cpu_times()
                    fds = _counter(proc, "num_fds", ps)
                    handles = _counter(proc, "num_handles", ps)
            except ps.AccessDenied:
                if proc is root:
                    raise RssError(
                        "access_denied",
                        f"may not read process {ident[0]}: run the sampler as "
                        "root / an Administrator PowerShell",
                    ) from None
                continue
            except ps.Error:
                if proc is root:
                    return None
                continue  # a backend that ended between listing and reading
            total = total.plus(
                ProcMetrics(
                    rss_bytes=rss,
                    fds=fds,
                    handles=handles,
                    threads=threads,
                    cpu_s=float(times.user) + float(times.system),
                    procs=1,
                )
            )
        return total

    def by_pid(self, pid: int) -> ProcInfo:
        ps = self._psutil
        try:
            proc = ps.Process(pid)
            return ProcInfo(
                pid=pid,
                ppid=int(proc.ppid()),
                name=str(proc.name()),
                cmdline=tuple(proc.cmdline()),
                create_time=float(proc.create_time()),
            )
        except ps.Error:
            raise RssError("agent_not_found", f"no process with pid {pid}") from None


# ------------------------------------------------------------------ summary
def summarize(state: dict[str, Any]) -> dict[str, Any]:
    """Growth over the longest-lived agent process (see module docstring)."""
    samples: list[dict[str, Any]] = state["samples"]
    segments: dict[tuple[int, float], list[dict[str, Any]]] = {}
    for s in samples:
        segments.setdefault((int(s["pid"]), float(s["create_time"])), []).append(s)
    out: dict[str, Any] = {
        "samples": len(samples),
        "pids": sorted({pid for pid, _ in segments}),
        "restarts": max(len(segments) - 1, 0),
        "not_found_events": sum(
            1 for e in state["events"] if e["kind"] == "agent_not_found"
        ),
        "observed_h": round(state["elapsed_s"] / 3600.0, 3),
        "target_h": state["hours"],
        "done": state["done"],
        "baseline_mib": None,
        "end_mib": None,
        "max_mib": None,
        "first_mib": None,
        "growth_pct": None,
        "max_growth_pct": None,
    }
    if not segments:
        return out
    main = max(segments.values(), key=len)
    warmup_s = float(state["warmup_min"]) * 60.0
    start = float(main[0]["elapsed_s"])
    after = [s for s in main if float(s["elapsed_s"]) - start >= warmup_s] or main
    baseline = int(after[0]["rss_bytes"])
    end = int(main[-1]["rss_bytes"])
    peak = max(int(s["rss_bytes"]) for s in after)
    out.update(
        baseline_mib=round(baseline / MIB, 2),
        end_mib=round(end / MIB, 2),
        max_mib=round(peak / MIB, 2),
        first_mib=round(int(main[0]["rss_bytes"]) / MIB, 2),
        growth_pct=round((end - baseline) / baseline * 100.0, 2) if baseline else None,
        max_growth_pct=round((peak - baseline) / baseline * 100.0, 2)
        if baseline
        else None,
        baseline_at=after[0]["at"],
        main_pid=int(main[0]["pid"]),
    )
    return out
