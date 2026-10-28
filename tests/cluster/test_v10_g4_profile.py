"""V1.0 G4 (roadmap 1.0.3, D412): the local profiling run.

Opt-in (``perf`` capability: ``pytest -m perf tests/cluster/test_v10_g4_profile.py``).
A real controller + 4 agents (shipped liveness and dispatch defaults), the
``mf_profile_hook`` on their ``PYTHONPATH`` (SIGUSR2 dumps, dispatch timings),
and these phases, in order:

1. **idle** - CPU% of the controller and the agents over ``IDLE_S`` with 4
   connected nodes and no jobs; CPU per heartbeat (D412: idle CPU < 5 %).
2. **overhead** - ``SEQ_JOBS`` trivial jobs one after the other: submit ->
   RUNNING, submit -> terminal, API round trip (submit->start p50 recorded).
3. **burst** - ``BURST`` jobs at once (sleep ``BURST_SLEEP_S``): 16 slots, so
   the queue starts at ~100: every ``decide`` (incl. waits), every dispatch
   pass, the stored ``decision_ms`` (D412: p95 < 200 ms at 4 nodes / 100
   queued).
4. **events** - all through 2-3: ``/v1/events/ws`` ``job.state`` frames vs the
   transition time in the database; ``node.metrics`` frames vs ``recv_at``.
5. **transfer** - a ``TRANSFER_MIB`` MiB input: upload, agent staging (the
   attempt's ``transfers``), a ``TRANSFER_MIB // 2`` MiB output collected and
   downloaded.
6. **memory** (``MESHFORGE_PROFILE_WAVES`` > 0) - waves of ``WAVE_JOBS`` jobs
   with a hook dump after each; ``MESHFORGE_PROFILE_IDLE_MIN`` minutes idle
   with dumps (``MESHFORGE_PROFILE_IDLE_DUMP_EVERY`` minutes; 0 = only the
   sampler) and the idle RSS slope; ``MESHFORGE_PROFILE_TRACEMALLOC=1`` traces
   the controller; ``MESHFORGE_PROFILE_EVENTS_RING`` shrinks the event bus
   ring so it fills within the run; ``MESHFORGE_PROFILE_CALIBRATION_REFRESH_S``
   makes the calibration refit (a one-off RSS step at 30 min) repeat. Each
   dump carries the fill level of the controller's bounded stores
   (``caches``).
7. **db** - ``pg_stat_statements`` top statements with generic plans (needs a
   server with the module preloaded, e.g. ``MESHFORGE_TEST_PG=external`` on a
   ``postgres -c shared_preload_libraries=pg_stat_statements`` container;
   otherwise the reason is recorded).

Results: ``MESHFORGE_PROFILE_OUT`` (a folder; default the test's tmp dir) gets
``results.json`` + the hook dumps. Asserts only that every phase produced
numbers - the D412 verdicts are written into the results and the report, not
gated here (a busy developer machine is not the lab).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import SHIPPED, Fabric
from mf_profile import (
    HOOK_DIR,
    CpuWindow,
    EventTap,
    PySpy,
    Sampler,
    dump,
    pg_stat_reset,
    pg_top,
    pyspy_top,
    rss_slope,
    summary,
)
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.perf, pytest.mark.timeout(4 * 3600)]

if not sys.platform.startswith("linux"):
    pytest.skip(
        "the profiling run is Linux-only (SIGUSR2, /proc)", allow_module_level=True
    )


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


AGENTS: Final = 4
IDLE_S: Final = _env_int("MESHFORGE_PROFILE_IDLE_S", 60)
SEQ_JOBS: Final = _env_int("MESHFORGE_PROFILE_SEQ_JOBS", 20)
BURST: Final = _env_int("MESHFORGE_PROFILE_BURST", 100)
BURST_SLEEP_S: Final = 2.0
TRANSFER_MIB: Final = _env_int("MESHFORGE_PROFILE_TRANSFER_MIB", 128)
WAVES: Final = _env_int("MESHFORGE_PROFILE_WAVES", 0)
WAVE_JOBS: Final = _env_int("MESHFORGE_PROFILE_WAVE_JOBS", 60)
IDLE_MIN: Final = _env_int("MESHFORGE_PROFILE_IDLE_MIN", 0)
HISTORY: Final = _env_int("MESHFORGE_PROFILE_HISTORY", 0)
"""Jobs run (and kept) BEFORE the measured phases: the database of a long run."""
TRACEMALLOC: Final = os.environ.get("MESHFORGE_PROFILE_TRACEMALLOC") == "1"
IDLE_DUMP_EVERY: Final = _env_int("MESHFORGE_PROFILE_IDLE_DUMP_EVERY", 1)
"""A hook dump every N idle minutes (and the last; 0 = none): a dump walks
every object and allocates megabytes, which a pure-idle RSS slope must not
include."""
CALIBRATION_REFRESH_S: Final = _env_int("MESHFORGE_PROFILE_CALIBRATION_REFRESH_S", 0)
"""``controller.planner.calibration_refresh_s`` override (0 = shipped 1800 s):
a refit every minute shows whether its memory step repeats or plateaus."""
EVENTS_RING: Final = _env_int("MESHFORGE_PROFILE_EVENTS_RING", 0)
"""``controller.events.ring_size`` override (0 = shipped 10 000): a small ring
fills in minutes, so the memory phase can show the bus ring's plateau."""
PYSPY: Final = os.environ.get("MESHFORGE_PROFILE_PYSPY") or None
"""A py-spy binary: the controller is recorded per phase (``pyspy-<phase>.txt``)."""
EXEC: Final = {
    "enabled": True,
    "allow_unsandboxed": True,
    "grace_s": 1.0,
    "max_cpu_jobs": 4,
    "max_concurrent_attempts": 4,
}
TERMINAL: Final = {"succeeded", "failed", "cancelled"}
POLL_S: Final = 10.0

