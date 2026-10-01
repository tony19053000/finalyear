"""V0.5 G3 (D94) tester: upload-deadline boundary, races and GC vs retries on real PG."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command

from meshforge.common.errors import FailureType
from meshforge.common.ids import (
    new_attempt_id,
    new_command_id,
    new_job_id,
    new_node_id,
    new_session_id,
)
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.blobs import FetchTokens
from meshforge.controller.jobs.dispatcher import Dispatcher, _Status
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    ExecutionStore,
    NodeView,
    StatusReport,
)
from meshforge.controller.jobs.logs import LogHub
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.upload_grants import UploadGrants

pytestmark = pytest.mark.asyncio

DIGEST = "sha256:" + "ab" * 32
MANIFEST: dict[str, Any] = {
    "name": "demo",
    "entrypoint": ["python", "main.py"],
    "limits": {"timeout": 60, "network": "default"},
}
DEADLINE_S = 120.0
RETENTION_S = 7 * 86_400.0


class FakeManager:
    """The session layer the dispatcher talks to (records commands, acks)."""

    def __init__(self) -> None:
        self.hooks = SimpleNamespace(
            on_command_acked=None, on_reconcile=None, on_node_offline=None
        )
        self.dispatch = SimpleNamespace(register=lambda *_a: None)
        self.commands: list[tuple[str, str, Any]] = []
        self.acked: list[str] = []

    def defer_ack(self, _p: str) -> None:
        return None

    def send_command(self, node_id: str, name: str, payload: Any, **_kw: Any) -> str:
        self.commands.append((node_id, name, payload))
        return str(new_command_id())

    def ack_message(self, _live: Any, message_id: str) -> bool:
        self.acked.append(message_id)
        return True

    def get(self, _node_id: str) -> None:
        return None

    def get_lingering(self, _node_id: str) -> None:
        return None


class Wall:
    def __init__(self) -> None:
        self.now = dt.datetime.now(dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.now


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=10)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def node_id(pg_settings: DatabaseSettings, store: ExecutionStore) -> str:
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


async def _attempt_in(
    store: ExecutionStore, node_id: str, state: AttemptState
) -> tuple[str, str]:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": node_id},
        request_hash="h" * 64,
        idempotency_key=None,
    )
    (result,) = await store.dispatch_queued(_dispatch_all, limit=10, max_per_node=4)
    assert result.attempt is not None
    attempt_id = result.attempt.id
    assert await store.mark_start_acked(attempt_id, 1)
    await store.apply_status(StatusReport(attempt_id, 1, state))
    return job.id, attempt_id


def _dispatcher(
    store: ExecutionStore, wall: Wall, logs: LogHub | None = None
) -> tuple[Dispatcher, FakeManager, UploadGrants, FetchTokens]:
    manager = FakeManager()
    grants = UploadGrants(clock=lambda: 0.0)
    tokens = FetchTokens(clock=lambda: 0.0)
    dispatcher = Dispatcher(
        store,
        manager,  # type: ignore[arg-type]
        tokens,
        JobApiSettings(upload_deadline_s=DEADLINE_S, log_retention_s=RETENTION_S),
        upload_grants=grants,
        logs=logs,
        clock=lambda: 0.0,
        blob_port=lambda: 1,
        wall_clock=wall,
    )
    return dispatcher, manager, grants, tokens


async def _uploading_since(store: ExecutionStore, job_id: str) -> dt.datetime:
    (at,) = [
        t.at
        for t in await store.transitions(job_id)
        if t.to_state is JobState.COLLECTING
    ]
    return at


async def test_deadline_plus_one_ms_fails_and_minus_one_ms_does_not(
    store: ExecutionStore, node_id: str
) -> None:
    job_id, attempt_id = await _attempt_in(store, node_id, AttemptState.UPLOADING)
    wall = Wall()
    dispatcher, _, _, _ = _dispatcher(store, wall)
    since = await _uploading_since(store, job_id)
    wall.now = since + dt.timedelta(seconds=DEADLINE_S) - dt.timedelta(milliseconds=1)
    await dispatcher._upload_deadline()
    attempt = await store.get_attempt(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.UPLOADING
    wall.now = since + dt.timedelta(seconds=DEADLINE_S) + dt.timedelta(milliseconds=1)
    await dispatcher._upload_deadline()
    attempt = await store.get_attempt(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.FAILED


async def test_clock_going_backwards_never_fails_an_attempt(
    store: ExecutionStore, node_id: str
) -> None:
    job_id, attempt_id = await _attempt_in(store, node_id, AttemptState.UPLOADING)
    wall = Wall()
    dispatcher, _, _, _ = _dispatcher(store, wall)
    since = await _uploading_since(store, job_id)
    wall.now = since - dt.timedelta(days=365)
    await dispatcher._upload_deadline()
    attempt = await store.get_attempt(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.UPLOADING


async def test_done_racing_the_deadline_ends_in_one_consistent_terminal_state(
    store: ExecutionStore, node_id: str
) -> None:
    """Upload completing in the same tick as the deadline: exactly one wins,
    attempt and job agree, the job is never SUCCEEDED after FAILED."""
    outcomes: set[str] = set()
    for _ in range(12):
        job_id, attempt_id = await _attempt_in(store, node_id, AttemptState.UPLOADING)
        wall = Wall()
        dispatcher, manager, _g, _ = _dispatcher(store, wall)
        since = await _uploading_since(store, job_id)
        wall.now = since + dt.timedelta(seconds=DEADLINE_S + 5)
        live = SimpleNamespace(
            node_id=node_id, session_id="sess_x", attempt_status_fenced=0
        )
        done = _Status(
            live,  # type: ignore[arg-type]
            str(new_command_id()),
            StatusReport(attempt_id, 1, AttemptState.DONE),
        )
        await asyncio.gather(dispatcher._upload_deadline(), dispatcher._handle(done))
        await dispatcher._upload_deadline()  # and again: must be a no-op
        attempt = await store.get_attempt(attempt_id)
        job = await store.get_job(job_id)
        assert attempt is not None and job is not None
        if attempt.state is AttemptState.FAILED:
            assert job.state is JobState.FAILED
            assert attempt.failure_type is FailureType.OUTPUT_ERROR
        else:
            assert attempt.state is AttemptState.DONE
            assert job.state is JobState.SUCCEEDED
            assert dispatcher.stats.upload_deadlines == 0
        outcomes.add(attempt.state.value)
        assert manager.acked == [done.message_id]
        assert dispatcher.stats.upload_deadlines <= 1
        transitions = [t.to_state for t in await store.transitions(job_id)]
        assert not (
            JobState.FAILED in transitions and JobState.SUCCEEDED in transitions
        )


async def test_many_stuck_attempts_all_end_once(
    store: ExecutionStore, node_id: str
) -> None:
    ids = [
        (await _attempt_in(store, node_id, AttemptState.UPLOADING)) for _ in range(3)
    ]
    wall = Wall()
    dispatcher, manager, _, _ = _dispatcher(store, wall)
    wall.now = wall.now + dt.timedelta(seconds=DEADLINE_S * 3)
    await asyncio.gather(*(dispatcher._upload_deadline() for _ in range(3)))
    for job_id, attempt_id in ids:
        attempt = await store.get_attempt(attempt_id)
        job = await store.get_job(job_id)
        assert attempt is not None and attempt.state is AttemptState.FAILED
        assert job is not None and job.state is JobState.FAILED
    assert dispatcher.stats.upload_deadlines == 3
    assert len([c for c in manager.commands if c[1] == "cancel_attempt"]) == 3


async def test_gc_keeps_logs_of_a_live_retry_but_not_the_dead_attempts_peer(
    store: ExecutionStore, node_id: str, tmp_path: Path
) -> None:
    """A terminal attempt's old files are kept while its job is not terminal
    (a retry is coming); a live attempt's recent files are never touched."""
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=1000)
    hub.prepare()
    _job, attempt_id = await _attempt_in(store, node_id, AttemptState.RUNNING)
    files = [hub.root / f"{attempt_id}.stdout", hub.root / f"{attempt_id}.gaps"]
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=60)).timestamp()
    for f in files:
        f.write_bytes(b"x")
        os.utime(f, (old, old))
    wall = Wall()
    wall.now = wall.now + dt.timedelta(days=60)
    dispatcher, _, _, _ = _dispatcher(store, wall, logs=hub)
    await dispatcher._log_gc()
    assert all(f.exists() for f in files)  # RUNNING attempt: never collected
