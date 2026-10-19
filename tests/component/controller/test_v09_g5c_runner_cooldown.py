"""V0.9 G5c item 0.0 (D255): the runner's cool-down never misses a run whose
job reached terminal **after** the tick's ``sync`` - real PostgreSQL.

The race the whole-component gate hit (a node re-submitted 0.034 s after its
previous run ended, cool-down 1 s): ``tick`` runs ``BenchStore.sync`` first,
then reads the node's load (non-terminal benchmark jobs) and its last end
(``bench_run.ended_at``). A job that went terminal between the two left the
node "free" (load 0) while its run was still ``submitted`` with no
``ended_at`` - so the cool-down read the previous end (or none). The fix
derives both from one snapshot and counts a terminal job of a
still-``submitted`` run as an end at its final transition time.

These tests force that ordering deterministically: the store's ``sync`` is
wrapped so the job is moved to terminal right after the real ``sync`` returns.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.bench.campaign import NodeRef, expand, parse_spec, spec_digest
from meshforge.common.ids import new_campaign_id, new_job_id
from meshforge.common.states import JobState
from meshforge.controller.bench.control import CampaignControl, Throttle
from meshforge.controller.bench.runner import BenchRunner, RunnerSettings
from meshforge.controller.bench.store import BenchStore, RunRow
from meshforge.controller.bench.submit import IDEMPOTENCY_PREFIX, SubmitBatch
from meshforge.controller.jobs.store import JobScheduling, JobStore
from meshforge.controller.jobs.transitions import JobEvent
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

SPEC: dict[str, Any] = {
    "name": "cooldown-race",
    "seed": 7,
    "families": [{"family": "cpu.sort"}],
    "strategies": ["local"],
    "repetitions": 3,
    "budget": {"max_runs": 100},
}
NODES = [NodeRef("node_a"), NodeRef("node_b")]
SPEC_YAML = (
    "name: cooldown-race\nseed: 7\nfamilies: [{family: cpu.sort}]\n"
    "strategies: [local]\nrepetitions: 3\nbudget: {max_runs: 100}\n"
)


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


class _Submitter:
    """Records what the runner asked for; submits nothing."""

    def __init__(self) -> None:
        self.budgets: list[dict[str, int]] = []

    async def submit_next(
        self, _record: Any, _count: int, *, node_budget: Any, **_kw: Any
    ) -> SubmitBatch:
        self.budgets.append(dict(node_budget))
        return SubmitBatch()


class _LateEndStore(BenchStore):
    """``sync`` runs, **then** the hook ends a job - the gate's ordering."""

    after_sync: Any = None

    async def sync(self, campaign_id: str | None = None) -> int:
        moved = await super().sync(campaign_id)
        hook, self.after_sync = self.after_sync, None
        if hook is not None:
            await hook()
        return moved


async def _setup(
    engine: AsyncEngine, *, cooldown_s: float
) -> tuple[_LateEndStore, CampaignControl, JobStore, str]:
    store, control, jobs = (
        _LateEndStore(engine),
        CampaignControl(engine),
        JobStore(engine),
    )
    spec = parse_spec(SPEC)
    campaign_id = new_campaign_id()
    await store.create_campaign(
        campaign_id=campaign_id,
        name=spec.name,
        spec_yaml=SPEC_YAML,
        spec_digest=spec_digest(spec),
        seed=spec.seed,
        expansion=expand(spec, NODES),
        nodes=[n.node_id for n in NODES],
        created_by=None,
    )
    assert await control.start(
        campaign_id, Throttle(max_concurrent_per_node=1, cooldown_s=cooldown_s)
    )
    return store, control, jobs, campaign_id


async def _submit(jobs: JobStore, store: BenchStore, run: RunRow) -> str:
    job_id = str(new_job_id())
    await jobs.create_queued_job(
        job_id=job_id,
        name="bench",
        manifest={"name": "bench", "entrypoint": ["python", "x.py"]},
        bundle_digest="sha256:" + "0" * 64,
        placement={"mode": "offload", "node_id": run.node_id},
        request_hash="h" * 64,
        idempotency_key=f"{IDEMPOTENCY_PREFIX}{run.id}",
        scheduling=JobScheduling(submitter_node_id=run.origin_node_id),
    )
    assert await store.mark_submitted(run.id, job_id, generation=0)
    return job_id


def _runner(
    store: BenchStore, control: CampaignControl
) -> tuple[BenchRunner, _Submitter]:
    submitter = _Submitter()
    runner = BenchRunner(
        store=store,
        control=control,
        submitter=submitter,  # type: ignore[arg-type]
        settings=RunnerSettings(),
    )
    return runner, submitter


async def _first_run(store: BenchStore, campaign_id: str, node: str) -> RunRow:
    return next(
        r for r in await store.runs(campaign_id, limit=100) if r.node_id == node
    )


async def test_a_job_ending_after_sync_still_holds_its_node_in_cool_down(
    engine: AsyncEngine,
) -> None:
    store, control, jobs, cid = await _setup(engine, cooldown_s=30.0)
    run = await _first_run(store, cid, "node_a")
    job_id = await _submit(jobs, store, run)
    runner, submitter = _runner(store, control)

    async def end_job() -> None:
        await jobs.transition_job(job_id, JobEvent.CANCEL, expected=JobState.QUEUED)

    # the job is running during sync (run stays submitted, no ended_at) and
    # ends right after it: load says 0, the run has no end yet
    store.after_sync = end_job
    report = await runner.tick()
    assert report.excluded.get("node_a") == "cooldown", report
    assert all("node_a" not in b for b in submitter.budgets), submitter.budgets
    assert submitter.budgets == [{"node_b": 1}]
    # the next tick's sync settles it: still in cool-down (from the job's end)
    report = await runner.tick()
    assert report.excluded.get("node_a") == "cooldown"


