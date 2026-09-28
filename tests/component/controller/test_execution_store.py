"""ExecutionStore on real PostgreSQL (V0.4 G4: D65, D66, D68, D69).

Every write is one transaction on real rows: dispatch commits the attempt and the
``QUEUED -> PREPARING`` arrow together; a status report is fenced on the token and
idempotent on the state; the terminal transaction writes all five phase rows.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id, new_node_id, new_session_id
from meshforge.common.states import AttemptPhase, AttemptState, JobState
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchFailure,
    ExecutionStore,
    NodeView,
    StatusKind,
    StatusReport,
)
from meshforge.controller.jobs.progress import AgentPhases
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.jobs.transitions import AttemptEvent
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

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


@pytest_asyncio.fixture
async def node_id(pg_settings: DatabaseSettings, store: ExecutionStore) -> str:
    return await _node(pg_settings)


async def _queued(store: ExecutionStore, node_id: str) -> JobRecord:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": node_id},
        request_hash="h" * 64,
        idempotency_key=None,
    )
    return job


def _dispatch_all(_job: JobRecord, node: NodeView | None) -> Dispatch | None:
    if node is None:
        return None
    return Dispatch(attempt_id=str(new_attempt_id()), start_snapshot={"k": 1})


async def test_dispatch_commits_attempt_and_preparing_together(
    store: ExecutionStore, node_id: str
) -> None:
    job = await _queued(store, node_id)
    (result,) = await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=1)
    assert result.attempt is not None
    assert result.attempt.fencing_token == 1
    assert result.attempt.state is AttemptState.ASSIGNED
    assert result.attempt.start_snapshot == {"k": 1}
    assert result.job.state is JobState.PREPARING
    stored = await store.get_job(job.id)
    assert stored is not None and stored.state is JobState.PREPARING
    assert stored.attempt_epoch == 1
    # nothing left to dispatch
    assert await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=1) == []


async def test_one_attempt_per_node_at_a_time(
    store: ExecutionStore, node_id: str
) -> None:
    first = await _queued(store, node_id)
    second = await _queued(store, node_id)
    results = await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=1)
    assert [r.job.id for r in results] == [first.id]
    stored = await store.get_job(second.id)
    assert stored is not None and stored.state is JobState.QUEUED


async def test_a_refused_dispatch_fails_the_queued_job(
    store: ExecutionStore, node_id: str
) -> None:
    job = await _queued(store, node_id)

    def refuse(_job: JobRecord, _node: NodeView | None) -> DispatchFailure:
        return DispatchFailure(FailureType.NODE_LOST, "placed_node_offline")

    (result,) = await store.dispatch_queued(refuse, limit=10, max_per_node=1)
    assert result.attempt is None
    stored = await store.get_job(job.id)
    assert stored is not None and stored.state is JobState.FAILED
    assert stored.failure_type is FailureType.NODE_LOST
    assert stored.failure_detail == "placed_node_offline"


async def _running_attempt(store: ExecutionStore, node_id: str) -> tuple[str, str]:
    job = await _queued(store, node_id)
    (result,) = await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=1)
    assert result.attempt is not None
    assert await store.mark_start_acked(result.attempt.id, 1)
    assert not await store.mark_start_acked(result.attempt.id, 1)  # first wins
    return job.id, result.attempt.id


async def test_success_path_moves_the_job_and_writes_five_phase_rows(
    store: ExecutionStore, node_id: str
) -> None:
    job_id, attempt_id = await _running_attempt(store, node_id)
    staging = await store.apply_status(
        StatusReport(attempt_id, 1, AttemptState.STAGING, runtime="subprocess")
    )
    assert staging.kind is StatusKind.APPLIED
    # skip ENV_READY/RUNNING/UPLOADING: the path is walked, every arrow recorded
    done = await store.apply_status(
        StatusReport(
            attempt_id,
            1,
            AttemptState.DONE,
            exit_code=0,
            phases=AgentPhases(stage_s=0.25, env_s=0.0, run_s=1.5, collect_s=0.01),
        )
    )
    assert done.kind is StatusKind.APPLIED and done.ended
    assert done.job is not None and done.job.state is JobState.SUCCEEDED
    assert done.attempt is not None and done.attempt.exit_code == 0
    assert done.attempt.runtime == "subprocess"
    assert done.attempt.started_at is not None and done.attempt.ended_at is not None
    history = [t.to_state for t in await store.transitions(job_id)]
    assert history == [
        JobState.SUBMITTED,
        JobState.PLANNING,
        JobState.QUEUED,
        JobState.PREPARING,
        JobState.RUNNING,
        JobState.COLLECTING,
        JobState.SUCCEEDED,
    ]
    phases = {p.phase: p for p in await store.phases(attempt_id)}
    assert set(phases) == {p.value for p in AttemptPhase}
    assert phases["run"].duration_ms == 1500 and not phases["run"].skipped
    assert phases["env"].duration_ms == 0 and not phases["env"].skipped  # measured 0
    assert not phases["queue"].skipped
    # a replayed DONE is a duplicate, never a second terminal write
    again = await store.apply_status(StatusReport(attempt_id, 1, AttemptState.DONE))
    assert again.kind is StatusKind.DUPLICATE


async def test_stale_token_and_contradicting_terminal_reports_are_fenced(
    store: ExecutionStore, node_id: str
) -> None:
    _job_id, attempt_id = await _running_attempt(store, node_id)
    stale = await store.apply_status(StatusReport(attempt_id, 7, AttemptState.RUNNING))
    assert stale.kind is StatusKind.FENCED
    lost = await store.end_attempt(
        attempt_id,
        1,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail="agent_lost_attempt",
    )
    assert lost.kind is StatusKind.APPLIED
    assert lost.attempt is not None and lost.attempt.state is AttemptState.LOST
    assert lost.job is not None and lost.job.state is JobState.FAILED
    assert lost.job.failure_type is FailureType.NODE_LOST
    phases = {p.phase: p for p in await store.phases(attempt_id)}
    assert set(phases) == {p.value for p in AttemptPhase}
    assert all(phases[p].skipped for p in ("stage", "env", "run", "collect"))
    late = await store.apply_status(StatusReport(attempt_id, 1, AttemptState.DONE))
    assert late.kind is StatusKind.FENCED
    unknown = await store.apply_status(
        StatusReport(str(new_attempt_id()), 1, AttemptState.DONE)
    )
    assert unknown.kind is StatusKind.UNKNOWN


async def test_cancel_queued_now_and_running_via_agent(
    store: ExecutionStore, node_id: str
) -> None:
    queued = await _queued(store, node_id)
    now = await store.request_cancel(queued.id)
    assert now is not None and now.cancelled_now
    assert now.job.state is JobState.CANCELLED
    again = await store.request_cancel(queued.id)
    assert again is not None and not again.cancelled_now and again.attempt is None

    job_id, attempt_id = await _running_attempt(store, node_id)
    await store.apply_status(StatusReport(attempt_id, 1, AttemptState.RUNNING))
    via = await store.request_cancel(job_id)
    assert via is not None and not via.cancelled_now
    assert via.attempt is not None and via.attempt.id == attempt_id
    assert via.job.cancel_requested_at is not None
    ended = await store.apply_status(
        StatusReport(
            attempt_id,
            1,
            AttemptState.FAILED,
            failure_type=FailureType.CANCELLED,
            detail="cancelled",
        )
    )
    assert ended.job is not None and ended.job.state is JobState.CANCELLED
    assert await store.request_cancel(str(new_job_id())) is None


async def test_overdue_running_uses_the_job_timeout(
    store: ExecutionStore, node_id: str
) -> None:
    _job_id, attempt_id = await _running_attempt(store, node_id)
    await store.apply_status(StatusReport(attempt_id, 1, AttemptState.RUNNING))
    assert await store.overdue_running(30.0) == []
    # timeout 60 s: with a grace of -61 s it is "overdue" already
    (overdue,) = await store.overdue_running(-61.0)
    assert overdue.attempt.id == attempt_id
    live = await store.live_attempts(node_id)
    assert [a.attempt.id for a in live] == [attempt_id]
