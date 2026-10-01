"""V0.5 G2 (D86, migration 0010): an attempt reported as ``venv`` is stored on real
PostgreSQL; downgrading to 0009 maps it to ``subprocess`` and restores the narrow
CHECK; upgrading again allows ``venv``."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
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
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "ab" * 32
MANIFEST: dict[str, Any] = {"name": "demo", "entrypoint": ["python", "main.py"]}


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _venv_attempt(store: ExecutionStore, pg_settings: DatabaseSettings) -> str:
    engine = create_engine(pg_settings)
    try:
        node = str(new_node_id())
        await Registry(engine).upsert_node_on_hello(
            node_id=node,
            hostname="h",
            os="linux",
            platform="x86_64",
            hardware={"capabilities": ["exec", "exec-venv"]},
            session_epoch=1,
            session_id=str(new_session_id()),
            state=NodeState.ONLINE,
        )
    finally:
        await engine.dispose()
    await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": node},
        request_hash="h" * 64,
        idempotency_key=None,
    )

    def dispatch(_job: JobRecord, view: NodeView | None) -> Dispatch | None:
        return (
            None
            if view is None
            else Dispatch(attempt_id=str(new_attempt_id()), start_snapshot={})
        )

    (result,) = await store.dispatch_queued(dispatch, limit=10, max_per_node=1)
    assert result.attempt is not None
    applied = await store.apply_status(
        StatusReport(result.attempt.id, 1, AttemptState.STAGING, runtime="venv")
    )
    assert applied.kind is StatusKind.APPLIED
    assert applied.attempt is not None and applied.attempt.runtime == "venv"
    return result.attempt.id


async def _runtime_of(pg_settings: DatabaseSettings, attempt_id: str) -> str | None:
    engine = create_engine(pg_settings)
    try:
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT runtime FROM attempt WHERE id = :id"), {"id": attempt_id}
            )
            value = row.scalar_one()
            return None if value is None else str(value)
    finally:
        await engine.dispose()


async def _set_runtime(
    pg_settings: DatabaseSettings, attempt_id: str, value: str
) -> None:
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE attempt SET runtime = :v WHERE id = :id"),
                {"v": value, "id": attempt_id},
            )
    finally:
        await engine.dispose()


async def test_venv_attempt_is_stored_and_migration_0010_round_trips(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    attempt_id = await _venv_attempt(store, pg_settings)
    assert await _runtime_of(pg_settings, attempt_id) == "venv"
    cfg = alembic_config(pg_settings)
    await asyncio.to_thread(command.downgrade, cfg, "0009")
    assert await _runtime_of(pg_settings, attempt_id) == "subprocess"
    with pytest.raises(IntegrityError):
        await _set_runtime(pg_settings, attempt_id, "venv")  # 0009's narrow CHECK
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await _set_runtime(pg_settings, attempt_id, "venv")
    assert await _runtime_of(pg_settings, attempt_id) == "venv"
    with pytest.raises(IntegrityError):
        await _set_runtime(pg_settings, attempt_id, "kubernetes")
