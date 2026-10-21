"""V0.14 G4: a small benchmark campaign run through the **real** dispatcher on a
real PostgreSQL, for the evaluator's component tests.

* :func:`register_nodes` - nodes in the registry (online, ``exec``);
* :func:`make_campaign` - a ``cpu.sort`` campaign (:class:`BenchStore`, the
  real expansion): ``local`` on the origin + ``mode1`` on the others, one group
  per ``repeats`` value;
* :func:`dispatch_runs` - every run submitted like the campaign submitter does
  (its placement, its ``bench:<run_id>`` key, the submitter node) and placed
  by real dispatch ticks (:class:`DispatchPlanner`): each ``plan_decision`` row
  carries the replay with the shadow request (D215, D292);
* :func:`finish_runs` - every attempt ended ``done`` (with a measured ``run``
  phase), its ``bench_run`` ``done`` and a run record with the given total -
  so the labels, the matched runs (calibration) and the evaluator all read
  what a real campaign leaves behind.
"""

from __future__ import annotations

import datetime as dt
import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.bench.campaign import NodeRef, expand, parse_spec, spec_digest
from meshforge.common.ids import (
    new_campaign_id,
    new_job_id,
    new_node_id,
    new_session_id,
)
from meshforge.controller.bench.store import BenchStore, RunRow
from meshforge.controller.bench.submit import run_placement
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.bench import BenchRun
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.schema import COLUMN_NAMES, SCHEMA_VERSION

__all__ = [
    "FAMILY",
    "FINGERPRINT",
    "Dispatched",
    "Manager",
    "dispatch_runs",
    "finish_runs",
    "make_campaign",
    "medians",
    "planner",
    "register_nodes",
]

FAMILY = "cpu.sort"
FINGERPRINT = "ab" * 32
MANIFEST = {"name": "bench", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "cd" * 32
T0 = dt.datetime(2026, 10, 20, 9, 0, tzinfo=dt.UTC)
_N = itertools.count(1)


@dataclass
class _Live:
    node_id: str
    session_id: str
    state: NodeState = NodeState.ONLINE
    rtt_ewma_ms: float | None = 1.0
    policy: Any = None
    policy_slots: Any = None
    clock: Any = field(default_factory=lambda: SimpleNamespace(latest=None))


class Manager:
    """Every node has a live session (the dispatcher's view)."""

    def __init__(self, nodes: Sequence[str]) -> None:
        self.lives = {n: _Live(n, str(new_session_id())) for n in nodes}

    def get(self, node_id: str) -> _Live | None:
        return self.lives.get(node_id)

    def get_lingering(self, node_id: str) -> None:
        return None


async def register_nodes(engine: AsyncEngine, count: int) -> list[str]:
    nodes = sorted(str(new_node_id()) for _ in range(count))
    registry = Registry(engine)
    for node in nodes:
        await registry.upsert_node_on_hello(
            node_id=node,
            hostname=f"h-{node[-4:]}",
            os="linux",
            platform="x86_64",
            hardware={"capabilities": ["exec", "exec-subprocess"]},
            session_epoch=1,
            session_id=str(new_session_id()),
            state=NodeState.ONLINE,
            hardware_scores=None,
            cpu_slots=64,
            gpu_slots=0,
        )
    return nodes


def planner(nodes: Sequence[str]) -> DispatchPlanner:
    return DispatchPlanner(
        manager=Manager(nodes),  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=60.0),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )


async def make_campaign(
    engine: AsyncEngine, nodes: Sequence[str], repeats: Sequence[int]
) -> tuple[str, list[RunRow]]:
    """``cpu.sort`` x one group per ``repeats`` value; local on ``nodes[0]``,
    mode1 on the others; 3 repetitions."""
    spec = parse_spec(
        {
            "name": "g4-eval",
            "seed": 3,
            "families": [{"family": FAMILY, "params": {"repeats": list(repeats)}}],
            "strategies": ["local", "mode1"],
            "nodes": list(nodes),
            "origin": nodes[0],
            "repetitions": 3,
            "budget": {"max_runs": 1000, "max_hours": 720},
        }
    )
    expansion = expand(spec, [NodeRef(n) for n in nodes])
    campaign_id = new_campaign_id()
    store = BenchStore(engine)
    await store.create_campaign(
        campaign_id=campaign_id,
        name=spec.name,
        spec_yaml="name: g4-eval\n",
        spec_digest=spec_digest(spec),
        seed=spec.seed,
        expansion=expansion,
        nodes=list(nodes),
        created_by=None,
    )
    return campaign_id, await store.runs(campaign_id, limit=10_000)


