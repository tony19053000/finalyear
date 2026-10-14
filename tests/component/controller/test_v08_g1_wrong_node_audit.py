"""V0.8 G1 item 0 (D168 N3): a WRONG_NODE refusal leaves an ``audit_event``.

The refusal itself is V0.7 G7 (THREAT_MODEL CG-E2,
``test_v07_g7_cross_node_reports.py``); this file proves that each refusal is
also recorded - ``attempt_wrong_node``, actor and ``node_id`` = the reporting
node, the attempt's own node in ``details`` - in the same transaction, while
an accepted report from the right node records nothing. Real PostgreSQL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from alembic import command
from sqlalchemy import select

from meshforge.common.ids import (
    new_attempt_id,
    new_job_id,
    new_node_id,
    new_session_id,
)
from meshforge.common.states import AttemptState
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    ExecutionStore,
    NodeView,
    StatusKind,
    StatusReport,
)
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.enrollment import AuditAction, AuditEvent
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]

DIGEST = "sha256:" + "ab" * 32
MANIFEST: dict[str, Any] = {
    "name": "demo",
    "entrypoint": ["python", "main.py"],
    "limits": {"timeout": 60, "network": "default"},
}


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=10)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _node(pg_settings: DatabaseSettings) -> str:
    engine = create_engine(pg_settings)
    try:
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
    finally:
        await engine.dispose()


def _dispatch_all(_job: JobRecord, node: NodeView | None) -> Dispatch | None:
    if node is None:
        return None
    return Dispatch(attempt_id=str(new_attempt_id()), start_snapshot={"k": 1})


async def _audit_rows(pg_settings: DatabaseSettings) -> list[AuditEvent]:
    engine = create_engine(pg_settings)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(AuditEvent).where(
                    AuditEvent.action == AuditAction.ATTEMPT_WRONG_NODE.value
                )
            )
            return list(rows.all())  # type: ignore[arg-type]
    finally:
        await engine.dispose()


async def test_each_wrong_node_refusal_is_audited_and_an_accepted_report_is_not(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node_a = await _node(pg_settings)
    node_b = await _node(pg_settings)
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": node_a},
        request_hash="h" * 64,
        idempotency_key=None,
    )
    (result,) = await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=4)
    assert result.attempt is not None
    attempt_id, token = result.attempt.id, result.attempt.fencing_token
    assert await store.mark_start_acked(attempt_id, token)
    ok = await store.apply_status(
        StatusReport(attempt_id, token, AttemptState.RUNNING), node_id=node_a
    )
    assert ok.kind is StatusKind.APPLIED
    assert await _audit_rows(pg_settings) == []  # the right node: no row

    for state in (AttemptState.DONE, AttemptState.FAILED):
        refused = await store.apply_status(
            StatusReport(attempt_id, token, state), node_id=node_b
        )
        assert refused.kind is StatusKind.WRONG_NODE

    rows = await _audit_rows(pg_settings)
    assert len(rows) == 2
    for row, state in zip(rows, ("done", "failed"), strict=True):
        assert row.actor == f"node:{node_b}"
        assert row.node_id == node_b
        assert row.details == {
            "attempt_id": attempt_id,
            "job_id": job.id,
            "placed_node_id": node_a,
            "reported": state,
        }
    # The refusal changed nothing but the audit trail.
    attempt = await store.get_attempt(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.RUNNING


async def test_an_unknown_attempt_is_not_audited_as_wrong_node(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    node_b = await _node(pg_settings)
    outcome = await store.apply_status(
        StatusReport(str(new_attempt_id()), 1, AttemptState.DONE), node_id=node_b
    )
    assert outcome.kind is StatusKind.UNKNOWN
    assert await _audit_rows(pg_settings) == []
