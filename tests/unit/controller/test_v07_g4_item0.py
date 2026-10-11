"""V0.7 G4 item 0 (D156, reviewer non-blocking findings of G3) without a database.

* (a) a committed revocation never turns into an error because the immediate
  sync failed: ``Revocations.revoke`` returns ``enforced=False`` (REST says
  ``enforcement: deferred``), counts it, and the periodic sync enforces it;
* (b) the in-memory set carries each serial's reason: a superseded serial is
  refused ``cert_superseded`` (PERMISSION_DENIED), a revoked one
  ``cert_revoked``;
* (c) the cut scan runs only when the serial set changed - and admission
  re-checks the presented serial against the current set, so skipping the scan
  cannot let a listed serial in;
* (d) the certificate-expiry timer runs on the monotonic clock; when it fires
  while the certificate is still valid by the wall clock (the clock was set
  back) it re-arms instead of cutting, and cuts once ``not_after`` is reached.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Mapping
from typing import Any, cast

import grpc
import pytest

from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, NodeState
from meshforge.controller.registry import NodeRecord, Registry, SessionEndReason
from meshforge.controller.revocation import (
    RevocationError,
    Revocations,
    RevocationSnapshot,
    RevokeOutcome,
)
from meshforge.controller.session.manager import Accepted, Rejected, SessionManager
from meshforge.controller.session.peer_cert import (
    PeerCert,
    PeerCertError,
    PeerCertReason,
    refusal_for,
    status_for,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ------------------------------------------------------------------ doubles


class Store:
    def __init__(self) -> None:
        self.snap = RevocationSnapshot(frozenset(), frozenset(), ())
        self.fail_snapshot = False
        self.revoked: list[str] = []

    async def revoke_node(
        self, node_id: str, *, actor: str, source: str | None = None
    ) -> RevokeOutcome:
        self.revoked.append(node_id)  # "committed"
        return RevokeOutcome(node_id, "abc", False, "online")

    async def snapshot(self) -> RevocationSnapshot:
        if self.fail_snapshot:
            raise RevocationError("down", details={"cause": "OSError"})
        return self.snap


class Sessions:
    def __init__(self) -> None:
        self.scans: list[frozenset[str]] = []
        self.sent: list[Any] = []
        self.live: set[str] = set()
        self.boom = False

    def has_session(self, node_id: str) -> bool:
        return node_id in self.live

    async def revoke_node(self, node_id: str) -> bool:
        if self.boom:
            raise RuntimeError("cut failed")
        self.live.discard(node_id)
        return True

    async def cut_revoked_certs(self, serials: Any) -> frozenset[str]:
        self.scans.append(frozenset(serials))
        return frozenset()

    def broadcast(self, payload_name: str, payload: Any) -> int:
        self.sent.append((payload_name, payload))
        return 0


def _revocations() -> tuple[Revocations, Store, Sessions]:
    store, sessions = Store(), Sessions()
    return Revocations(store, sessions), store, sessions  # type: ignore[arg-type]


# --------------------------------------------------------------------- (a)


async def test_a_committed_revoke_survives_a_failed_sync_and_says_deferred() -> None:
    revocations, store, _sessions = _revocations()
    store.fail_snapshot = True
    outcome = await revocations.revoke(str(new_node_id()), actor="t")
    assert store.revoked  # the commit happened
    assert outcome.enforced is False
    assert outcome.as_dict()["enforcement"] == "deferred"
    assert outcome.session_killed is False
    assert revocations.deferred_enforcements == 1
    assert revocations.sync_failures == 1
    # The periodic path enforces it once the database answers again.
    store.fail_snapshot = False
    store.snap = RevocationSnapshot(frozenset(), frozenset({"abc"}), ("abc",))
    await revocations.sync()
    assert "abc" in revocations.revoked_serials()


async def test_a_failing_live_cut_after_the_commit_is_deferred_not_raised() -> None:
    revocations, store, sessions = _revocations()
    node = str(new_node_id())
    sessions.live.add(node)
    sessions.boom = True
    store.snap = RevocationSnapshot(frozenset({node}), frozenset({"abc"}), ("abc",))
    outcome = await revocations.revoke(node, actor="t")
    assert outcome.enforced is False
    assert revocations.deferred_enforcements == 1


async def test_a_successful_revoke_is_immediate() -> None:
    revocations, store, sessions = _revocations()
    node = str(new_node_id())
    sessions.live.add(node)
    store.snap = RevocationSnapshot(frozenset({node}), frozenset({"abc"}), ("abc",))
    outcome = await revocations.revoke(node, actor="t")
    assert outcome.enforced is True and outcome.session_killed is True
    assert outcome.as_dict()["enforcement"] == "immediate"


# --------------------------------------------------------------------- (b)


async def test_the_set_carries_reasons_and_refusals_follow_them() -> None:
    revocations, store, _ = _revocations()
    store.snap = RevocationSnapshot(
        frozenset(),
        frozenset({"aa", "bb", "cc"}),
        ("aa", "bb", "cc"),
        reasons={"aa": "revoked", "bb": "superseded"},  # cc: no reason given
    )
    await revocations.sync()
    refused = revocations.revoked_serials()
    assert isinstance(refused, Mapping)
    assert dict(refused) == {"aa": "revoked", "bb": "superseded", "cc": "revoked"}
    with pytest.raises(TypeError):
        refused["dd"] = "revoked"  # type: ignore[index]  # read-only view
    sup = refusal_for("bb", refused)
    assert sup is not None and sup.reason is PeerCertReason.CERT_SUPERSEDED
    assert status_for(sup.reason) is grpc.StatusCode.PERMISSION_DENIED
    rev = refusal_for("aa", refused)
    assert rev is not None and rev.reason is PeerCertReason.REVOKED
    assert refusal_for("cc", refused).reason is PeerCertReason.REVOKED  # type: ignore[union-attr]
    assert refusal_for("dd", refused) is None
    # A plain set (no reasons) still refuses, as revoked.
    plain = refusal_for("aa", frozenset({"aa"}))
    assert plain is not None and plain.reason is PeerCertReason.REVOKED


# --------------------------------------------------------------------- (c)


async def test_the_cut_scan_runs_only_when_the_serial_set_changes() -> None:
    revocations, store, sessions = _revocations()
    await revocations.sync()  # first sync: scans once (empty set)
    assert len(sessions.scans) == 1
    await revocations.sync()
    await revocations.sync()
    assert len(sessions.scans) == 1  # unchanged: no scan
    store.snap = RevocationSnapshot(frozenset(), frozenset({"aa"}), ("aa",))
    await revocations.sync()
    assert sessions.scans[-1] == {"aa"} and len(sessions.scans) == 2
    await revocations.sync()
    assert len(sessions.scans) == 2
    assert revocations.cut_scans == 2


# Admission: the presented serial is re-checked against the in-memory set.


class Registry_:  # noqa: N801 - a fake of Registry
    def __init__(self) -> None:
        self.nodes: dict[str, NodeRecord] = {}

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

    async def set_node_state(self, node_id: str, state: NodeState, **kw: Any) -> bool:
        return True

    async def close_session(self, session_id: str, reason: SessionEndReason) -> bool:
        return True

    async def touch_node(self, node_id: str) -> None:
        return None

    async def record_rtt(self, session_id: str, **kw: Any) -> None:
        return None


class Wall:
    def __init__(self) -> None:
        self.now = dt.datetime(2026, 10, 11, 12, 0, tzinfo=dt.UTC)

    def __call__(self) -> dt.datetime:
        return self.now


def _manager(wall: Wall | None = None) -> SessionManager:
    return SessionManager(
        cast(Registry, Registry_()),
        LivenessConfig(),
        clock=lambda: 1000.0,
        wall_clock=wall or Wall(),
    )


def _hello(node_id: str, epoch: int = 1) -> agent_pb2.Hello:
    return agent_pb2.Hello(
        node_id=node_id,
        protocol_version=1,
        agent_version="0.1.0.dev0",
        hostname="laptop",
        session_epoch=epoch,
        os="Ubuntu 24.04",
        platform="linux-x86_64",
    )


def _cert(node_id: str, serial: str, not_after: dt.datetime) -> PeerCert:
    return PeerCert(
        node_id=node_id,
        serial=serial,
        fingerprint="a" * 64,
        not_after=not_after,
        public_key_fingerprint="b" * 64,
    )


@pytest.mark.parametrize(
    ("reason", "code"),
    [("superseded", "cert_superseded"), ("revoked", "cert_revoked")],
)
async def test_admission_refuses_a_serial_the_set_lists(reason: str, code: str) -> None:
    manager = _manager()
    manager.refused_serials = lambda: {"5e": reason}
    node = str(new_node_id())
    later = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    refused = await manager.admit(
        _hello(node), "ipv4:127.0.0.1:1", cert=_cert(node, "5e", later)
    )
    assert isinstance(refused, Rejected)
    assert refused.abort_status is grpc.StatusCode.PERMISSION_DENIED
    assert refused.reason_code == code
    assert manager.get(node) is None
    # Another serial of the same node is admitted.
    ok = await manager.admit(
        _hello(node, 2), "ipv4:127.0.0.1:1", cert=_cert(node, "5f", later)
    )
    assert isinstance(ok, Accepted)
    for live in manager.snapshot():
        if live.cert_expiry is not None:
            live.cert_expiry.cancel()


# --------------------------------------------------------------------- (d)


async def test_an_early_expiry_timer_rearms_and_a_due_one_cuts() -> None:
    wall = Wall()
    manager = _manager(wall)
    node = str(new_node_id())
    not_after = wall.now + dt.timedelta(seconds=30)
    accepted = await manager.admit(
        _hello(node), "ipv4:127.0.0.1:1", cert=_cert(node, "77", not_after)
    )
    assert isinstance(accepted, Accepted)
    live = accepted.live
    assert live.cert_not_after == not_after and live.cert_expiry is not None
    first = live.cert_expiry
    # The wall clock was set back: the monotonic timer fires while the
    # certificate is still valid by the wall clock.
    first.cancel()
    manager._cert_expired(live)
    assert not live.ended
    assert live.cert_expiry is not None and live.cert_expiry is not first
    # Exactly at not_after: due.
    live.cert_expiry.cancel()
    wall.now = not_after
    manager._cert_expired(live)
    assert live.ended and live.end_reason is SessionEndReason.CERT_EXPIRED
    await asyncio.sleep(0)


async def test_a_late_timer_still_cuts_an_expired_certificate() -> None:
    wall = Wall()
    manager = _manager(wall)
    node = str(new_node_id())
    not_after = wall.now + dt.timedelta(seconds=5)
    accepted = await manager.admit(
        _hello(node), "ipv4:127.0.0.1:1", cert=_cert(node, "78", not_after)
    )
    assert isinstance(accepted, Accepted)
    assert accepted.live.cert_expiry is not None
    accepted.live.cert_expiry.cancel()
    wall.now = not_after + dt.timedelta(hours=1)  # clock jumped forward
    manager._cert_expired(accepted.live)
    assert accepted.live.end_reason is SessionEndReason.CERT_EXPIRED


async def test_peer_cert_error_reasons_are_stable() -> None:
    assert PeerCertReason.CERT_SUPERSEDED.value == "cert_superseded"
    err = PeerCertError(PeerCertReason.CERT_SUPERSEDED, "x")
    assert status_for(err.reason) is grpc.StatusCode.PERMISSION_DENIED


# --------------------------------------------------------------------- (e)


@pytest.mark.parametrize("client_certs", ["none", "optional", "required"])
async def test_controller_tls_contexts_send_no_session_tickets(
    tmp_path: Any, client_certs: Any
) -> None:
    """D156 (e): no TLS 1.3 session tickets (what a two-thread client trips
    over; nothing resumes sessions anyway)."""
    from meshforge.common import pki
    from meshforge.controller.fabric_ca import create_fabric, ensure_server_cert
    from meshforge.controller.server_tls import ServerTls

    fabric = create_fabric(tmp_path / "ca")
    first = ensure_server_cert(tmp_path / "tls", fabric, hosts=["127.0.0.1"])
    holder = ServerTls(
        fabric, first, hosts=["127.0.0.1"], lifetime=pki.SERVER_CERT_LIFETIME
    )
    assert holder.ssl_context(client_certs=client_certs).num_tickets == 0


async def test_peer_server_tls_context_sends_no_session_tickets(tmp_path: Any) -> None:
    from mf_fabric import fabric_ca, node_material

    from meshforge.agent.peer.server import peer_server_tls

    key, cert = node_material(str(new_node_id()))
    key_file, cert_file = tmp_path / "node.key", tmp_path / "node.crt"
    key_file.write_bytes(key)
    cert_file.write_bytes(cert)
    tls = peer_server_tls(fabric_ca().cert, cert_file=cert_file, key_file=key_file)
    assert tls.context.num_tickets == 0