MAIN: Final = """\
import os, sys, time
sleep_s, out_mib = float(sys.argv[1]), int(sys.argv[2])
if os.path.exists("data.bin"):
    with open("data.bin", "rb") as fh:
        while fh.read(1 << 20):
            pass
time.sleep(sleep_s)
os.makedirs("out", exist_ok=True)
with open("out/result.bin", "wb") as fh:
    if out_mib:
        for _ in range(out_mib):
            fh.write(os.urandom(1 << 20))
    else:
        fh.write(b"ok")
"""


class Api:
    def __init__(self, fabric: Fabric, tap: EventTap | None = None) -> None:
        self.fabric = fabric
        self.tap = tap

    def client(self, timeout_s: float = 600) -> ControllerClient:
        return ControllerClient(
            self.fabric.http_base,
            timeout_s=timeout_s,
            token=SecretStr(self.fabric.api_token()),
            ssl_context=self.fabric.ssl_context,
        )

    def tree(self, root: Path, data_mib: int = 0) -> tuple[BuiltTree, float]:
        root.mkdir(parents=True)
        (root / "main.py").write_text(MAIN, "utf-8")
        if data_mib:
            with (root / "data.bin").open("wb") as fh:
                for _ in range(data_mib):
                    fh.write(os.urandom(1 << 20))
        built = build_tree(root)  # the product default (8 MiB chunks), as `compute run`
        client = self.client()
        started = time.perf_counter()
        try:
            upload_tree(client, built)
        finally:
            client.close()
        return built, time.perf_counter() - started

    def submit(
        self, tree: BuiltTree, name: str, sleep_s: float, out_mib: int = 0,
        node_id: str | None = None,
    ) -> tuple[str, float]:  # fmt: skip
        body: dict[str, Any] = {
            "manifest": {
                "name": name,
                "entrypoint": ["python", "main.py", str(sleep_s), str(out_mib)],
                "outputs": ["out/*"],
            },
            "tree_id": str(tree.tree.tree_id),
        }
        if node_id is not None:
            body["placement"] = {"node_id": node_id}
        started = time.perf_counter()
        resp = self.fabric.call("POST", "/v1/jobs", json=body, timeout=30.0)
        took = (time.perf_counter() - started) * 1000.0
        assert resp.status_code == 201, resp.text
        return str(resp.json()["job_id"]), took

    def job(self, job_id: str) -> dict[str, Any]:
        resp = self.fabric.call("GET", f"/v1/jobs/{job_id}", timeout=30.0)
        assert resp.status_code == 200, resp.text
        return dict(resp.json())

    def wait(self, ids: list[str], within_s: float) -> dict[str, dict[str, Any]]:
        """Until every job is terminal: watched on the event stream (the tap),
        so the measurement does not load the controller with polling; a REST
        check every ``POLL_S`` is the fallback (a missed frame)."""
        deadline = time.monotonic() + within_s
        done: dict[str, dict[str, Any]] = {}
        next_poll = time.monotonic() + POLL_S
        while time.monotonic() < deadline:
            if (
                self.tap is not None
                and time.monotonic() < next_poll
                and not set(ids) <= self.tap.terminal
            ):
                time.sleep(0.05)
                continue
            next_poll = time.monotonic() + POLL_S
            for job_id in [i for i in ids if i not in done]:
                job = self.job(job_id)
                if job["state"] in TERMINAL:
                    done[job_id] = job
            if len(done) == len(ids):
                return done
            time.sleep(0.25)
        raise AssertionError(
            f"{len(ids) - len(done)} jobs not terminal in {within_s} s"
        )


