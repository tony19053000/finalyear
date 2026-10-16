"""V0.8 G3 item 1 (D180): alerts are the ``attempt_wrong_node`` audit rows,
and the refusal's outcome carries the committed row's id - the key the live
``alert`` event shares with ``GET /v1/alerts`` (merge by id, no duplicates).

Real PostgreSQL: a real WRONG_NODE refusal through the execution store, then
:class:`~meshforge.controller.alerts.AlertLog` reads it back.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from alembic import command

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import AttemptState
from meshforge.controller.alerts import AlertLog
from meshforge.controller.enrollment.invites import audit
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
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.enrollment import AuditAction
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
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    made = create_engine(pg_settings, pool_size=10)
    try:
        yield made
    finally:
        await made.dispose()


async def _node(engine: Any) -> str:
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


def _dispatch_all(_job: JobRecord, node: NodeView | None) -> Dispatch | None:
    if node is None:
        return None
    return Dispatch(attempt_id=str(new_attempt_id()), start_snapshot={"k": 1})


async def test_a_refusal_names_its_audit_row_and_the_log_lists_it(engine: Any) -> None:
    store = ExecutionStore(engine)
    node_a, node_b = await _node(engine), await _node(engine)
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

    refused = await store.apply_status(
        StatusReport(attempt_id, token, AttemptState.DONE), node_id=node_b
    )
    assert refused.kind is StatusKind.WRONG_NODE
    assert refused.alert is not None
    assert refused.alert["job_id"] == job.id
    assert refused.alert["placed_node_id"] == node_a

    (listed,) = await AlertLog(engine).list(limit=10)
    assert str(listed.id) == refused.alert["id"]  # the merge key
    assert listed.kind == "wrong_node"
    assert listed.node_id == node_b and listed.actor == f"node:{node_b}"
    assert listed.details == {
        "attempt_id": attempt_id,
        "job_id": job.id,
        "placed_node_id": node_a,
        "reported": "done",
    }
    # Only a refusal names an alert.
    accepted = await store.apply_status(
        StatusReport(attempt_id, token, AttemptState.RUNNING), node_id=node_a
    )
    assert accepted.alert is None


async def test_the_log_pages_newest_first_and_lists_alert_actions_only(
    engine: Any,
) -> None:
    sessions = create_session_factory(engine)
    ids: list[int] = []
    for i in range(5):
        async with sessions() as db, db.begin():
            # Interleaved non-alert rows never appear in the list.
            await audit(db, AuditAction.LOGIN, actor="user:x", details={"i": i})
            row = await audit(
                db,
                AuditAction.ATTEMPT_WRONG_NODE,
                actor="node:n",
                node_id="node_x",
                details={"i": i},
            )
            await db.flush()
            ids.append(int(row.id))
    log = AlertLog(engine)
    first = await log.list(limit=2)
    assert [r.id for r in first] == ids[::-1][:2]
    rest = await log.list(limit=10, before=first[-1].id)
    assert [r.id for r in rest] == ids[::-1][2:]
    assert all(r.kind == "wrong_node" for r in first + rest)
    assert await log.list(limit=10, before=min(ids)) == []