async def test_a_terminal_unsynced_job_ended_long_ago_frees_the_node(
    engine: AsyncEngine,
) -> None:
    store, control, jobs, cid = await _setup(engine, cooldown_s=30.0)
    run = await _first_run(store, cid, "node_a")
    job_id = await _submit(jobs, store, run)
    runner, submitter = _runner(store, control)
    long_ago = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)

    async def end_job_long_ago() -> None:
        await jobs.transition_job(job_id, JobEvent.CANCEL, expected=JobState.QUEUED)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE job_transition SET at = :t WHERE job_id = :j"),
                {"t": long_ago, "j": job_id},
            )

    store.after_sync = end_job_long_ago
    report = await runner.tick()
    assert "node_a" not in report.excluded, report
    assert submitter.budgets == [{"node_a": 1, "node_b": 1}]


async def test_a_running_job_keeps_the_node_full_not_free(
    engine: AsyncEngine,
) -> None:
    store, control, jobs, cid = await _setup(engine, cooldown_s=30.0)
    run = await _first_run(store, cid, "node_a")
    await _submit(jobs, store, run)
    runner, submitter = _runner(store, control)
    report = await runner.tick()
    assert report.excluded.get("node_a") == "full"
    assert submitter.budgets == [{"node_b": 1}]


async def test_node_slots_sees_a_job_running_or_ended_never_neither(
    engine: AsyncEngine,
) -> None:
    """Before the end the job fills the slot; after it (not yet synced) it is
    an end the cool-down counts from - no state in between. (The single
    REPEATABLE READ snapshot covers the reads racing an end; that ordering is
    not forceable from outside one statement, so it is argued, not timed.)"""
    store, control, jobs, cid = await _setup(engine, cooldown_s=30.0)
    run = await _first_run(store, cid, "node_a")
    job_id = await _submit(jobs, store, run)
    load, since = await control.node_slots(cid, {"node_a", "node_b"})
    assert load == {"node_a": 1} and since == {}
    await jobs.transition_job(job_id, JobEvent.CANCEL, expected=JobState.QUEUED)
    load, since = await control.node_slots(cid, {"node_a", "node_b"})
    assert load == {} and set(since) == {"node_a"} and since["node_a"] < 30.0


# --- G5c fix r1 (tester T-2): the cool-down is per node, across campaigns ----


async def _bench_job(jobs: JobStore, node: str, key: str) -> str:
    job_id = str(new_job_id())
    await jobs.create_queued_job(
        job_id=job_id,
        name="bench",
        manifest={"name": "bench", "entrypoint": ["python", "x.py"]},
        bundle_digest="sha256:" + "0" * 64,
        placement={"mode": "offload", "node_id": node},
        request_hash="h" * 64,
        idempotency_key=key,
        scheduling=JobScheduling(submitter_node_id=node),
    )
    return job_id


async def test_another_campaigns_filler_ending_on_the_node_holds_the_cool_down(
    engine: AsyncEngine,
) -> None:
    """A queue filler (no ``bench_run`` row of its own) of campaign B ends on
    node_a: campaign A's runner leaves node_a alone for A's ``cooldown_s``."""
    store, control, jobs, cid_a = await _setup(engine, cooldown_s=30.0)
    _store_b, _control_b, _jobs_b, cid_b = await _setup(engine, cooldown_s=30.0)
    run_b = await _first_run(store, cid_b, "node_a")
    filler = await _bench_job(
        jobs, "node_a", f"{IDEMPOTENCY_PREFIX}{run_b.id}:filler:1"
    )
    load, since = await control.node_slots(cid_a, {"node_a", "node_b"})
    assert load == {"node_a": 1} and since == {}
    await jobs.transition_job(filler, JobEvent.CANCEL, expected=JobState.QUEUED)
    load, since = await control.node_slots(cid_a, {"node_a", "node_b"})
    assert load == {} and set(since) == {"node_a"} and since["node_a"] < 30.0
    assert set(await control.seconds_since_last_end(cid_a, {"node_a"})) == {"node_a"}


async def test_a_non_bench_job_ending_on_the_node_is_no_cool_down(
    engine: AsyncEngine,
) -> None:
    _store, control, jobs, cid = await _setup(engine, cooldown_s=30.0)
    user_job = await _bench_job(jobs, "node_a", "user-key-1")
    await jobs.transition_job(user_job, JobEvent.CANCEL, expected=JobState.QUEUED)
    _load, since = await control.node_slots(cid, {"node_a"})
    assert since == {}


async def test_ends_older_than_the_lookback_are_not_read(engine: AsyncEngine) -> None:
    """The read is bounded to ``cooldown_s`` + margin: an end before it cannot
    hold a node, so it is absent (the same decision as an old end)."""
    store, control, jobs, cid = await _setup(engine, cooldown_s=1.0)
    run = await _first_run(store, cid, "node_a")
    job_id = await _submit(jobs, store, run)
    await jobs.transition_job(job_id, JobEvent.CANCEL, expected=JobState.QUEUED)
    await store.sync(cid)
    _load, since = await control.node_slots(cid, {"node_a"})
    assert set(since) == {"node_a"}
    old = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE job SET updated_at = :t WHERE id = :j"),
            {"t": old, "j": job_id},
        )
        await conn.execute(
            text("UPDATE job_transition SET at = :t WHERE job_id = :j"),
            {"t": old, "j": job_id},
        )
        await conn.execute(
            text("UPDATE bench_run SET ended_at = :t WHERE id = :r"),
            {"t": old, "r": run.id},
        )
    _load, since = await control.node_slots(cid, {"node_a"})
    assert since == {}
