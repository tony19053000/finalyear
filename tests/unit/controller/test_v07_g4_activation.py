"""V0.7 G4 deliverable 1 (D140): session admission activates a renewed
certificate - without a database.

* a Hello on the node's **pending** certificate asks the renewal store to make
  it current (and the runtime is told, to re-sync the revocation set);
* a Hello on any other certificate than the current or pending one is refused
  ``cert_superseded`` and never reaches the store;
* if the store says the serial is no longer pending (a newer renewal replaced
  it meanwhile), the Hello is refused too.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, cast

import pytest

from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, NodeState
from meshforge.controller.registry import NodeRecord, Registry, SessionEndReason
from meshforge.controller.session.manager import Accepted, Rejected, SessionManager
from meshforge.controller.session.peer_cert import PeerCert

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

LATER = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)


class Registry_:  # noqa: N801 - a fake of Registry
    def __init__(self) -> None:
        self.nodes: dict[str, NodeRecord] = {}

    async def get_node(self, node_id: str) -> NodeRecord | None:
        return self.nodes.get(node_id)

    async def upsert_node_on_hello(self, **kw: Any) -> NodeRecord:
        now = dt.datetime.now(dt.UTC)
        old = self.nodes.get(kw["node_id"])
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
            cert_serial=old.cert_serial if old else None,
            pending_cert_serial=old.pending_cert_serial if old else None,
        )
        self.nodes[rec.node_id] = rec
        return rec

    async def open_session(self, **kw: Any) -> None:
        return None

    async def set_node_state(self, node_id: str, state: NodeState, **kw: Any) -> bool:
        return True

    async def close_session(self, session_id: str, reason: SessionEndReason) -> bool:
        return True

    async def touch_node(self, node_id: str) -> None:
        return None

    async def record_rtt(self, session_id: str, **kw: Any) -> None:
        return None


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


def _cert(node_id: str, serial: str) -> PeerCert:
    return PeerCert(
        node_id=node_id,
        serial=serial,
        fingerprint="a" * 64,
        not_after=LATER,
        public_key_fingerprint="b" * 64,
    )


@pytest.fixture
def setup() -> tuple[SessionManager, Registry_, list[tuple[str, str]], list[str]]:
    reg = Registry_()
    manager = SessionManager(
        cast(Registry, reg), LivenessConfig(), clock=lambda: 1000.0
    )
    calls: list[tuple[str, str]] = []
    told: list[str] = []
    answer: dict[str, str | None] = {"value": "aa"}

    async def activate(node_id: str, serial: str) -> str | None:
        calls.append((node_id, serial))
        return answer["value"]

    manager.activate_cert = activate
    manager.on_cert_activated = told.append
    manager._test_answer = answer  # type: ignore[attr-defined]
    return manager, reg, calls, told


def _record(reg: Registry_, node_id: str, *, current: str, pending: str | None) -> None:
    now = dt.datetime.now(dt.UTC)
    reg.nodes[node_id] = NodeRecord(
        node_id=node_id,
        hostname="h",
        os="l",
        platform="p",
        hardware={},
        state=NodeState.OFFLINE,
        first_seen_at=now,
        last_seen_at=now,
        last_session_id=None,
        session_epoch=1,
        cert_serial=current,
        pending_cert_serial=pending,
    )


def _cancel_timers(manager: SessionManager) -> None:
    for live in manager.snapshot():
        if live.cert_expiry is not None:
            live.cert_expiry.cancel()


async def test_a_hello_on_the_pending_certificate_activates_it(setup: Any) -> None:
    manager, reg, calls, told = setup
    node = str(new_node_id())
    _record(reg, node, current="aa", pending="bb")
    result = await manager.admit(
        _hello(node, 2), "ipv4:127.0.0.1:1", cert=_cert(node, "bb")
    )
    assert isinstance(result, Accepted)
    assert calls == [(node, "bb")] and told == [node]
    _cancel_timers(manager)


async def test_the_current_certificate_needs_no_activation(setup: Any) -> None:
    manager, reg, calls, told = setup
    node = str(new_node_id())
    _record(reg, node, current="aa", pending="bb")
    result = await manager.admit(
        _hello(node, 2), "ipv4:127.0.0.1:1", cert=_cert(node, "aa")
    )
    assert isinstance(result, Accepted)
    assert calls == [] and told == []
    _cancel_timers(manager)


async def test_another_certificate_is_superseded_and_never_activated(
    setup: Any,
) -> None:
    manager, reg, calls, told = setup
    node = str(new_node_id())
    _record(reg, node, current="aa", pending="bb")
    result = await manager.admit(
        _hello(node, 2), "ipv4:127.0.0.1:1", cert=_cert(node, "cc")
    )
    assert isinstance(result, Rejected)
    assert result.reason_code == "cert_superseded"
    assert calls == [] and told == []
    assert manager.get(node) is None


async def test_a_pending_certificate_replaced_meanwhile_is_refused(setup: Any) -> None:
    manager, reg, calls, told = setup
    manager._test_answer["value"] = None  # the store: no longer pending
    node = str(new_node_id())
    _record(reg, node, current="aa", pending="bb")
    result = await manager.admit(
        _hello(node, 2), "ipv4:127.0.0.1:1", cert=_cert(node, "bb")
    )
    assert isinstance(result, Rejected) and result.reason_code == "cert_superseded"
    assert calls == [(node, "bb")] and told == []


async def test_without_renewal_a_pending_serial_is_not_trusted(setup: Any) -> None:
    manager, reg, _calls, _told = setup
    manager.activate_cert = None
    node = str(new_node_id())
    _record(reg, node, current="aa", pending="bb")
    result = await manager.admit(
        _hello(node, 2), "ipv4:127.0.0.1:1", cert=_cert(node, "bb")
    )
    assert isinstance(result, Rejected)
