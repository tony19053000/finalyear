"""V0.7 G3 deliverable 6 (D138) without a database: the session manager's
revocation half and :class:`~meshforge.controller.revocation.Revocations`.

* ``revoke_node`` applies ``REVOKE`` (the node-loss hook fires: attempts are
  lost) before it cuts the stream with ``revoked``, ends a linger, and is a
  no-op for a node without a session;
* a Hello is refused (``PERMISSION_DENIED`` / ``node_revoked``) when the
  *database* says revoked, whatever the in-memory state;
* every accepted ``HelloAck`` carries the revocation set;
* ``Revocations.sync`` kills only revoked nodes that have a session,
  broadcasts only when the serial set changed, and ``run`` survives a
  database error with the last set kept.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from collections.abc import Mapping
from typing import Any, cast

import grpc
import pytest

from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, LivenessEvent, NodeState
from meshforge.controller.registry import NodeRecord, Registry, SessionEndReason
from meshforge.controller.revocation import (
    RevocationError,
    Revocations,
    RevocationSnapshot,
)
from meshforge.controller.session.manager import (
    Accepted,
    LiveSession,
    Rejected,
    SessionManager,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class Registry_:  # noqa: N801 - a fake of Registry
    def __init__(self) -> None:
        self.nodes: dict[str, NodeRecord] = {}
        self.closed: list[tuple[str, SessionEndReason]] = []
        self.states: list[tuple[str, NodeState]] = []

    async def get_node(self, node_id: str) -> NodeRecord | None:
        return self.nodes.get(node_id)

    async def upsert_node_on_hello(self, **kw: Any) -> NodeRecord:
        now = dt.datetime.now(dt.UTC)
        rec = NodeRecord(
            node_id=kw["node_id"],
            hostname=kw["hostname"],
            os=kw["os"],
            platform=kw["platform"],
            hardware=dict(cast(Mapping[str, Any], kw["hardware"])),
            state=kw["state"],
            first_seen_at=now,
            last_seen_at=now,
            last_session_id=kw["session_id"],
            session_epoch=kw["session_epoch"],
        )
        self.nodes[rec.node_id] = rec
        return rec

    async def open_session(self, **kw: Any) -> None:
        return None

    async def set_node_state(
        self, node_id: str, state: NodeState, *, session_id: str
    ) -> bool:
        self.states.append((node_id, state))
        return True

    async def close_session(self, session_id: str, reason: SessionEndReason) -> bool:
        self.closed.append((session_id, reason))
        return True

    async def touch_node(self, node_id: str) -> None:
        return None

    async def record_rtt(self, session_id: str, **kw: Any) -> None:
        return None


def _manager() -> tuple[SessionManager, Registry_]:
    reg = Registry_()
    manager = SessionManager(
        cast(Registry, reg), LivenessConfig(), clock=lambda: 1000.0
    )
    return manager, reg


def _hello(node_id: str, epoch: int) -> agent_pb2.Hello:
    return agent_pb2.Hello(
        node_id=node_id,
        protocol_version=1,
        agent_version="0.1.0.dev0",
        hostname="laptop",
        session_epoch=epoch,
        os="Ubuntu 24.04",
        platform="linux-x86_64",
    )


async def _admit(manager: SessionManager, node_id: str, epoch: int = 1) -> Any:
    return await manager.admit(_hello(node_id, epoch), "ipv4:127.0.0.1:1")


async def test_revoke_node_loses_attempts_first_then_cuts_the_stream() -> None:
    manager, reg = _manager()
    gone: list[tuple[str, LivenessEvent]] = []
    manager.hooks.on_node_offline = lambda live, event: gone.append(
        (live.node_id, event)
    )
    node = str(new_node_id())
    accepted = await _admit(manager, node)
    assert isinstance(accepted, Accepted)
    live: LiveSession = accepted.live
    assert await manager.revoke_node(node) is True
    assert live.ended and live.end_reason is SessionEndReason.REVOKED
    assert live.state is NodeState.REVOKED
    assert gone == [(node, LivenessEvent.REVOKE)]  # the node-loss path, once
    assert manager.get(node) is None and not manager.has_session(node)
    assert await manager.revoke_node(node) is False  # nothing left to cut
    assert await manager.revoke_node(str(new_node_id())) is False
    for _ in range(20):
        await asyncio.sleep(0)
    assert (live.session_id, SessionEndReason.REVOKED) in reg.closed


async def test_a_lingering_session_is_ended_and_its_attempts_lost() -> None:
    manager, _reg = _manager()
    gone: list[LivenessEvent] = []
    manager.hooks.on_node_offline = lambda _live, event: gone.append(event)
    node = str(new_node_id())
    accepted = await _admit(manager, node)
    assert isinstance(accepted, Accepted)
    await manager.end_session(accepted.live, SessionEndReason.STREAM_LOST)
    assert manager.get_lingering(node) is not None and manager.has_session(node)
    assert await manager.revoke_node(node) is True
    assert manager.get_lingering(node) is None
    assert gone == [LivenessEvent.REVOKE]


async def test_a_hello_is_refused_when_the_database_says_revoked() -> None:
    manager, reg = _manager()
    node = str(new_node_id())
    first = await _admit(manager, node)
    assert isinstance(first, Accepted)
    await manager.end_session(first.live, SessionEndReason.AGENT_CLOSED)
    for _ in range(20):
        await asyncio.sleep(0)
    # The controller CLI wrote "revoked" straight into the database.
    reg.nodes[node] = dataclasses.replace(reg.nodes[node], state=NodeState.REVOKED)
    refused = await _admit(manager, node, epoch=2)
    assert isinstance(refused, Rejected)
    assert refused.abort_status is grpc.StatusCode.PERMISSION_DENIED
    assert refused.reason_code == "node_revoked"
    assert "invite --replace" in refused.reason


async def test_every_hello_ack_carries_the_revocation_set() -> None:
    manager, _reg = _manager()
    manager.revoked_serials = lambda: ("abc", "def")
    accepted = await _admit(manager, str(new_node_id()))
    assert isinstance(accepted, Accepted)
    assert list(accepted.hello_ack.hello_ack.revoked_serials) == ["abc", "def"]


# ---------------------------------------------------------------- Revocations


class Store:
    def __init__(self) -> None:
        self.snap = RevocationSnapshot(frozenset(), frozenset(), ())
        self.fail = False

    async def snapshot(self) -> RevocationSnapshot:
        if self.fail:
            raise RevocationError("down", details={"cause": "OSError"})
        return self.snap


class Sessions:
    def __init__(self, live: set[str]) -> None:
        self.live = live
        self.cut: list[str] = []
        self.sent: list[tuple[str, Any]] = []

    def has_session(self, node_id: str) -> bool:
        return node_id in self.live

    async def revoke_node(self, node_id: str) -> bool:
        self.cut.append(node_id)
        self.live.discard(node_id)
        return True

    def broadcast(self, payload_name: str, payload: Any) -> int:
        self.sent.append((payload_name, payload))
        return 1


async def test_sync_kills_connected_revoked_nodes_and_broadcasts_changes() -> None:
    store, sessions = Store(), Sessions({"n1", "n2"})
    revocations = Revocations(store, sessions)  # type: ignore[arg-type]
    assert await revocations.sync() == frozenset()
    assert sessions.sent == [] and sessions.cut == []
    store.snap = RevocationSnapshot(
        frozenset({"n1", "n3"}), frozenset({"aa", "bb"}), ("bb", "aa")
    )
    assert await revocations.sync() == frozenset({"n1"})
    assert sessions.cut == ["n1"]  # n3 has no session
    ((name, update),) = sessions.sent
    assert name == "revocation_update" and list(update.serials) == ["bb", "aa"]
    assert revocations.serials == {"aa", "bb"}
    assert revocations.sent_serials() == ("bb", "aa")
    assert await revocations.sync() == frozenset()  # nothing new: no broadcast
    assert len(sessions.sent) == 1


async def test_run_keeps_the_last_set_when_the_database_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, sessions = Store(), Sessions(set())
    store.snap = RevocationSnapshot(frozenset(), frozenset({"aa"}), ("aa",))
    revocations = Revocations(store, sessions)  # type: ignore[arg-type]
    await revocations.sync()
    store.fail = True
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await revocations.run(0.5)
    assert sleeps == [0.5, 0.5, 0.5]
    assert revocations.sync_failures == 3
    assert revocations.serials == {"aa"}