@dataclass(frozen=True)
class Dispatched:
    run: RunRow
    job_id: str
    attempt_id: str


async def dispatch_runs(
    store: ExecutionStore, decide: DispatchPlanner, runs: Sequence[RunRow]
) -> list[Dispatched]:
    """Submit every run like the campaign submitter and dispatch them."""
    jobs: dict[str, RunRow] = {}
    for run in runs:
        job_id = str(new_job_id())
        await store.create_queued_job(
            job_id=job_id,
            name="bench",
            manifest=MANIFEST,
            bundle_digest=DIGEST,
            placement=run_placement(run),
            request_hash="h" * 64,
            idempotency_key=run.job_key,
            scheduling=JobScheduling(
                fingerprint=FINGERPRINT,
                input_bytes=0,
                submitter_node_id=run.origin_node_id,
            ),
        )
        jobs[job_id] = run
    out: list[Dispatched] = []
    for _tick in range(20):
        results = await store.dispatch_planned(
            decide.decide,
            limit=500,
            max_per_node=500,
            now=dt.datetime.now(dt.UTC),
            aging_s=600.0,
        )
        placed = [
            (r.job.id, r.attempt.id)
            for r in results
            if r.attempt is not None and r.job.id in jobs
        ]
        out.extend(Dispatched(jobs.pop(job), job, attempt) for job, attempt in placed)
        if not jobs:
            break
    assert not jobs, f"{len(jobs)} runs were never dispatched"
    return out


async def finish_runs(
    engine: AsyncEngine,
    dispatched: Sequence[Dispatched],
    total_ms: Callable[[RunRow], int],
    *,
    run_ms: Callable[[RunRow], int] | None = None,
    ended_at: Callable[[Dispatched, int], dt.datetime] | None = None,
) -> None:
    """Every attempt ``done`` (``run`` phase = ``run_ms``, stage/env/collect
    skipped), its run ``done`` and an ``ok`` run record with ``total_ms``."""
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(dispatched):
            run = item.run
            end = (
                ended_at(item, index)
                if ended_at is not None
                else T0 + dt.timedelta(seconds=index)
            )
            await db.execute(
                text("UPDATE attempt SET state = 'done', ended_at = :e WHERE id = :a"),
                {"e": end, "a": item.attempt_id},
            )
            phases = (
                ("stage", 0, True),
                ("env", 0, True),
                ("run", (run_ms or total_ms)(run), False),
                ("collect", 0, True),
            )
            for phase, ms, skipped in phases:
                await db.execute(
                    text(
                        "INSERT INTO attempt_phase (attempt_id, phase, started_at, "
                        "duration_ms, skipped) VALUES (:a, :p, now(), :ms, :s)"
                    ),
                    {"a": item.attempt_id, "p": phase, "ms": ms, "s": skipped},
                )
            await db.execute(
                update(BenchRun)
                .where(BenchRun.id == run.id)
                .values(state="done", job_id=item.job_id)
            )
            row: dict[str, Any] = dict.fromkeys(COLUMN_NAMES)
            row.update(
                schema_version=SCHEMA_VERSION,
                attempt_id=item.attempt_id,
                job_id=item.job_id,
                attempt_no=1,
                campaign_id=run.campaign_id,
                group_id=run.group_id,
                config_id=run.config_id,
                repetition=run.repetition,
                fingerprint=FINGERPRINT,
                node_id=run.node_id,
                submitter_node_id=run.origin_node_id,
                strategy=run.strategy,
                placement_mode="pinned",
                workload_family=run.family,
                workload_params=dict(run.params),
                workload_status="ok",
                condition="none",
                condition_measured={"verified": True, "reason": None},
                total_ms=total_ms(run),
                attempt_state="done",
                failure_class="ok",
                attempt_created_at=end - dt.timedelta(seconds=1),
                attempt_ended_at=end,
                recorded_at=end,
            )
            rows.append(row)
            next(_N)
        if rows:
            await db.execute(insert(RunRecord), rows)


def medians(by_node: Mapping[str, int]) -> Callable[[RunRow], int]:
    """total_ms = the node's value + the repetition (a tight, clear spread)."""
    return lambda run: by_node[run.node_id] + run.repetition
