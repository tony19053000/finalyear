"""V0.10 G3: a split job's planner decision on a real PostgreSQL.

The pure planner's decision for a split job (Mode 3 set rows, Local / Mode 1
rows of one lane, a rejected ``split_too_small`` row, the extra estimate terms
and the margin test) goes through the real dispatch tick's decision write
(``plan_decision.candidates`` jsonb, inside its savepoint), comes back through
the decision read byte-for-byte, is accepted by the REST answer model
(``GET /v1/jobs/{id}/decisions``) and is rendered by ``compute explain``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import io
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from rich.console import Console

from meshforge.cli.explain_commands import decision_tables
from meshforge.common.ids import new_job_id
from meshforge.controller.api.decision_routes import _out
from meshforge.controller.jobs.decision_store import DecisionReads
from meshforge.controller.jobs.execution_store import ExecutionStore, Wait
from meshforge.controller.jobs.planning import decision_record
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitInfo,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

DIGEST = "sha256:" + "ab" * 32
MANIFEST = {"name": "demo", "entrypoint": ["python", "main.py"]}
EXEC = frozenset(
    {"exec", "exec-subprocess", "exec-split"}
)  # V0.10 G4: lanes need exec-split


@pytest_asyncio.fixture
async def engine_store(
    pg_settings: DatabaseSettings,
) -> AsyncIterator[tuple[ExecutionStore, DecisionReads]]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine), DecisionReads(engine)
    finally:
        await engine.dispose()


async def _queued(store: ExecutionStore) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(),
    )
    return job


def _split_decision(items: int, rate: float) -> Any:
    request = PlanRequest(
        job_id="job_01J0000000000000000000000",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        trees=(TreeNeed("sha256:" + "a" * 64, 1_000_000),),
        submitter_node_id="node_a",
        seeded=True,
        history=History(item_rate_by_node=dict.fromkeys(("node_a", "node_b"), rate)),
        split=SplitInfo(items=items),
    )
    nodes = tuple(
        NodeSnapshot(
            node_id=n, state="online", connected=True, capabilities=EXEC, cpu_slots=1
        )
        for n in ("node_a", "node_b", "node_c")
    )
    return plan(request, ClusterSnapshot(nodes=nodes), "cost_model")


async def test_split_decisions_round_trip_and_explain(
    engine_store: tuple[ExecutionStore, DecisionReads],
) -> None:
    store, reads = engine_store
    cases = {
        "split": _split_decision(3000, 10.0),
        "dont": _split_decision(3000, 3000.0),
        "small": _split_decision(3, 1.0),
    }
    jobs = {name: await _queued(store) for name in cases}
    by_job = {jobs[name].id: decision for name, decision in cases.items()}

    def decide(job: JobRecord, _fabric: Any) -> Any:
        return Wait(decision_record(by_job[job.id]))

    await store.dispatch_planned(
        decide,
        limit=10,
        max_per_node=8,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )
    for name, decision in cases.items():
        stored = await reads.decisions(jobs[name].id)
        assert len(stored) == 1, name
        assert stored[0].candidates == candidates_json(decision), name
        answer = _out(stored[0]).model_dump(mode="json")
        modes = [c["mode"] for c in answer["candidates"]]
        assert "split" in modes, name
        out = io.StringIO()
        console = Console(file=out, width=300, color_system=None)
        for table in decision_tables({"decisions": [answer]}):
            console.print(table)
        text = out.getvalue()
        expected = {
            "split": "split over 3 nodes:",
            "dont": "not split: the best set",
            "small": "not split: 3 item(s) are fewer than 2 x split.min_nodes",
        }[name]
        assert expected in text, text
