"""V1.0 G4 (D412) profiling kit for a local cluster fabric.

Imported as ``mf_profile`` by ``tests/cluster/test_v10_g4_profile.py``. Pure
measurement helpers: nothing here changes how the fabric behaves.

* :data:`HOOK_DIR` - the ``sitecustomize`` folder (``mf_profile_hook``) put on
  the fabric processes' ``PYTHONPATH``: ``SIGUSR2`` dumps (RSS, threads, live
  objects, asyncio tasks, tracemalloc) and dispatch/decide timings;
* :func:`summary` - nearest-rank percentiles (the planner_eval rule: integer
  rank, no interpolation);
* :class:`CpuWindow` - CPU% of processes over a window (psutil);
* :class:`Sampler` - RSS / threads / FDs of the fabric's processes every few s;
* :class:`EventTap` - ``/v1/events/ws`` frames with their receive time;
* :func:`dump` - ask a process for a hook dump and wait for the file;
* :func:`pg_top` - top statements from ``pg_stat_statements`` + generic plans.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import signal
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

HOOK_DIR: Final = Path(__file__).resolve().parent / "mf_profile_hook"


def nearest_rank(values: Sequence[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summary(values: Iterable[float]) -> dict[str, Any]:
    vals = [float(v) for v in values if math.isfinite(float(v))]
    if not vals:
        return {"n": 0}

    def r(x: float | None) -> float | None:
        return None if x is None else round(x, 3)

    return {
        "n": len(vals),
        "p50": r(nearest_rank(vals, 50)),
        "p95": r(nearest_rank(vals, 95)),
        "p99": r(nearest_rank(vals, 99)),
        "max": r(max(vals)),
        "mean": r(sum(vals) / len(vals)),
    }


@dataclass
class CpuWindow:
    """CPU seconds of ``pids`` between :meth:`start` and :meth:`stop`."""

    pids: dict[str, int]
    _t0: float = 0.0
    _c0: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def _cpu(pid: int) -> float:
        import psutil

        t = psutil.Process(pid).cpu_times()
        return float(t.user + t.system)

    def start(self) -> CpuWindow:
        self._t0 = time.monotonic()
        self._c0 = {k: self._cpu(p) for k, p in self.pids.items()}
        return self

    def stop(self) -> dict[str, Any]:
        wall = time.monotonic() - self._t0
        out: dict[str, Any] = {"wall_s": round(wall, 2), "processes": {}}
        for k, p in self.pids.items():
            used = self._cpu(p) - self._c0[k]
            out["processes"][k] = {
                "cpu_s": round(used, 3),
                "cpu_pct": round(100.0 * used / wall, 3),
            }
        return out


def proc_row(pid: int) -> dict[str, Any] | None:
    import psutil

    try:
        p = psutil.Process(pid)
        with p.oneshot():
            return {
                "rss": int(p.memory_info().rss),
                "threads": int(p.num_threads()),
                "fds": int(p.num_fds()),
                "cpu_s": round(float(sum(p.cpu_times()[:2])), 3),
            }
    except psutil.Error:
        return None


def rss_slope(
    series: Sequence[tuple[float, int]], since_s: float, until_s: float = math.inf
) -> dict[str, Any]:
    """Least-squares RSS slope (MiB/h) of ``(t, rss_bytes)`` points in
    ``[since_s, until_s]``, with the first/last value: a leak keeps a positive
    slope over a long idle window, a filled bounded store does not."""
    pts = [(t, v / 2**20) for t, v in series if since_s <= t <= until_s]
    if len(pts) < 3:
        return {"n": len(pts)}
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mv = sum(v for _, v in pts) / n
    var = sum((t - mt) ** 2 for t, _ in pts)
    slope = sum((t - mt) * (v - mv) for t, v in pts) / var if var else 0.0
    return {
        "n": n,
        "span_s": round(pts[-1][0] - pts[0][0], 1),
        "first_mib": round(pts[0][1], 1),
        "last_mib": round(pts[-1][1], 1),
        "slope_mib_per_h": round(slope * 3600.0, 2),
    }


class Sampler(threading.Thread):
    """``pids()`` sampled every ``every_s``: ``rows`` = ``(t, label, row)``."""

    def __init__(self, pids: Callable[[], dict[str, int]], every_s: float) -> None:
        super().__init__(name="mf-profile-sampler", daemon=True)
        self._pids = pids
        self._every = every_s
        self._stop_evt = threading.Event()
        self.t0 = time.monotonic()
        self.rows: list[tuple[float, str, dict[str, Any]]] = []
        self.marks: list[tuple[float, str]] = []

    def mark(self, what: str) -> None:
        self.marks.append((round(time.monotonic() - self.t0, 2), what))

    def run(self) -> None:
        while not self._stop_evt.is_set():
            t = round(time.monotonic() - self.t0, 2)
            for label, pid in self._pids().items():
                row = proc_row(pid)
                if row is not None:
                    self.rows.append((t, label, row))
            self._stop_evt.wait(self._every)

    def stop(self) -> None:
        self._stop_evt.set()
        self.join(10)

    def series(self, label: str, key: str) -> list[tuple[float, int]]:
        return [(t, int(r[key])) for t, lbl, r in self.rows if lbl == label]


class EventTap(threading.Thread):
    """Every ``/v1/events/ws`` frame with its receive wall time (admin token:
    every event). ``frames`` = ``(recv_wall, frame)``."""

    def __init__(self, fabric: Any) -> None:
        super().__init__(name="mf-profile-events", daemon=True)
        self._fabric = fabric
        self._stop_evt = threading.Event()
        self.frames: list[tuple[float, dict[str, Any]]] = []
        self.error: str | None = None
        self.ready = threading.Event()
        self.terminal: set[str] = set()
        """Jobs seen reaching a terminal state (so waiting needs no polling)."""

    def run(self) -> None:
        from websockets.sync.client import connect

        url = self._fabric.http_base.replace("https://", "wss://") + "/v1/events/ws"
        headers = {"Authorization": f"Bearer {self._fabric.api_token()}"}
        try:
            with connect(
                url,
                additional_headers=headers,
                open_timeout=10,
                ssl=self._fabric.ssl_context,
                max_size=None,
            ) as ws:
                self.ready.set()
                while not self._stop_evt.is_set():
                    with contextlib.suppress(TimeoutError):
                        raw = ws.recv(timeout=0.5)
                        frame = json.loads(raw)
                        self.frames.append((time.time(), frame))
                        data = frame.get("data") or {}
                        if frame.get("type") == "job.state" and data.get("state") in (
                            "succeeded",
                            "failed",
                            "cancelled",
                        ):
                            self.terminal.add(str(data.get("job_id")))
        except Exception as exc:  # noqa: BLE001 - reported in the results
            self.error = f"{type(exc).__name__}: {exc}"
            self.ready.set()

    def stop(self) -> None:
        self._stop_evt.set()
        self.join(10)


def dump(
    pid: int, out_dir: Path, *, role: str, timeout_s: float = 120.0
) -> dict[str, Any]:
    """SIGUSR2 ``pid`` and return the hook's new JSON dump."""
    before = set(out_dir.glob(f"{role}-{pid}-*.json"))
    os.kill(pid, signal.SIGUSR2)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        new = sorted(set(out_dir.glob(f"{role}-{pid}-*.json")) - before)
        if new:
            return dict(json.loads(new[-1].read_text("utf-8")))
        time.sleep(0.2)
    raise AssertionError(f"no profile dump from {role} {pid} within {timeout_s} s")


