"""V0.6 chaos suite (roadmap 0.6.12, D118): seeded random faults over N jobs.

Tier ``chaos`` (``tests/cluster/chaos/``; opt-in: ``--run-chaos``). Linux only:
the disk-full fault needs a namespaced tmpfs and the ledger's overlap check a
``flock``.

**Fabric.** A real controller and four agent processes (subprocess runtime,
three CPU slots each); agent 3's workdir is a 8 MiB tmpfs in its own mount
namespace (the disk-full target). Every job is an automatically placed
``mf_ledger`` workload (sleep 1-6 s; ~10 % exit 3 on purpose) with ``retry:
{max_attempts: 5, backoff_s: 0|1}`` and a policy drawn from four.

**Faults** (drawn from ``random.Random(seed)``, one every 1-3 s for
``CHAOS_FAULT_S``, while the jobs arrive in five waves): agent ``SIGKILL``
(restarted 0.5-3 s later), agent ``SIGSTOP`` for longer than the lease
(partition-like: its workloads keep running), the agent's link to the
controller blackholed for longer than the lease (a Wi-Fi drop), controller
``SIGKILL`` + restart (at most twice), the disk-full agent's tmpfs filled for
3-6 s (once: ``disk_full`` is not retryable, so a full node fails every job it
is given meanwhile - reported, see the V0.6 G3 handoff). At most two agents
are faulted at once. Then every fault is undone.

**Invariants after quiescence** (every job terminal or the test fails):

1. every job is ``succeeded`` or ``failed``;
2. a ``succeeded`` job has exactly one ``done`` attempt - its last one - and
   its side effect names that attempt (**exactly-once commit**);
3. no job ``failed`` with a retryable type while attempts remained;
4. no attempt row is left live;
5. a job meant to succeed never ends ``user_error``;
6. ledger ``overlap`` lines (two attempts of a job running at once) only for
   jobs with an attempt ``lost`` on a node that was partitioned (stopped or
   cut off) - never from a kill;
7. a run that completed but was not committed (an extra side effect) belongs
   to a ``lost``/``failed`` attempt on a node that had a fault.

The seed is printed (and put in every failure message): replay one with
``MESHFORGE_CHAOS_SEEDS=<seed> pytest tests/cluster/chaos --run-chaos``.
``MESHFORGE_CHAOS_JOBS`` sets N (default 50). A JSON summary per seed is
written to ``$MESHFORGE_CHAOS_REPORT`` (a directory) when set.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import os
import random
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import START_TIMEOUT_S, Fabric, Node, Timing, tmpfs_unsupported_reason
from mf_ledger import (
    Ledger,
    job_view,
    ledger_manifest,
    make_ledger,
    submit,
    upload_workload,
)

from meshforge.common.errors import FailureType, is_retryable

pytestmark = [pytest.mark.chaos, pytest.mark.timeout(900)]

if not sys.platform.startswith("linux"):
    pytest.skip(
        "the chaos suite is Linux-only (tmpfs namespace, flock)",
        allow_module_level=True,
    )

TIMING = Timing(
    name="chaos",
    # SUSPECT at 0.8 s, OFFLINE at 2.0 s after the last heartbeat
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.3,
    lease_multiplier=4,
    controller_hello_timeout_s=3.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=1.0,
    agent_hello_timeout_s=3.0,
)
LEASE_S = 2.0
EXEC = {
    "enabled": True,
    "allow_unsandboxed": True,
    "grace_s": 1.0,
    "max_cpu_jobs": 3,
    "max_concurrent_attempts": 3,
    "terminal_resend_s": 1.0,
}
JOBS_CFG = {"dispatch_interval_s": 0.1, "reconnect_grace_s": 8.0}
AGENTS = 4
DISK_AGENT = 3
MAX_ATTEMPTS = 5
CHAOS_FAULT_S = 25.0
QUIESCE_S = 300.0
POLICIES = ("cost_model", "least_loaded", "random", "round_robin")
TERMINAL = {"succeeded", "failed", "cancelled"}
LIVE_ATTEMPT = {"assigned", "staging", "env_ready", "running", "uploading"}


def _seeds() -> list[int]:
    raw = os.environ.get("MESHFORGE_CHAOS_SEEDS", "").strip()
    if raw:
        return [int(s) for s in raw.split(",") if s.strip()]
    return [int(time.time()) % 100_000]


def _jobs_n() -> int:
    return int(os.environ.get("MESHFORGE_CHAOS_JOBS", "50"))


@dataclasses.dataclass
class Plan:
    """One job's seeded shape."""

    sleep_s: float
    exit_code: int
    backoff_s: int
    policy: str


