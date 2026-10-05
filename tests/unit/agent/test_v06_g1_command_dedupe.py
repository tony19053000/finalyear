"""V0.6 G1 (D113): every controller command has exactly one effect.

Two layers (ARCHITECTURE §13 audit table):

1. the session client drops a **repeated** ``message_id`` (a controller re-send
   after a lost Ack) for every command payload - the handler runs once, the
   Ack is written each time (the controller may have missed the first);
2. each command's effect is idempotent by its own key when it arrives again
   in a **new** envelope (a Reconcile-driven re-send, or a new agent process):
   ``CancelAttempt`` by attempt (one terminal report, a release frees once),
   ``RefreshFetchToken`` by ``(attempt_id, fencing_token)`` (newest applied),
   ``GcRequest{set_pins}`` newest wins even when two requests overlap.
   ``StartAttempt`` (``test_executor.py::test_a_duplicate_start_while_running_runs_once``
   and the finished-attempt case in ``test_state_order_and_release_after_ack``)
   and ``PrepareEnv`` (``test_node_prepare.py``,
   ``test_env_built_then_hit_and_a_replayed_request_runs_once``) are pinned
   where they were built.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_executor import FakeRuntime, Reports, _executor, _settle, _start
from test_gc import Rig
from test_session_client_unit import _online, client  # noqa: F401 - fixture

from meshforge.agent.gc.service import GcService
from meshforge.agent.session.client import AgentSessionClient
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

COMMANDS: dict[str, Any] = {
    "start_attempt": agent_pb2.StartAttempt(attempt_id="a", fencing_token=1),
    "cancel_attempt": agent_pb2.CancelAttempt(attempt_id="a", fencing_token=1),
    "refresh_fetch_token": agent_pb2.RefreshFetchToken(attempt_id="a", fencing_token=1),
    "refresh_upload_grant": agent_pb2.RefreshUploadGrant(
        attempt_id="a", fencing_token=1
    ),
    "gc": agent_pb2.GcRequest(run_now=True),
    "prepare_env": agent_pb2.PrepareEnv(request_id="r"),
}


@pytest.mark.parametrize("payload", sorted(COMMANDS))
async def test_a_repeated_message_id_reaches_the_handler_once(
    client: AgentSessionClient,  # noqa: F811 - the imported fixture
    payload: str,
) -> None:
    call = _online(client)
    seen: list[str] = []
    client.command_handler = lambda msg: seen.append(msg.WhichOneof("payload"))
    msg = agent_pb2.ControllerMessage(
        message_id=new_command_id(), session_id=client.session_id
    )
    getattr(msg, payload).CopyFrom(COMMANDS[payload])
    for _ in range(3):  # the controller re-sent it twice (lost Acks)
        await client._handle(msg)
    assert seen == [payload]
    acks = [m.ack.acked_message_id for m in call.written if m.HasField("ack")]
    assert acks == [msg.message_id] * 3
    assert client.stats.duplicates == 2


def _cancel(attempt_id: str, *, release: bool = False) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        message_id=new_command_id(),
        cancel_attempt=agent_pb2.CancelAttempt(
            attempt_id=attempt_id, fencing_token=1, reason="test", release=release
        ),
    )


async def test_a_cancel_in_new_envelopes_stops_once_and_reports_once(
    tmp_path: Path,
) -> None:
    runtime, reports = FakeRuntime(hang=True), Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    start = _start(digest)
    attempt_id = start.start_attempt.attempt_id
    executor.handle(start)
    for _ in range(200):
        if runtime.calls:
            break
        await asyncio.sleep(0.01)
    for _ in range(3):
        executor.handle(_cancel(attempt_id))
    await _settle(executor)
    finals = [
        s
        for _, s in reports.sent
        if attempt_state_from_proto(s.state).value == "failed"
    ]
    assert len(finals) == 1
    assert failure_type_from_proto(finals[0].failure.failure_type).value == "cancelled"
    # a release twice frees once (the second finds nothing to free)
    for _ in range(2):
        executor.handle(_cancel(attempt_id, release=True))
    await asyncio.sleep(0)
    assert executor.held() == []
    await executor.close()


async def test_a_refresh_in_new_envelopes_applies_the_newest(tmp_path: Path) -> None:
    runtime, reports = FakeRuntime(hang=True), Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    start = _start(digest)
    attempt_id = start.start_attempt.attempt_id
    executor.handle(start)
    for _ in range(200):
        if runtime.calls:
            break
        await asyncio.sleep(0.01)
    for token in ("tok-1", "tok-2", "tok-2"):
        executor.handle(
            agent_pb2.ControllerMessage(
                message_id=new_command_id(),
                refresh_fetch_token=agent_pb2.RefreshFetchToken(
                    attempt_id=attempt_id,
                    fencing_token=1,
                    fetch_token=token,
                    blob_url="http://127.0.0.1:9",
                    blob_port=9,
                ),
            )
        )
    attempt = executor._attempts[attempt_id]
    assert attempt.fetch_token == "tok-2"  # the last one sent
    # another fencing token never touches it
    executor.handle(
        agent_pb2.ControllerMessage(
            message_id=new_command_id(),
            refresh_fetch_token=agent_pb2.RefreshFetchToken(
                attempt_id=attempt_id,
                fencing_token=2,
                fetch_token="zombie",
                blob_url="http://127.0.0.1:9",
                blob_port=9,
            ),
        )
    )
    assert attempt.fetch_token == "tok-2"
    assert runtime.calls == 1  # nothing re-ran
    await executor.close()


async def test_an_upload_grant_refresh_in_new_envelopes_applies_the_newest(
    tmp_path: Path,
) -> None:
    """V0.6 D114: RefreshUploadGrant re-sent in new envelopes (each Reconcile
    mints a new one) -> the last applies; another fencing token never does."""
    runtime, reports = FakeRuntime(hang=True), Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    start = _start(digest)
    attempt_id = start.start_attempt.attempt_id
    executor.handle(start)
    for _ in range(200):
        if runtime.calls:
            break
        await asyncio.sleep(0.01)

    def refresh(token: str, fencing: int = 1) -> agent_pb2.ControllerMessage:
        return agent_pb2.ControllerMessage(
            message_id=new_command_id(),
            refresh_upload_grant=agent_pb2.RefreshUploadGrant(
                attempt_id=attempt_id,
                fencing_token=fencing,
                upload_token=token,
                blob_url="http://127.0.0.1:9",
                blob_port=9,
            ),
        )

    for token in ("grant-1", "grant-2", "grant-2"):
        executor.handle(refresh(token))
    attempt = executor._attempts[attempt_id]
    assert attempt.upload_token == "grant-2"
    executor.handle(refresh("zombie", fencing=2))
    assert attempt.upload_token == "grant-2"
    assert runtime.calls == 1
    await executor.close()


async def test_overlapping_pin_requests_newest_wins(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    service = GcService(rig.gc, interval_s=3600)
    first = ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
    second = ["sha256:" + "c" * 64]
    service.handle(agent_pb2.GcRequest(set_pins=True, pinned_trees=first))
    service.handle(agent_pb2.GcRequest(set_pins=True, pinned_trees=second))
    await asyncio.gather(*list(service._pin_tasks))
    assert rig.index.pins("tree") == {"c" * 64}
    assert service.pins_superseded == 1
    # the same request again (a new envelope) changes nothing
    service.handle(agent_pb2.GcRequest(set_pins=True, pinned_trees=second))
    await asyncio.gather(*list(service._pin_tasks))
    assert rig.index.pins("tree") == {"c" * 64}
    rig.index.close()