class PySpy:
    """``py-spy record`` of some processes for one phase (``binary`` None =
    off): ``pyspy-<phase>[-<label>].txt``. Attaching needs the hook's
    ``MF_PROFILE_PTRACER=1`` under Yama ptrace scope 1."""

    def __init__(self, binary: str | None, out_dir: Path) -> None:
        self.binary = binary
        self.out_dir = out_dir
        self._procs: list[Any] = []
        self.files: dict[str, str] = {}

    def start(self, phase: str, pid: int, *, idle: bool = False, **more: int) -> None:
        """``idle``: also sample waiting threads (``--idle``: wall time, I/O)."""
        import subprocess

        if not self.binary:
            return
        for label, target in {"": pid, **more}.items():
            name = phase if not label else f"{phase}-{label}"
            path = self.out_dir / f"pyspy-{name}.txt"
            self._procs.append(
                subprocess.Popen(
                    [
                        self.binary,
                        "record",
                        "--pid",
                        str(target),
                        "-r",
                        "100",
                        "-f",
                        "raw",
                        "--nonblocking",
                        "-o",
                        str(path),
                        *(["--idle"] if idle else []),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.STDOUT,
                )
            )
            self.files[name] = str(path)
        time.sleep(1.0)

    def stop(self) -> None:
        for proc in self._procs:
            proc.send_signal(signal.SIGINT)
        for proc in self._procs:
            try:
                proc.wait(30)
            except Exception:  # noqa: BLE001 - never leave it behind
                proc.kill()
        self._procs = []


async def _pg(settings: Any, fn: Callable[[Any], Any]) -> Any:
    import asyncpg

    conn = await asyncpg.connect(
        host=settings.host,
        port=settings.port,
        user=settings.user,
        password=settings.password.get_secret_value(),
        database=settings.name,
        timeout=10,
    )
    try:
        return await fn(conn)
    finally:
        await conn.close()


def pg_run(settings: Any, fn: Callable[[Any], Any]) -> Any:
    return asyncio.run(_pg(settings, fn))


def pg_stat_reset(settings: Any) -> str | None:
    """Create the extension and reset it; the reason when unavailable."""

    async def go(conn: Any) -> None:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        await conn.execute("SELECT pg_stat_statements_reset()")

    try:
        pg_run(settings, go)
    except Exception as exc:  # noqa: BLE001 - reported, not fatal
        return f"{type(exc).__name__}: {exc}"
    return None


async def _generic_plan(conn: Any, query: str) -> list[str]:
    """``EXPLAIN (GENERIC_PLAN)`` of a normalised statement (``$1`` kept):
    asyncpg's extended protocol would want the parameters, so the plan is run
    by ``EXECUTE`` in a ``DO`` block (simple protocol) into a temp table."""
    tag = "$mfplan$"
    assert tag not in query
    await conn.execute("CREATE TEMP TABLE IF NOT EXISTS _mf_plan(n serial, line text)")
    await conn.execute("TRUNCATE _mf_plan")
    # the statement is pg_stat_statements' own text, dollar-quoted (tag checked)
    await conn.execute(
        "DO $mfdo$ DECLARE r record; BEGIN FOR r IN EXECUTE "  # noqa: S608
        f"'EXPLAIN (GENERIC_PLAN) ' || {tag}{query}{tag} LOOP "
        'INSERT INTO _mf_plan(line) VALUES (r."QUERY PLAN"); END LOOP; END $mfdo$'
    )
    rows = await conn.fetch("SELECT line FROM _mf_plan ORDER BY n")
    return [str(x[0]) for x in rows][:30]


def pg_top(settings: Any, limit: int = 15, explain: int = 10) -> list[dict[str, Any]]:
    """Top statements of this database by total time, each with its share and
    (for the first ``explain``) a generic plan (PG16 ``EXPLAIN (GENERIC_PLAN)``)."""

    async def go(conn: Any) -> list[dict[str, Any]]:
        rows = await conn.fetch(
            """
            SELECT s.query, s.calls, s.total_exec_time, s.mean_exec_time,
                   s.rows, s.shared_blks_hit, s.shared_blks_read
            FROM pg_stat_statements s
            JOIN pg_database d ON d.oid = s.dbid
            WHERE d.datname = current_database()
              AND s.query NOT ILIKE '%pg_stat_statements%'
              AND s.query NOT ILIKE 'EXPLAIN%'
            ORDER BY s.total_exec_time DESC
            """
        )
        total = sum(float(r["total_exec_time"]) for r in rows) or 1.0
        out: list[dict[str, Any]] = []
        for i, r in enumerate(rows[:limit]):
            item = {
                "query": " ".join(str(r["query"]).split())[:600],
                "calls": int(r["calls"]),
                "total_ms": round(float(r["total_exec_time"]), 2),
                "mean_ms": round(float(r["mean_exec_time"]), 4),
                "share_pct": round(100.0 * float(r["total_exec_time"]) / total, 2),
                "rows": int(r["rows"]),
            }
            if i < explain and str(r["query"]).lstrip().upper().startswith(
                ("SELECT", "WITH", "UPDATE", "DELETE", "INSERT")
            ):
                try:
                    item["plan"] = await _generic_plan(conn, str(r["query"]))
                except Exception as exc:  # noqa: BLE001 - some cannot be planned
                    item["plan_error"] = f"{type(exc).__name__}: {exc}"[:200]
            out.append(item)
        return [{"total_ms_all": round(total, 2), "statements": len(rows)}, *out]

    return list(pg_run(settings, go))


def pyspy_top(
    path: Path,
    match: str,
    *,
    top: int = 30,
    skip_idle: bool = True,
    spine_pct: float = 90.0,
) -> dict[str, Any]:
    """Self and inclusive sample shares per function of the stacks containing
    ``match`` (a ``py-spy record -f raw`` file: one ``frame;frame;... count``
    per line; ``--subprocesses`` adds a ``process N:"cmdline"`` frame).
    ``skip_idle``: samples parked in the selector (an idle event loop) are
    left out, so the shares are of busy time. Frames on at least
    ``spine_pct`` % of the samples (``main``, ``run_until_complete``, ...)
    say nothing and are left out of the inclusive list; ``meshforge_pct`` is
    the inclusive list of the product's own frames only."""
    from collections import Counter

    self_c: Counter[str] = Counter()
    incl: Counter[str] = Counter()
    total = 0
    for line in path.read_text("utf-8", errors="replace").splitlines():
        stack, _, count_s = line.rpartition(" ")
        if match not in stack or not count_s.isdigit():
            continue
        frames = [f for f in stack.split(";") if not f.startswith("process ")]
        if not frames:
            continue
        if skip_idle and any(
            k in frames[-1] for k in ("select (selectors.py", "poll (selectors.py")
        ):
            continue
        n = int(count_s)
        total += n
        self_c[frames[-1]] += n
        for f in set(frames):
            incl[f] += n

    def pct(v: int) -> float:
        return round(100.0 * v / max(1, total), 2)

    def rows(
        c: Counter[str], keep: Callable[[str, int], bool]
    ) -> list[tuple[str, float]]:
        return [(k, pct(v)) for k, v in c.most_common() if keep(k, v)][:top]

    def body(k: str, v: int) -> bool:
        return pct(v) < spine_pct

    return {
        "samples": total,
        "self_pct": rows(self_c, lambda k, v: True),
        "inclusive_pct": rows(incl, body),
        "meshforge_pct": rows(incl, lambda k, v: "meshforge/" in k and body(k, v)),
    }