@dataclasses.dataclass
class Chaos:
    """The fault driver: what is broken now, what was ever broken."""

    fabric: Fabric
    rng: random.Random
    disk_ok: bool
    log: list[tuple[float, str, str]] = dataclasses.field(default_factory=list)
    restore_at: dict[str, tuple[float, Callable[[], None]]] = dataclasses.field(
        default_factory=dict
    )
    partitioned: set[str] = dataclasses.field(default_factory=set)
    """node_ids ever stopped or cut off (overlaps are allowed only there)."""
    faulted: set[str] = dataclasses.field(default_factory=set)
    """node_ids that had any fault."""
    controller_kills: int = 0
    counts: collections.Counter[str] = dataclasses.field(
        default_factory=collections.Counter
    )

    def busy(self, node: Node) -> bool:
        return node.name in self.restore_at

    def _note(self, kind: str, target: str) -> None:
        self.counts[kind] += 1
        self.log.append((round(time.monotonic(), 2), kind, target))

    def inject(self) -> None:
        free = [n for n in self.fabric.nodes if not self.busy(n)]
        kinds = ["agent_kill", "agent_stop", "link_cut"]
        if (
            self.disk_ok
            and not self.counts["disk_full"]
            and not self.busy(self.fabric.nodes[DISK_AGENT])
        ):
            kinds.append("disk_full")  # once: a full node fails what it gets
        if self.controller_kills < 2:
            kinds.append("controller_kill")
        if len(self.fabric.nodes) - len(free) >= 2:  # keep capacity: no new node fault
            kinds = [k for k in kinds if k in ("controller_kill",)]
        if not kinds:
            return
        kind = self.rng.choice(kinds)
        now = time.monotonic()
        if kind == "controller_kill":
            self.controller_kills += 1
            self._note(kind, "controller")
            self.fabric.kill_controller()
            time.sleep(self.rng.uniform(0.5, 2.0))
            self.fabric.restart_controller()
            return
        if kind == "disk_full":
            node = self.fabric.nodes[DISK_AGENT]
        else:
            node = self.rng.choice([n for n in free if n.index != DISK_AGENT] or free)
        self.faulted.add(node.node_id)
        self._note(kind, node.name)
        if kind == "agent_kill":
            node.kill()
            back = now + self.rng.uniform(0.5, 3.0)
            self.restore_at[node.name] = (
                back,
                lambda n=node: n.restart(graceful=False),
            )
        elif kind == "agent_stop":
            self.partitioned.add(node.node_id)
            node.pause()
            back = now + LEASE_S + self.rng.uniform(0.5, 2.0)
            self.restore_at[node.name] = (back, node.resume)
        elif kind == "link_cut":
            self.partitioned.add(node.node_id)
            node.link.blackhole(True)
            back = now + LEASE_S + self.rng.uniform(0.5, 2.0)
            self.restore_at[node.name] = (back, lambda n=node: n.link.blackhole(False))
        else:
            node.fill_disk()
            back = now + self.rng.uniform(3.0, 6.0)
            self.restore_at[node.name] = (back, node.free_disk)

    def restore_due(self, *, all_now: bool = False) -> None:
        now = time.monotonic()
        for name, (at, undo) in list(self.restore_at.items()):
            if all_now or now >= at:
                undo()
                del self.restore_at[name]


def _plans(rng: random.Random, n: int) -> list[Plan]:
    return [
        Plan(
            sleep_s=rng.uniform(1.0, 6.0),
            exit_code=3 if rng.random() < 0.1 else 0,
            backoff_s=rng.choice((0, 1)),
            policy=rng.choice(POLICIES),
        )
        for _ in range(n)
    ]


