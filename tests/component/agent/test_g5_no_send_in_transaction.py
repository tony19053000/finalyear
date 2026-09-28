"""D70-5 / D77 guard: the controller never sends a command inside an open store
transaction.

A command sent while the transaction that justifies it is still open could reach
the agent before the commit - and a rollback would leave an attempt running that
the database has no row for (D65: commit **then** send). ``_decide`` runs
synchronously inside ``dispatch_queued``'s transaction, so the rule is easy to
break by moving one call.

The runtime's own :class:`ExecutionStore` session factory is wrapped so every
open database session marks the current asyncio task (a ``ContextVar``); the
manager's ``send_command`` records whether the task that calls it holds one. The
scenario drives every send path the dispatcher has: StartAttempt (dispatch),
CancelAttempt (the cancel route), a StartAttempt re-send after a Reconcile, and a
CancelAttempt for an attempt the controller does not know.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of

from meshforge.common.ids import new_attempt_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

_OPEN: contextvars.ContextVar[int] = contextvars.ContextVar("open_db", default=0)


class _Tracked:
    """``async with factory() as db`` that marks the task while it is open."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._token: contextvars.Token[int] | None = None

    async def __aenter__(self) -> Any:
        db = await self._inner.__aenter__()
        self._token = _OPEN.set(_OPEN.get() + 1)
        return db

    async def __aexit__(self, *exc: object) -> Any:
        if self._token is not None:
            _OPEN.reset(self._token)
        return await self._inner.__aexit__(*exc)


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(pg_settings, jobs={"dispatch_interval_s": 0.1})


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


async def test_no_command_is_sent_while_a_store_session_is_open(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    grpc_target: str,
    data_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, dispatcher = runtime.manager, runtime.dispatcher
    assert manager is not None and dispatcher is not None
    factory = runtime.jobs._sessions
    monkeypatch.setattr(runtime.jobs, "_sessions", lambda: _Tracked(factory()))
    sends: list[tuple[str, int]] = []
    real_send = manager.send_command

    def recording_send(node_id: str, payload_name: str, payload: Any, **kw: Any) -> Any:
        sends.append((payload_name, _OPEN.get()))
        return real_send(node_id, payload_name, payload, **kw)

    monkeypatch.setattr(manager, "send_command", recording_send)
    # One StartAttempt lost "between commit and send" so the Reconcile re-sends it.
    real_start = dispatcher._send_start
    lost: list[str] = []

    def lose_first(job: Any, attempt: Any, *, resend: bool) -> None:
        if not resend and not lost:
            lost.append(attempt.id)
            return
        real_start(job, attempt, resend=resend)

    monkeypatch.setattr(dispatcher, "_send_start", lose_first)

    agent = await ExecAgent.start(grpc_target, data_dir)
    try:
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text("import time\ntime.sleep(60)\n")
        manifest = {"name": "guard", "entrypoint": ["python", "main.py"]}
        job_id = await submit(http, project, agent.node_id, manifest)
        await wait_job(http, job_id, states=frozenset({"preparing"}))
        for _ in range(100):
            if lost:
                break
            await asyncio.sleep(0.05)
        assert lost
        session = manager.get(agent.node_id)
        assert session is not None
        await manager.end_session(session, SessionEndReason.STREAM_LOST)
        await wait_job(http, job_id, states=frozenset({"running"}))
        # An attempt the controller does not know, held by the agent: the
        # Reconcile answer is a CancelAttempt (sent from the dispatcher task).
        (real,) = await runtime.jobs.live_attempts(agent.node_id)
        fake = agent_pb2.Reconcile()
        fake.running_attempts.add(
            attempt_id=real.attempt.id, fencing_token=real.attempt.fencing_token
        )
        fake.running_attempts.add(attempt_id=str(new_attempt_id()), fencing_token=1)
        live = manager.get(agent.node_id)
        assert live is not None
        manager.commands.on_reconcile(live, fake)
        resp = await http.post(f"/v1/jobs/{job_id}/cancel")
        assert resp.status_code == 202, resp.text
        job = await wait_job(http, job_id, timeout_s=15)
        assert job["state"] == "cancelled", job
    finally:
        await agent.stop()
    names = [name for name, _ in sends]
    assert names.count("start_attempt") >= 1, names  # the re-send
    assert names.count("cancel_attempt") >= 2, names  # unknown + requested
    inside = [(name, depth) for name, depth in sends if depth]
    assert not inside, f"commands sent inside an open store session: {inside}"
