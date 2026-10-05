"""V0.6 G2: migration 0013 on real PostgreSQL (D105, D106, D110, D112).

* a job queued before the upgrade gets ``queued_at = created_at`` (it keeps its
  place in the queue); other states stay NULL;
* every new CHECK refuses the first out-of-range value and accepts the bound;
* ``plan_decision``: a ``dispatched`` row must name its attempt, any other row
  must not; candidates are a JSON array of at most 64 entries;
* ``create_queued_job`` stores the scheduling fields and stamps ``queued_at``;
* down/up cycles are clean (``alembic check`` after each).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meshforge.common.ids import new_job_id
from meshforge.controller.jobs.store import JobScheduling, JobStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "cd" * 32


async def _sql(
    pg: DatabaseSettings, statement: str, params: dict[str, Any] | None = None
) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params or {})
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _job_row(pg: DatabaseSettings, job_id: str, state: str) -> None:
    await _sql(
        pg,
        "INSERT INTO job (id, name, state, manifest, bundle_digest, placement, "
        "request_hash, created_at, updated_at) VALUES (:id, 'j', :state, '{}', "
        ":digest, '{}', :hash, now() - interval '1 hour', now())",
        {"id": job_id, "state": state, "digest": DIGEST, "hash": "a" * 64},
    )


@pytest_asyncio.fixture
async def migrated(pg_settings: DatabaseSettings) -> AsyncIterator[DatabaseSettings]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    yield pg_settings


async def test_upgrade_backfills_queued_at_for_queued_jobs_only(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    await asyncio.to_thread(command.upgrade, cfg, "0012")
    queued, running = str(new_job_id()), str(new_job_id())
    await _job_row(pg_settings, queued, "queued")
    await _job_row(pg_settings, running, "running")
    await asyncio.to_thread(command.upgrade, cfg, "0013")
    rows = dict(
        await _sql(
            pg_settings,
            "SELECT id, queued_at = created_at FROM job WHERE id IN (:a, :b)",
            {"a": queued, "b": running},
        )
    )
    assert rows[queued] is True
    assert rows[running] is None
    defaults = await _sql(
        pg_settings,
        "SELECT priority, policy, max_attempts, retry_backoff_s, on_lost_retry, "
        "submitter_seeded FROM job WHERE id = :a",
        {"a": queued},
    )
    assert defaults == [(0, None, 3, 5, True, False)]
    await asyncio.to_thread(command.check, cfg)


@pytest.mark.parametrize(
    ("column", "good", "bad"),
    [
        ("priority", [-10, 10], [-11, 11]),
        ("max_attempts", [1, 10], [0, 11]),
        ("retry_backoff_s", [0, 300], [-1, 301]),
        ("policy", ["cost_model", "random"], ["oracle", ""]),
    ],
)
async def test_job_checks_accept_the_bound_and_refuse_bound_plus_one(
    migrated: DatabaseSettings, column: str, good: list[Any], bad: list[Any]
) -> None:
    job_id = str(new_job_id())
    await _job_row(migrated, job_id, "queued")
    for value in good:
        await _sql(
            migrated,
            f"UPDATE job SET {column} = :v WHERE id = :i",  # noqa: S608 - fixed names
            {"v": value, "i": job_id},
        )
    for value in bad:
        with pytest.raises(IntegrityError):
            await _sql(
                migrated,
                f"UPDATE job SET {column} = :v WHERE id = :i",  # noqa: S608 - fixed names
                {"v": value, "i": job_id},
            )


async def test_node_slot_checks(migrated: DatabaseSettings) -> None:
    await _sql(
        migrated,
        "INSERT INTO node (node_id, hostname, os, platform, hardware, state, "
        "first_seen_at, last_seen_at, session_epoch) VALUES ('node_x', 'h', 'l', "
        "'p', '{}', 'online', now(), now(), 1)",
    )
    for column in ("cpu_slots", "gpu_slots"):
        for value in (0, 64, None):
            await _sql(migrated, f"UPDATE node SET {column} = :v", {"v": value})  # noqa: S608
        for value in (-1, 65):
            with pytest.raises(IntegrityError):
                await _sql(migrated, f"UPDATE node SET {column} = :v", {"v": value})  # noqa: S608


async def test_attempt_slot_check(migrated: DatabaseSettings) -> None:
    rows = await _sql(
        migrated,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'ck_attempt_slot'",
    )
    assert "cpu" in rows[0][0] and "gpu" in rows[0][0]


async def _decision(
    pg: DatabaseSettings,
    job_id: str,
    *,
    outcome: str,
    attempt_id: str | None = None,
    candidates: Any = (),
) -> None:
    await _sql(
        pg,
        "INSERT INTO plan_decision (job_id, attempt_no, attempt_id, outcome, "
        "policy, snapshot_digest, reason_key, candidates) VALUES (:j, 1, :a, :o, "
        "'cost_model', :d, :d, CAST(:c AS jsonb))",
        {
            "j": job_id,
            "a": attempt_id,
            "o": outcome,
            "d": "0" * 64,
            "c": json.dumps(
                list(candidates) if not isinstance(candidates, dict) else candidates
            ),
        },
    )


async def test_plan_decision_checks(migrated: DatabaseSettings) -> None:
    job_id = str(new_job_id())
    await _job_row(migrated, job_id, "queued")
    await _decision(migrated, job_id, outcome="waiting")
    await _decision(migrated, job_id, outcome="failed", candidates=[{}] * 64)
    with pytest.raises(IntegrityError):  # dispatched without its attempt
        await _decision(migrated, job_id, outcome="dispatched")
    with pytest.raises(IntegrityError):  # 65 candidates
        await _decision(migrated, job_id, outcome="waiting", candidates=[{}] * 65)
    with pytest.raises(IntegrityError):  # not an array
        await _decision(migrated, job_id, outcome="waiting", candidates={"a": 1})
    with pytest.raises(IntegrityError):
        await _decision(migrated, job_id, outcome="maybe")
    # The job's rows go with it.
    await _sql(migrated, "DELETE FROM job WHERE id = :j", {"j": job_id})
    left = await _sql(
        migrated, "SELECT count(*) FROM plan_decision WHERE job_id = :j", {"j": job_id}
    )
    assert left == [(0,)]


async def test_create_queued_job_stores_scheduling_and_queued_at(
    migrated: DatabaseSettings,
) -> None:
    engine = create_engine(migrated)
    try:
        store = JobStore(engine)
        job, created = await store.create_queued_job(
            job_id=str(new_job_id()),
            name="j",
            manifest={"name": "j"},
            bundle_digest=DIGEST,
            placement={"mode": "auto", "node_id": None},
            request_hash="b" * 64,
            idempotency_key=None,
            scheduling=JobScheduling(
                priority=7,
                policy="least_loaded",
                max_attempts=5,
                retry_backoff_s=0,
                on_lost_retry=False,
                submitter_node_id="node_01ABC",
                submitter_seeded=True,
                input_bytes=12345,
                fingerprint="f" * 64,
            ),
        )
        assert created
        plan = job.scheduling
        assert (plan.priority, plan.policy, plan.max_attempts) == (7, "least_loaded", 5)
        assert plan.retry_backoff_s == 0 and plan.on_lost_retry is False
        assert plan.submitter_node_id == "node_01ABC" and plan.submitter_seeded
        assert plan.input_bytes == 12345 and plan.fingerprint == "f" * 64
        assert plan.queued_at is not None and plan.not_before is None
        history = await store.transitions(job.id)
        queued_arrow = history[-1]
        assert queued_arrow.to_state.value == "queued"
        assert plan.queued_at >= queued_arrow.at
    finally:
        await engine.dispose()


async def test_down_up_cycles_are_clean(migrated: DatabaseSettings) -> None:
    cfg = alembic_config(migrated)
    for _ in range(2):
        await asyncio.to_thread(command.downgrade, cfg, "0012")
        tables = await _sql(
            migrated,
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name = 'plan_decision'",
        )
        assert tables == [(0,)]
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.check, cfg)
