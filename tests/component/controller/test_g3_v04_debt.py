"""V0.5 G3 (D94): the carried V0.4 debt on real PostgreSQL.

**UPLOADING backstop.**
An attempt that entered UPLOADING and never finished is failed ``output_error``
once ``upload_deadline_total_s`` has passed since the job's persisted
``COLLECTING`` transition - measured with an injected wall clock. Its upload
grant is revoked (a late ``PUT`` is refused), a late ``DONE`` is fenced and
released, and a controller restart (a new dispatcher, new in-memory grants)
keeps the same deadline because it is anchored on the database row.

**Log GC.** The dispatcher's sweep deletes the log files of attempts whose job is
terminal and unchanged for ``log_retention_s`` - the database decides; a live
attempt, a terminal attempt of a job that is not terminal, and a terminal job
inside the retention keep their files.
"""

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
from sqlalchemy import text

from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
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
from meshforge.controller.upload_grants import GrantRefusal, UploadGrants

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


async def test_stuck_uploading_fails_output_error_after_the_deadline_only(
    store: ExecutionStore, node_id: str
) -> None:
    job_id, attempt_id = await _attempt_in(store, node_id, AttemptState.UPLOADING)
    wall = Wall()
    dispatcher, manager, grants, _ = _dispatcher(store, wall)
    token = grants.mint(attempt_id, ttl_s=3600, max_blobs=3, max_bytes=1 << 20)
    since = await _uploading_since(store, job_id)

    wall.now = since + dt.timedelta(seconds=DEADLINE_S - 1)
    await dispatcher._upload_deadline()
    attempt = await store.get_attempt(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.UPLOADING
    assert dispatcher.stats.upload_deadlines == 0 and manager.commands == []

    wall.now = since + dt.timedelta(seconds=DEADLINE_S)
    await dispatcher._upload_deadline()
    attempt = await store.get_attempt(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.FAILED
    assert attempt.failure_type is FailureType.OUTPUT_ERROR
    assert attempt.detail == "upload_deadline_exceeded"
    job = await store.get_job(job_id)
    assert job is not None and job.state is JobState.FAILED
    assert job.failure_type is FailureType.OUTPUT_ERROR
    assert dispatcher.stats.upload_deadlines == 1
    ((node, name, payload),) = manager.commands
    assert (node, name) == (node_id, "cancel_attempt")
    assert payload.attempt_id == attempt_id
    assert payload.reason == "upload_deadline_exceeded"

    # The grant is revoked: a late upload is refused before a byte is read.
    with pytest.raises(GrantRefusal) as refused:
        grants.begin(token.get_secret_value(), Digest.parse(DIGEST), 10)
    assert refused.value.reason == "unknown"
    # A late DONE is acked and never makes the job SUCCEEDED: with outputs its
    # digest is not under a (revoked) grant -> a duplicate of the stored
    # FAILED(output_error); without outputs it contradicts the terminal attempt
    # -> fenced and released.
    live = SimpleNamespace(
        node_id=node_id, session_id="sess_x", attempt_status_fenced=0
    )
    with_outputs = _Status(
        live,  # type: ignore[arg-type]
        str(new_command_id()),
        StatusReport(attempt_id, 1, AttemptState.DONE, output_digest=DIGEST),
    )
    await dispatcher._handle(with_outputs)
    assert manager.acked == [with_outputs.message_id]
    assert dispatcher.stats.statuses_duplicate == 1
    bare = _Status(
        live,  # type: ignore[arg-type]
        str(new_command_id()),
        StatusReport(attempt_id, 1, AttemptState.DONE),
    )
    await dispatcher._handle(bare)
    assert manager.acked[-1] == bare.message_id
    assert dispatcher.stats.statuses_fenced == 1
    assert manager.commands[-1][2].release is True
    assert dispatcher.stats.statuses_applied == 0
    job = await store.get_job(job_id)
    assert job is not None and job.state is JobState.FAILED

    # Idempotent: the next tick does nothing more.
    await dispatcher._upload_deadline()
    assert dispatcher.stats.upload_deadlines == 1


async def test_a_controller_restart_keeps_the_deadline(
    store: ExecutionStore, node_id: str
) -> None:
    job_id, attempt_id = await _attempt_in(store, node_id, AttemptState.UPLOADING)
    since = await _uploading_since(store, job_id)
    wall = Wall()
    first, _, _, _ = _dispatcher(store, wall)
    wall.now = since + dt.timedelta(seconds=DEADLINE_S / 2)
    await first._upload_deadline()  # seen, not due

    # "Restart": a new dispatcher, new in-memory grants, no cache. The deadline
    # is still measured from the stored COLLECTING transition, not from now.
    second, manager, _, _ = _dispatcher(store, wall)
    wall.now = since + dt.timedelta(seconds=DEADLINE_S - 1)
    await second._upload_deadline()
    attempt = await store.get_attempt(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.UPLOADING
    wall.now = since + dt.timedelta(seconds=DEADLINE_S + 1)
    await second._upload_deadline()
    attempt = await store.get_attempt(attempt_id)
    assert attempt is not None and attempt.state is AttemptState.FAILED
    assert attempt.failure_type is FailureType.OUTPUT_ERROR
    assert second.stats.upload_deadlines == 1 and len(manager.commands) == 1


async def test_other_live_states_are_not_touched(
    store: ExecutionStore, node_id: str
) -> None:
    _, running = await _attempt_in(store, node_id, AttemptState.RUNNING)
    wall = Wall()
    dispatcher, manager, _, _ = _dispatcher(store, wall)
    wall.now = wall.now + dt.timedelta(days=30)
    await dispatcher._upload_deadline()
    attempt = await store.get_attempt(running)
    assert attempt is not None and attempt.state is AttemptState.RUNNING
    assert manager.commands == [] and dispatcher.stats.upload_deadlines == 0


async def test_the_derived_default_deadline() -> None:
    settings = JobApiSettings()
    assert settings.upload_deadline_total_s == pytest.approx(600 + 2048)
    pinned = JobApiSettings(upload_deadline_s=60)
    assert pinned.upload_deadline_total_s == 60
    with pytest.raises(ValueError, match="upload_deadline_s"):
        JobApiSettings(upload_deadline_s=59.9)
    with pytest.raises(ValueError, match="upload_min_rate_bytes_s"):
        JobApiSettings(upload_min_rate_bytes_s=64 * 1024 - 1)


# ------------------------------------------------------------------ log GC


def _log_files(hub: LogHub, attempt_id: str) -> list[Path]:
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(days=365)).timestamp()
    paths = [hub.path(attempt_id, s) for s in ("stdout", "stderr")]
    for path in paths:
        path.write_bytes(b"output")
        os.utime(path, (old, old))  # the mtime pre-filter never decides here
    return paths


async def test_log_gc_removes_only_terminal_job_logs_past_retention(
    store: ExecutionStore,
    node_id: str,
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=1 << 20)
    hub.prepare()
    wall = Wall()
    dispatcher, _, _, _ = _dispatcher(store, wall, hub)

    done_job, done = await _attempt_in(store, node_id, AttemptState.DONE)
    _, running = await _attempt_in(store, node_id, AttemptState.RUNNING)
    half_job, half = await _attempt_in(store, node_id, AttemptState.RUNNING)
    # A terminal attempt whose job is not terminal (a retry in V0.6 looks like
    # this): the attempt guard alone would delete it; the job guard keeps it.
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE attempt SET state = 'failed', ended_at = now() WHERE id = :i"
                ),
                {"i": half},
            )
    finally:
        await engine.dispose()
    files = {a: _log_files(hub, a) for a in (done, running, half)}
    done_record = await store.get_job(done_job)
    assert done_record is not None and done_record.state is JobState.SUCCEEDED
    half_record = await store.get_job(half_job)
    assert half_record is not None and half_record.state is JobState.RUNNING

    # Inside the retention: everything stays.
    wall.now = done_record.updated_at + dt.timedelta(seconds=RETENTION_S - 1)
    await dispatcher._log_gc()
    assert all(p.exists() for paths in files.values() for p in paths)

    # Past it: only the terminal job's files go.
    wall.now = done_record.updated_at + dt.timedelta(seconds=RETENTION_S + 1)
    await dispatcher._log_gc()
    assert not any(p.exists() for p in files[done])
    assert all(p.exists() for p in files[running])
    assert all(p.exists() for p in files[half])
    assert dispatcher.stats.log_files_deleted == 2


async def test_log_gc_runs_on_the_tick_interval(
    store: ExecutionStore, node_id: str, tmp_path: Path
) -> None:
    hub = LogHub(tmp_path / "logs", blobs=None, max_bytes=1 << 20)
    hub.prepare()
    _, done = await _attempt_in(store, node_id, AttemptState.DONE)
    paths = _log_files(hub, done)
    wall = Wall()
    wall.now += dt.timedelta(seconds=RETENTION_S + 60)
    mono = [0.0]
    dispatcher = Dispatcher(
        store,
        FakeManager(),  # type: ignore[arg-type]
        FetchTokens(clock=lambda: mono[0]),
        JobApiSettings(log_retention_s=RETENTION_S, log_gc_interval_s=60),
        logs=hub,
        clock=lambda: mono[0],
        blob_port=lambda: 1,
        wall_clock=wall,
    )
    await dispatcher._periodic(None)
    assert all(p.exists() for p in paths)  # not yet due
    mono[0] = 60.0
    await dispatcher._periodic(None)
    assert not any(p.exists() for p in paths)