def _ts(value: str) -> float:
    return dt.datetime.fromisoformat(value).timestamp()


def _arrow(job: dict[str, Any], state: str) -> float | None:
    for t in job["transitions"]:
        if t["to_state"] == state:
            return _ts(t["at"])
    return None


def _job_times(job: dict[str, Any]) -> dict[str, float | None]:
    created = _ts(job["created_at"])
    running = _arrow(job, "running")
    end = next(
        (_ts(t["at"]) for t in job["transitions"] if t["to_state"] in TERMINAL), None
    )
    queued = _arrow(job, "queued")
    preparing = _arrow(job, "preparing")
    ms = lambda a, b: None if a is None or b is None else (b - a) * 1000.0  # noqa: E731
    return {
        "submit_to_running_ms": ms(created, running),
        "submit_to_terminal_ms": ms(created, end),
        "queued_to_preparing_ms": ms(queued, preparing),
        "preparing_to_running_ms": ms(preparing, running),
    }


def _decisions(pg_settings: Any) -> list[float]:
    from mf_profile import pg_run

    async def go(conn: Any) -> list[float]:
        rows = await conn.fetch(
            "SELECT decision_ms FROM plan_decision WHERE decision_ms IS NOT NULL"
        )
        return [float(r[0]) for r in rows]

    return list(pg_run(pg_settings, go))