def _views(fabric: Fabric, job_ids: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for job_id in job_ids:
        view = job_view(fabric, job_id)
        if view is not None:
            out[job_id] = view
    return out


async def _live_attempts(pg_settings: Any) -> int:
    from sqlalchemy import text

    from meshforge.controller.store.engine import create_engine

    engine = create_engine(pg_settings)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT count(*) FROM attempt WHERE state IN "
                    "('assigned','staging','env_ready','running','uploading')"
                )
            )
            return int(result.scalar_one())
    finally:
        await engine.dispose()


@pytest.mark.parametrize("seed", _seeds())
def test_chaos(
    seed: int,
    make_fabric: Callable[..., Fabric],
    tmp_path: Path,
    pg_settings: Any,
    record_property: Callable[[str, object], None],
) -> None:
    print(f"\nchaos seed={seed}", flush=True)  # noqa: T201 - the replay handle
    record_property("chaos_seed", seed)
    try:
        _run(seed, make_fabric, tmp_path, pg_settings, record_property)
    except AssertionError as exc:
        raise AssertionError(f"[chaos seed={seed}] {exc}") from exc


def _run(
    seed: int,
    make_fabric: Callable[..., Fabric],
    tmp_path: Path,
    pg_settings: Any,
    record_property: Callable[[str, object], None],
) -> None:
    import asyncio

    rng = random.Random(seed)  # noqa: S311 - seeded chaos, not crypto
    disk_ok = tmpfs_unsupported_reason() is None
    fabric = make_fabric(
        agents=AGENTS,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs=JOBS_CFG,
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        agent_tmpfs_workdir={DISK_AGENT: 8 * 1024 * 1024} if disk_ok else None,
    )
    ledger = Ledger(make_ledger(tmp_path / "ledger"))
    digest = upload_workload(fabric, tmp_path)
    plans = _plans(rng, _jobs_n())
    chaos = Chaos(fabric, rng, disk_ok)
    jobs: list[str] = []
    plan_of: dict[str, Plan] = {}
    waves = [plans[i::5] for i in range(5)]
    started = time.monotonic()
    next_fault = started + rng.uniform(1.0, 3.0)
    next_wave = started
    while time.monotonic() - started < CHAOS_FAULT_S or waves:
        if waves and time.monotonic() >= next_wave:
            for plan in waves.pop(0):
                key = f"chaos-{seed}-{len(jobs)}"
                job_id = _submit_retrying(fabric, digest, ledger, plan, key)
                jobs.append(job_id)
                plan_of[job_id] = plan
            next_wave = time.monotonic() + CHAOS_FAULT_S / 6
        chaos.restore_due()
        if (
            time.monotonic() >= next_fault
            and time.monotonic() - started < CHAOS_FAULT_S
        ):
            chaos.inject()
            next_fault = time.monotonic() + rng.uniform(1.0, 3.0)
        time.sleep(0.05)
    chaos.restore_due(all_now=True)
    fabric.wait_all_connected(within=START_TIMEOUT_S)

    deadline = time.monotonic() + QUIESCE_S
    views: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        views = _views(fabric, jobs)
        if len(views) == len(jobs) and all(
            v["state"] in TERMINAL for v in views.values()
        ):
            break
        time.sleep(0.25)
    stuck = {
        j: views.get(j, {}).get("state")
        for j in jobs
        if views.get(j, {}).get("state") not in TERMINAL
    }
    assert not stuck, (
        f"invariant 1: jobs not terminal after {QUIESCE_S} s: {stuck}; faults {chaos.log}"
    )
    time.sleep(1.0)  # late reports of released attempts land
    views = _views(fabric, jobs)

    problems: list[str] = []
    by_job = ledger.by_job()
    extra_runs = 0
    overlaps = 0
    retries = 0
    states: collections.Counter[str] = collections.Counter()
    for job_id in jobs:
        view, plan = views[job_id], plan_of[job_id]
        attempts = view["attempts"]
        retries += max(0, len(attempts) - 1)
        state, ftype = view["state"], view["failure_type"]
        states[state if state != "failed" else f"failed:{ftype}"] += 1
        done = [a for a in attempts if a["state"] == "done"]
        side = ledger.side_effects(job_id)
        if state == "succeeded":
            if len(done) != 1 or done[0] is not attempts[-1]:
                problems.append(f"2: {job_id} succeeded with done attempts {done}")
            elif side.count(done[0]["attempt_id"]) != 1:
                problems.append(f"2: {job_id} committed attempt not once in {side}")
        elif state == "failed":
            if done:
                problems.append(f"2: {job_id} failed with a done attempt")
            if (
                ftype is not None
                and is_retryable(FailureType(ftype))
                and len(attempts) < MAX_ATTEMPTS
            ):
                problems.append(
                    f"3: {job_id} failed {ftype} after {len(attempts)} attempts"
                )
            if ftype == "user_error" and plan.exit_code == 0:
                problems.append(f"5: {job_id} meant to succeed ended user_error")
        else:
            problems.append(f"1: {job_id} ended {state}")
        lost_partitioned = any(
            a["state"] == "lost" and a["node_id"] in chaos.partitioned for a in attempts
        )
        for line in by_job.get(job_id, []):
            if line.kind == "overlap":
                overlaps += 1
                if not lost_partitioned:
                    problems.append(
                        f"6: {job_id} overlap without a partitioned lost attempt"
                    )
        committed = {a["attempt_id"] for a in done}
        by_attempt = {a["attempt_id"]: a for a in attempts}
        for attempt_id in side:
            if attempt_id in committed:
                continue
            attempt = by_attempt.get(attempt_id)
            if attempt is not None and attempt["state"] == "failed" and plan.exit_code:
                continue  # the run that exits 3 on purpose: its own failure
            extra_runs += 1
            if attempt is None:
                problems.append(f"7: {job_id} side effect of unknown attempt")
                continue
            if not (
                attempt["state"] in ("lost", "failed")
                and attempt["node_id"] in chaos.faulted | chaos.partitioned
            ):
                problems.append(
                    f"7: {job_id} uncommitted run of {attempt['state']} attempt on "
                    "an unfaulted node"
                )
    live = asyncio.run(_live_attempts(pg_settings))
    if live:
        problems.append(f"4: {live} attempt rows still live")
    events = collections.Counter(
        str(r.get("event"))
        for r in fabric.controller_records()
        if r.get("event")
        in (
            "controller.jobs.execution_overlap",
            "controller.jobs.retry_scheduled",
            "controller.jobs.retries_exhausted",
        )
    )
    summary = {
        "seed": seed,
        "jobs": len(jobs),
        "states": dict(states),
        "retries": retries,
        "faults": dict(chaos.counts),
        "ledger_overlaps": overlaps,
        "overlap_events": events["controller.jobs.execution_overlap"],
        "retry_events": events["controller.jobs.retry_scheduled"],
        "exhausted_events": events["controller.jobs.retries_exhausted"],
        "uncommitted_completed_runs": extra_runs,
        "elapsed_s": round(time.monotonic() - started, 1),
    }
    print(f"chaos summary {json.dumps(summary, sort_keys=True)}", flush=True)  # noqa: T201
    record_property("chaos_summary", json.dumps(summary, sort_keys=True))
    out = os.environ.get("MESHFORGE_CHAOS_REPORT")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"chaos-{seed}.json").write_text(
            json.dumps({**summary, "fault_log": chaos.log}, indent=1)
        )
    assert not problems, "\n".join(problems[:40]) + f"\nfaults: {chaos.log}"


def _submit_retrying(
    fabric: Fabric, digest: str, ledger: Ledger, plan: Plan, key: str
) -> str:
    """Submit through a controller that may be restarting right now (the
    Idempotency-Key makes a re-POST after a lost answer the same job)."""
    manifest = ledger_manifest(
        ledger.root,
        sleep_s=plan.sleep_s,
        exit_code=plan.exit_code,
        name="chaos",
        retry={"max_attempts": MAX_ATTEMPTS, "backoff_s": plan.backoff_s},
        policy=plan.policy,
    )
    deadline = time.monotonic() + 30
    while True:
        try:
            return submit(fabric, digest, manifest, idempotency_key=key)
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)
