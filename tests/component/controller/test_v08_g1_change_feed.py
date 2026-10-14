"""V0.8 G1 deliverable 3 (D172): job/attempt events are published only after
their transaction commits - a rolled-back change and a refused report publish
nothing; an engine without a sink publishes nothing. Real PostgreSQL."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.ids import (
    new_attempt_id,
    new_job_id,
    new_node_id,
    new_session_id,
)
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.events import changes
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    ExecutionStore,
    NodeView,
    StatusKind,
    StatusReport,
)
from meshforge.controller.jobs.store import JobRecord, JobStoreError
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

DIGEST = "sha256:" + "ab" * 32


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    made = create_engine(pg_settings, pool_size=5)
    try:
        yield made
    finally:
        changes.detach(made)
        await made.dispose()


async def _node(engine: AsyncEngine) -> str:
    node = str(new_node_id())
    await Registry(engine).upsert_node_on_hello(
        node_id=node,
        hostname="h",
        os="linux",
        platform="x86_64",
        hardware={"capabilities": ["exec", "exec-subprocess"]},
        session_epoch=1,
        session_id=str(new_session_id()),
        state=NodeState.ONLINE,
    )
    return node


async def _job(store: ExecutionStore, node: str, owner: str | None) -> JobRecord:
    record, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest={"name": "demo"},
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": node},
        request_hash="h" * 64,
        idempotency_key=None,
        owner_user_id=owner,
    )
    return record


def _all(_job: JobRecord, view: NodeView | None) -> Dispatch | None:
    if view is None:
        return None
    return Dispatch(attempt_id=str(new_attempt_id()), start_snapshot={"k": 1})


async def test_committed_changes_are_published_in_order_with_their_owner(
    engine: AsyncEngine,
) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    store = ExecutionStore(engine)
    node = await _node(engine)
    job = await _job(store, node, None)
    # Noting the change must not leave a stale row in the session: the record
    # the store returns still carries what the transaction wrote last.
    assert job.state is JobState.QUEUED and job.scheduling.queued_at is not None
    assert [(c.kind, c.data["state"]) for c in seen] == [
        ("job", "submitted"),
        ("job", "planning"),
        ("job", "queued"),
    ]
    assert all(c.data["job_id"] == job.id for c in seen)
    seen.clear()
    (result,) = await store.dispatch_queued(_all, limit=10, max_per_node=4)
    assert result.attempt is not None
    kinds = [(c.kind, c.data["state"]) for c in seen]
    assert ("job", "preparing") in kinds and ("attempt", "assigned") in kinds
    attempt_note = next(c for c in seen if c.kind == "attempt")
    assert attempt_note.data["node_id"] == node
    assert attempt_note.data["attempt_id"] == result.attempt.id
    seen.clear()
    token = result.attempt.fencing_token
    assert await store.mark_start_acked(result.attempt.id, token)
    running = await store.apply_status(
        StatusReport(result.attempt.id, token, AttemptState.RUNNING), node_id=node
    )
    assert running.kind is StatusKind.APPLIED
    assert ("attempt", "running") in [(c.kind, c.data["state"]) for c in seen]
    seen.clear()
    # A refused report (another node) publishes nothing.
    other = await _node(engine)
    refused = await store.apply_status(
        StatusReport(result.attempt.id, token, AttemptState.DONE), node_id=other
    )
    assert refused.kind is StatusKind.WRONG_NODE
    assert seen == []


async def test_a_rolled_back_change_publishes_nothing(engine: AsyncEngine) -> None:
    seen: list[changes.Change] = []
    changes.attach(engine, seen.extend)
    sessions = create_session_factory(engine)

    class BoomError(Exception):
        pass

    async def noted_then_failed() -> None:
        async with sessions() as db, db.begin():
            changes.note_job(db, "job_x", "queued", owner_user_id=None)
            raise BoomError

    with pytest.raises(BoomError):
        await noted_then_failed()
    assert seen == []
    # The same note in a transaction that commits is published, once.
    async with sessions() as db, db.begin():
        changes.note_job(db, "job_y", "queued", owner_user_id=None)
    assert [c.data["job_id"] for c in seen] == ["job_y"]
    # A failing write of the real store (unknown owner -> FK refused) too.
    seen.clear()
    store = ExecutionStore(engine)
    node = await _node(engine)
    with pytest.raises(JobStoreError):
        await _job(store, node, "usr_01J0A11CE00000000000000000")
    assert seen == []


async def test_no_sink_means_no_notes(engine: AsyncEngine) -> None:
    store = ExecutionStore(engine)
    node = await _node(engine)
    job = await _job(store, node, None)  # nothing attached: must simply work
    assert job.state is JobState.QUEUED
