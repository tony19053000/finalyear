"""V0.9 G5c tester r1 (item 0.0 / 0.5): the cool-down and slot invariants under
adversarial orderings - real PostgreSQL.

* hammer: two exclusive runners tick back to back while a completer ends
  benchmark jobs at random moments; afterwards, per node, the next submission
  is never earlier than ``cooldown_s`` after the previous job's final
  transition, and a node never carries more than one open bench job;
* a requeued (retried) run keeps its previous end for the cool-down;
* cool-down across campaigns on one node (the brief: "never submitted to
  < cooldown_s after its last bench job reached terminal").
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import random
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_v09_g5c_runner_cooldown import (  # type: ignore[import-not-found]
    SPEC,
    _first_run,
    _setup,
    _submit,
)

from meshforge.common.ids import new_job_id
from meshforge.common.states import JobState
from meshforge.controller.bench.control import CampaignControl
from meshforge.controller.bench.keys import run_key
from meshforge.controller.bench.runner import BenchRunner, RunnerSettings
from meshforge.controller.bench.store import BenchStore
from meshforge.controller.bench.submit import SubmitBatch
from meshforge.controller.jobs.store import JobScheduling, JobStore
from meshforge.controller.jobs.transitions import JobEvent
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

COOLDOWN = 0.4


@pytest_asyncio.fixture
async def engines(
    pg_settings: DatabaseSettings,
) -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    one, two = create_engine(pg_settings), create_engine(pg_settings)
    yield one, two
    await one.dispose()
    await two.dispose()


class _GenerationSubmitter:
    """Like the real submitter's database effect: one job per run generation
    under ``bench:<run>:<generation>``."""

    def __init__(self, store: BenchStore, jobs: JobStore) -> None:
        self.store, self.jobs = store, jobs

    async def submit_next(
        self, record: Any, _count: int, *, node_budget: Any, **_kw: Any
    ) -> SubmitBatch:
        planned = [
            r
            for r in await self.store.runs(record.id, limit=1000)
            if r.state == "planned"
        ]
        for node in sorted(node_budget):
            run = next((r for r in planned if r.node_id == node), None)
            if run is None:
                continue
            job_id = str(new_job_id())
            await self.jobs.create_queued_job(
                job_id=job_id,
                name="bench",
                manifest={"name": "bench", "entrypoint": ["python", "x.py"]},
                bundle_digest="sha256:" + "0" * 64,
                placement={"mode": "offload", "node_id": run.node_id},
                request_hash="h" * 64,
                idempotency_key=run_key(run.id, run.generation),
                scheduling=JobScheduling(submitter_node_id=run.origin_node_id),
            )
            await self.store.mark_submitted(run.id, job_id, generation=run.generation)
        return SubmitBatch()


def _exclusive_runner(engine: AsyncEngine) -> BenchRunner:
    store, jobs = BenchStore(engine), JobStore(engine)
    return BenchRunner(
        store=store,
        control=CampaignControl(engine),
        submitter=_GenerationSubmitter(store, jobs),  # type: ignore[arg-type]
        settings=RunnerSettings(),
        exclusive=True,
    )


async def test_hammer_two_runners_random_ends_never_break_slot_or_cooldown(
    engines: tuple[AsyncEngine, AsyncEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(SPEC, "repetitions", 8)
    one, two = engines
    _store, _control, jobs, cid = await _setup(one, cooldown_s=COOLDOWN)
    runners = [_exclusive_runner(one), _exclusive_runner(two)]
    rng = random.Random(1905)  # noqa: S311
    stop = asyncio.Event()
    max_open = 0

    async def completer() -> None:
        nonlocal max_open
        while not stop.is_set():
            await asyncio.sleep(rng.uniform(0.0, 0.25))
            async with one.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            "SELECT j.id, j.placement->>'node_id' FROM job j "
                            "WHERE j.idempotency_key LIKE 'bench:%' "
                            "AND j.state NOT IN ('succeeded','failed','cancelled')"
                        )
                    )
                ).all()
            per_node: dict[str, int] = {}
            for _jid, node in rows:
                per_node[node] = per_node.get(node, 0) + 1
            max_open = max([max_open, *per_node.values()])
            if rows:
                jid, _node = rng.choice(rows)
                with contextlib.suppress(Exception):  # job moved on meanwhile
                    await jobs.transition_job(
                        jid, JobEvent.CANCEL, expected=JobState.QUEUED
                    )

    async def ticker(runner: BenchRunner) -> None:
        while not stop.is_set():
            await runner.tick()
            await asyncio.sleep(rng.uniform(0.0, 0.03))

    tasks = [asyncio.create_task(completer())] + [
        asyncio.create_task(ticker(r)) for r in runners
    ]
    await asyncio.sleep(25)
    stop.set()
    await asyncio.gather(*tasks)

    async with one.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT r.node_id, r.id, r.submitted_at, j.id, "
                    "(SELECT t.at FROM job_transition t WHERE t.job_id = j.id "
                    " ORDER BY t.seq DESC LIMIT 1) AS final_at, j.state "
                    "FROM bench_run r JOIN job j ON j.id = r.job_id "
                    "WHERE r.campaign_id = :c ORDER BY r.node_id, r.submitted_at"
                ),
                {"c": cid},
            )
        ).all()
    assert len(rows) >= 6, "the hammer must have driven several submissions"
    assert max_open <= 1, max_open
    by_node: dict[str, list[Any]] = {}
    for row in rows:
        by_node.setdefault(row[0], []).append(row)
    gaps = 0
    for node, seq in by_node.items():
        for prev, nxt in itertools.pairwise(seq):
            final_at = prev[4]
            assert final_at is not None, (node, prev)  # next submitted while prev open
            gap = (nxt[2] - final_at).total_seconds()
            gaps += 1
            assert gap >= COOLDOWN - 0.02, (node, gap, prev, nxt)
    assert gaps >= 2


async def test_a_requeued_run_keeps_its_previous_end_for_the_cool_down(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    one, _two = engines
    store, control, jobs, cid = await _setup(one, cooldown_s=30.0)
    run = await _first_run(store, cid, "node_a")
    job_id = await _submit(jobs, store, run)
    await jobs.transition_job(job_id, JobEvent.CANCEL, expected=JobState.QUEUED)
    await store.sync(cid)  # settles: ended_at stamped (failed or requeued)
    load, since = await control.node_slots(cid, {"node_a"})
    assert load == {} and "node_a" in since and since["node_a"] < 30.0
    # whatever the run became (failed / planned again), the end is still there
    runs = {r.id: r for r in await store.runs(cid, limit=100)}
    assert runs[run.id].state in {"failed", "planned", "done"}


@pytest.mark.xfail(
    strict=True,
    reason="tester r1 T-2: the cool-down is read per campaign "
    "(_last_end filters BenchRun.campaign_id), so another campaign's job that "
    "just ended on the node does not hold it",
)
async def test_cool_down_also_holds_after_another_campaign_ended_on_the_node(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    """Brief 5g 0.0: "a node is never submitted to < cooldown_s after its last
    bench job reached terminal" - not only this campaign's last job. Campaign
    B's job ends on node_a; campaign A (cool-down 30 s) must not submit to it."""
    one, _two = engines
    store_a, control_a, jobs, cid_a = await _setup(one, cooldown_s=30.0)
    _store_b, _control_b, _jobs_b, cid_b = await _setup(one, cooldown_s=30.0)
    run_b = await _first_run(store_a, cid_b, "node_a")
    job_b = await _submit(jobs, store_a, run_b)
    await jobs.transition_job(job_b, JobEvent.CANCEL, expected=JobState.QUEUED)
    await store_a.sync()
    _load, since = await control_a.node_slots(cid_a, {"node_a"})
    assert "node_a" in since and since["node_a"] < 30.0, (
        "campaign A's cool-down ignores a bench job that ended on the node "
        "seconds ago (belonging to campaign B)"
    )