def _event_latency(tap: EventTap, jobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    arrows: dict[tuple[str, str], float] = {}
    for job_id, job in jobs.items():
        for t in job["transitions"]:
            arrows.setdefault((job_id, t["to_state"]), _ts(t["at"]))
    from_db: list[float] = []
    from_publish: list[float] = []
    metrics: list[float] = []
    for recv, frame in tap.frames:
        data = frame.get("data") or {}
        if frame.get("type") == "job.state":
            at = arrows.get((str(data.get("job_id")), str(data.get("state"))))
            if at is not None:
                from_db.append((recv - at) * 1000.0)
            if isinstance(frame.get("at"), (int, float)):
                from_publish.append((recv - float(frame["at"])) * 1000.0)
        elif frame.get("type") == "node.metrics" and data.get("recv_at"):
            metrics.append((recv - float(data["recv_at"])) * 1000.0)
    return {
        "frames": len(tap.frames),
        "error": tap.error,
        "job_state_from_transition_ms": summary(from_db),
        "job_state_from_publish_ms": summary(from_publish),
        "node_metrics_from_arrival_ms": summary(metrics),
    }


def _verdict(ok: bool) -> str:
    """A D412 verdict - none with tracemalloc on: tracing every allocation
    (and its multi-second snapshots on the loop) makes the controller many times
    slower (measured: decision p95 1.5 -> 26 ms, idle CPU 3 -> 17 %)."""
    if TRACEMALLOC:
        return "N/A (tracemalloc on: timings not representative)"
    return "PASS" if ok else "FAIL"


def _controller_extra() -> dict[str, dict[str, Any]]:
    extra: dict[str, dict[str, Any]] = {}
    if EVENTS_RING:
        extra["events"] = {"ring_size": EVENTS_RING}
    if CALIBRATION_REFRESH_S:
        extra["planner"] = {"calibration_refresh_s": float(CALIBRATION_REFRESH_S)}
    return extra


def _diff_top(
    a: dict[str, int], b: dict[str, int], n: int = 25
) -> list[tuple[str, int]]:
    keys = set(a) | set(b)
    deltas = [(k, int(b.get(k, 0)) - int(a.get(k, 0))) for k in keys]
    return sorted(deltas, key=lambda kv: -abs(kv[1]))[:n]


def test_profile_local_fabric(
    make_fabric: Callable[..., Fabric],
    pg_settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = Path(os.environ.get("MESHFORGE_PROFILE_OUT") or tmp_path / "profile")
    dumps = out / "dumps"
    dumps.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MF_PROFILE_DIR", str(dumps))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(filter(None, [str(HOOK_DIR), os.environ.get("PYTHONPATH")])),
    )
    if TRACEMALLOC:
        monkeypatch.setenv("MF_PROFILE_TRACEMALLOC", "controller")
    if PYSPY:
        monkeypatch.setenv("MF_PROFILE_PTRACER", "1")
    spy = PySpy(PYSPY, out)
    results: dict[str, Any] = {
        "started": dt.datetime.now(dt.UTC).isoformat(),
        "config": {
            "agents": AGENTS, "idle_s": IDLE_S, "seq_jobs": SEQ_JOBS, "burst": BURST,
            "transfer_mib": TRANSFER_MIB, "waves": WAVES, "wave_jobs": WAVE_JOBS,
            "idle_min": IDLE_MIN, "tracemalloc": TRACEMALLOC, "exec": EXEC,
            "history": HISTORY, "events_ring": EVENTS_RING or None,
            "calibration_refresh_s": CALIBRATION_REFRESH_S or None,
            "idle_dump_every": IDLE_DUMP_EVERY,
            "cpus": os.cpu_count(),
        },
    }  # fmt: skip
    pg_reason = pg_stat_reset(pg_settings)

    def save() -> None:
        (out / "results.json").write_text(json.dumps(results, indent=1), "utf-8")

    fabric = make_fabric(
        agents=AGENTS,
        timing=SHIPPED,
        agent_execution=EXEC,
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        controller_extra=_controller_extra(),
    )
    assert fabric.controller is not None
    ctl = fabric.controller

    def snap() -> dict[str, Any]:
        """A controller dump. It runs on the event loop's thread: with
        tracemalloc on it blocks the loop for seconds (heartbeats wait, a node
        may read OFFLINE), so the fabric is waited back to all-online."""
        found = dump(ctl.pid, dumps, role="controller")
        fabric.wait_all_connected(within=120)
        return found

    def pids() -> dict[str, int]:
        found = {"controller": ctl.pid} if ctl.running else {}
        found.update(
            {f"agent{n.index}": n.pid for n in fabric.nodes if n.process.running}
        )
        return found

    sampler = Sampler(pids, 2.0)
    sampler.start()
    tap = EventTap(fabric)
    tap.start()
    assert tap.ready.wait(20)
    api = Api(fabric, tap)
    all_jobs: dict[str, dict[str, Any]] = {}
    try:
        # ------------------------------------------------------- history
        if HISTORY:
            sampler.mark("history")
            seed_tree, _ = api.tree(tmp_path / "history")
            t0 = time.perf_counter()
            for start in range(0, HISTORY, 200):
                batch = [
                    api.submit(seed_tree, f"history-{i}", 0.0)[0]
                    for i in range(start, min(HISTORY, start + 200))
                ]
                api.wait(batch, 1800)
            results["history"] = {
                "jobs": HISTORY, "s": round(time.perf_counter() - t0, 1),
                "jobs_per_s": round(HISTORY / (time.perf_counter() - t0), 2),
            }  # fmt: skip
            save()
        # ---------------------------------------------------------- idle
        time.sleep(15)
        dumps_first = snap()
        sampler.mark("idle")
        window = CpuWindow(pids()).start()
        time.sleep(IDLE_S)
        idle = window.stop()
        spy.start("idle", ctl.pid)
        time.sleep(IDLE_S)
        spy.stop()
        hb_per_s = AGENTS / SHIPPED.heartbeat_s
        ctl_cpu_ms_s = (
            1000.0 * idle["processes"]["controller"]["cpu_s"] / idle["wall_s"]
        )
        idle["controller_cpu_ms_per_heartbeat_upper_bound"] = round(
            ctl_cpu_ms_s / hb_per_s, 3
        )
        idle["controller_rss_mib"] = round(dumps_first["rss_bytes"] / 2**20, 1)
        idle["verdict_idle_cpu_lt_5pct"] = _verdict(
            idle["processes"]["controller"]["cpu_pct"] < 5.0
        )
        results["idle"] = idle
        save()

        # ------------------------------------------------------ overhead
        sampler.mark("overhead")
        small, _ = api.tree(tmp_path / "small")
        api_ms: list[float] = []
        seq: dict[str, dict[str, Any]] = {}
        for i in range(SEQ_JOBS):
            job_id, took = api.submit(small, f"seq-{i}", 0.0)
            api_ms.append(took)
            seq.update(api.wait([job_id], 120))
        all_jobs.update(seq)
        times = [_job_times(j) for j in seq.values()]
        results["overhead"] = {
            "succeeded": sum(1 for j in seq.values() if j["state"] == "succeeded"),
            "submit_api_ms": summary(api_ms),
            **{k: summary([t[k] for t in times if t[k] is not None]) for k in times[0]},
        }
        save()

        # --------------------------------------------------------- burst
        sampler.mark("burst")
        before = snap()
        seen = {k: len(v) for k, v in before["timings"].items()}
        db_before = len(_decisions(pg_settings))
        spy.start("burst", ctl.pid)
        window = CpuWindow({"controller": ctl.pid}).start()
        t0 = time.perf_counter()
        ids = [api.submit(small, f"burst-{i}", BURST_SLEEP_S)[0] for i in range(BURST)]
        submit_s = time.perf_counter() - t0
        burst = api.wait(ids, 900)
        cpu = window.stop()
        spy.stop()
        after = snap()
        all_jobs.update(burst)
        decided = _decisions(pg_settings)[db_before:]
        btimes = [_job_times(j) for j in burst.values()]
        ends = [
            _ts(t["at"]) for j in burst.values() for t in j["transitions"]
            if t["to_state"] in TERMINAL
        ]  # fmt: skip
        makespan = max(ends) - min(_ts(j["created_at"]) for j in burst.values())
        results["burst"] = {
            "jobs": BURST,
            "succeeded": sum(1 for j in burst.values() if j["state"] == "succeeded"),
            "submit_all_s": round(submit_s, 2),
            "makespan_s": round(makespan, 2),
            "ideal_makespan_s": -(-BURST // (AGENTS * 4)) * BURST_SLEEP_S,
            "controller_cpu": cpu["processes"]["controller"],
            "decision_ms_stored": summary(decided),
            # the hook's timings over the burst: decide_ms = every call (incl.
            # waits); dispatch_pass_ms = DispatchSteps._dispatch; its parts
            "hook": {
                k: {**summary(v[seen.get(k, 0):]), "sum_ms": round(sum(v[seen.get(k, 0):]), 1)}
                for k, v in sorted(after["timings"].items())
            },
            "submit_to_running_ms": summary(
                [t["submit_to_running_ms"] for t in btimes if t["submit_to_running_ms"]]
            ),
        }  # fmt: skip
        stored = results["burst"]["decision_ms_stored"]
        results["burst"]["verdict_decision_p95_lt_200ms"] = _verdict(
            stored.get("p95") is not None and stored["p95"] < 200
        )
        save()

        # ------------------------------------------------------ transfer
        sampler.mark("transfer")
        node = fabric.nodes[0]
        spy.start("upload", ctl.pid, idle=True)
        big, up_s = api.tree(tmp_path / "big", data_mib=TRANSFER_MIB)
        spy.stop()
        spy.start("staging", ctl.pid, idle=True, agent=node.pid)
        job_id, _ = api.submit(big, "transfer", 0.0, TRANSFER_MIB // 2, node.node_id)
        tjob = api.wait([job_id], 900)[job_id]
        spy.stop()
        all_jobs[job_id] = tjob
        attempt = tjob["attempts"][-1]
        staged = [
            x
            for x in attempt.get("transfers", [])
            if x["tree_id"] == str(big.tree.tree_id)
        ]
        ctl_bytes = sum(x["bytes"] for x in staged if x["source"] == "controller")
        ctl_ms = max((x["duration_ms"] for x in staged), default=0)
        client = api.client()
        t0 = time.perf_counter()
        digest = str(tjob.get("output_digest") or attempt.get("output_digest"))
        resp = fabric.call(
            "GET", f"/v1/blobs/{digest.removeprefix('sha256:')}", timeout=600.0
        )
        down_s = time.perf_counter() - t0
        client.close()
        collecting = _arrow(tjob, "collecting")
        end = _arrow(tjob, "succeeded")
        mib = 2**20
        results["transfer"] = {
            "state": tjob["state"],
            "upload_mib_s": round(TRANSFER_MIB / up_s, 1),
            "upload_s": round(up_s, 2),
            "staging": staged,
            "staging_mib_s": round(ctl_bytes / mib / (ctl_ms / 1000.0), 1) if ctl_ms else None,
            "collect_s": None if collecting is None or end is None else round(end - collecting, 2),
            "download_mib": round(len(resp.content) / mib, 1),
            "download_mib_s": round(len(resp.content) / mib / down_s, 1),
            "times": _job_times(tjob),
        }  # fmt: skip
        save()

        # -------------------------------------------------------- memory
        mem: list[dict[str, Any]] = []
        mem_dumps = [snap()]
        mem.append(
            {
                "what": "after-transfer",
                "rss_mib": round(mem_dumps[0]["rss_bytes"] / mib, 1),
            }
        )
        for w in range(WAVES):
            sampler.mark(f"wave{w}")
            wave = [api.submit(small, f"wave{w}-{i}", 0.2)[0] for i in range(WAVE_JOBS)]
            all_jobs.update(api.wait(wave, 900))
            time.sleep(10)
            d = snap()
            mem_dumps.append(d)
            mem.append({"what": f"wave{w}", "rss_mib": round(d["rss_bytes"] / mib, 1),
                        "gc_objects": d["gc_objects"], "tasks": sum(d["tasks"].values()),
                        "traced_mib": round(d.get("tracemalloc_traced_bytes", 0) / mib, 2),
                        "caches": d.get("caches")})  # fmt: skip
            save()
        for m in range(IDLE_MIN):
            sampler.mark(f"idle{m}")
            time.sleep(60)
            last_minute = m == IDLE_MIN - 1
            if IDLE_DUMP_EVERY <= 0 or ((m + 1) % IDLE_DUMP_EVERY and not last_minute):
                continue  # the sampler's RSS only: a dump is work of its own
            d = snap()
            mem_dumps.append(d)
            mem.append({"what": f"idle-min{m + 1}", "rss_mib": round(d["rss_bytes"] / mib, 1),
                        "gc_objects": d["gc_objects"], "tasks": sum(d["tasks"].values()),
                        "traced_mib": round(d.get("tracemalloc_traced_bytes", 0) / mib, 2),
                        "caches": d.get("caches")})  # fmt: skip
            save()
        if len(mem_dumps) >= 3:
            first, last = mem_dumps[1], mem_dumps[-1]
            results["memory_diff"] = {
                "gc_top": _diff_top(first["gc_top"], last["gc_top"]),
                "tasks": _diff_top(first["tasks"], last["tasks"]),
                "threads": [first["threads"], last["threads"]],
            }
        results["memory"] = mem
        results["controller_first_dump"] = {
            k: dumps_first[k] for k in ("rss_bytes", "threads", "gc_objects", "tasks")
        }
        results["events"] = _event_latency(tap, all_jobs)
        results["events"]["verdict_p95_lt_500ms"] = _verdict(
            (results["events"]["job_state_from_transition_ms"].get("p95") or 1e9) < 500
        )
        save()
    finally:
        spy.stop()
        tap.stop()
        sampler.stop()
        results["samples"] = {
            label: sampler.series(label, "rss")[:: max(1, len(sampler.rows) // 2000)]
            for label in ("controller", "agent0")
        }
        results["threads"] = {
            label: sampler.series(label, "threads")[-1:]
            for label in ("controller", "agent0")
        }
        results["marks"] = sampler.marks
        results["pyspy"] = spy.files
        results["pyspy_top"] = {
            name: pyspy_top(
                Path(path),
                "meshforge/agent"
                if name.endswith("-agent")
                else "meshforge/controller",
                top=20,
                skip_idle=not name.startswith(("upload", "staging")),
            )
            for name, path in spy.files.items()
            if Path(path).is_file() and Path(path).stat().st_size
        }
        idle_marks = [
            t for t, what in sampler.marks if what.startswith("idle") and what != "idle"
        ]
        if idle_marks:
            # RSS slope over the idle tail (after the first idle minute): a
            # bounded store still filling reads as a positive slope here.
            results["memory_idle_slope_mib_per_h"] = {
                label: rss_slope(sampler.series(label, "rss"), idle_marks[0] + 60.0)
                for label in ("controller", "agent0")
            }
        results["db"] = {"unavailable": pg_reason} if pg_reason else pg_top(pg_settings)
        results["finished"] = dt.datetime.now(dt.UTC).isoformat()
        save()
    assert results["overhead"]["succeeded"] == SEQ_JOBS
    assert results["burst"]["succeeded"] == BURST
    assert results["transfer"]["state"] == "succeeded"
