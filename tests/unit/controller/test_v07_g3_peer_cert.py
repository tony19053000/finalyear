"""V0.7 G3 (0.7.5, D137): the session port's identity checks, branch by branch.

:func:`parse_peer` fails closed on every input (checklist 19): no certificate,
garbage, another fabric's node URI, the controller's own server certificate (no
``clientAuth``), expired / not yet valid, a revoked serial. The interceptor
refuses any call without an acceptable certificate unless its method is on the
allow-list, and the agent's channel helper refuses to start without an
enrolment. Real certificates throughout; the gRPC context is a stand-in that
only answers ``auth_context`` / ``peer`` / ``abort``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import grpc
import pytest

from meshforge.agent import enrollment
from meshforge.agent.identity import load_or_create
from meshforge.agent.session.tls import SessionTlsError, load_session_tls
from meshforge.common import pki
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.controller.session.peer_cert import (
    NodeCertInterceptor,
    PeerCertError,
    PeerCertReason,
    parse_peer,
    status_for,
)

pytestmark = [pytest.mark.unit, pytest.mark.security]

NOW = dt.datetime.now(dt.UTC)


@pytest.fixture(scope="module")
def ca() -> pki.CertificateAuthority:
    return pki.create_ca(new_fabric_id(), now=NOW - dt.timedelta(days=2))


def _node_cert(
    ca: pki.CertificateAuthority,
    node_id: str | None = None,
    *,
    now: dt.datetime = NOW,
    lifetime: dt.timedelta = dt.timedelta(days=30),
) -> tuple[str, bytes]:
    node = node_id or str(new_node_id())
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node),
        node_id=node,
        fabric_id=ca.fabric_id,
        now=now,
        lifetime=lifetime,
    )
    return node, pki.cert_to_pem(cert)


def test_accepts_a_current_node_cert(ca: pki.CertificateAuthority) -> None:
    node, pem = _node_cert(ca)
    peer = parse_peer(pem, fabric_id=ca.fabric_id, now=NOW)
    assert peer.node_id == node
    cert = pki.cert_from_pem(pem)
    assert peer.serial == format(cert.serial_number, "x")
    assert peer.fingerprint == pki.cert_fingerprint(cert)
    assert len(peer.public_key_fingerprint) == 64


@pytest.mark.parametrize(
    ("pem", "reason"),
    [
        (None, PeerCertReason.NO_CERT),
        (b"", PeerCertReason.NO_CERT),
        (b"-----BEGIN CERTIFICATE-----\ngarbage\n-----END CERTIFICATE-----\n",
         PeerCertReason.MALFORMED),
    ],
)  # fmt: skip
def test_missing_or_garbage(
    ca: pki.CertificateAuthority, pem: bytes | None, reason: PeerCertReason
) -> None:
    with pytest.raises(PeerCertError) as info:
        parse_peer(pem, fabric_id=ca.fabric_id, now=NOW)
    assert info.value.reason is reason


def test_another_fabric(ca: pki.CertificateAuthority) -> None:
    other = pki.create_ca(new_fabric_id(), now=NOW - dt.timedelta(days=1))
    _, pem = _node_cert(other)
    with pytest.raises(PeerCertError) as info:
        parse_peer(pem, fabric_id=ca.fabric_id, now=NOW)
    assert info.value.reason is PeerCertReason.WRONG_FABRIC


def test_the_server_cert_is_not_a_client_identity(
    ca: pki.CertificateAuthority,
) -> None:
    server = pki.issue_server_cert(ca, hosts=["127.0.0.1"], now=NOW)
    with pytest.raises(PeerCertError) as info:
        parse_peer(pki.cert_to_pem(server.cert), fabric_id=ca.fabric_id, now=NOW)
    # it names no node at all - refused before the EKU check even matters
    assert info.value.reason in (PeerCertReason.MALFORMED, PeerCertReason.NOT_CLIENT)


def test_expired_and_not_yet_valid(ca: pki.CertificateAuthority) -> None:
    _, pem = _node_cert(
        ca, now=NOW - dt.timedelta(days=1), lifetime=dt.timedelta(hours=1)
    )
    with pytest.raises(PeerCertError) as info:
        parse_peer(pem, fabric_id=ca.fabric_id, now=NOW)
    assert info.value.reason is PeerCertReason.EXPIRED
    _, fresh = _node_cert(ca)
    with pytest.raises(PeerCertError) as info:
        parse_peer(fresh, fabric_id=ca.fabric_id, now=NOW - dt.timedelta(hours=1))
    assert info.value.reason is PeerCertReason.EXPIRED


def test_revoked_serial(ca: pki.CertificateAuthority) -> None:
    _, pem = _node_cert(ca)
    serial = format(pki.cert_from_pem(pem).serial_number, "x")
    with pytest.raises(PeerCertError) as info:
        parse_peer(pem, fabric_id=ca.fabric_id, now=NOW, revoked={serial})
    assert info.value.reason is PeerCertReason.REVOKED
    assert status_for(PeerCertReason.REVOKED) is grpc.StatusCode.PERMISSION_DENIED
    assert status_for(PeerCertReason.NO_CERT) is grpc.StatusCode.UNAUTHENTICATED
    assert parse_peer(pem, fabric_id=ca.fabric_id, now=NOW, revoked={"ab"})


# ------------------------------------------------------------- interceptor


class _AbortedError(Exception):
    pass


class _Context:
    def __init__(self, pem: bytes | None) -> None:
        self._auth = {"x509_pem_cert": [pem]} if pem else {}
        self.trailing: tuple[tuple[str, str], ...] = ()
        self.aborted: tuple[grpc.StatusCode, str] | None = None

    def auth_context(self) -> dict[str, list[bytes]]:
        return self._auth

    def peer(self) -> str:
        return "ipv4:127.0.0.1:5000"

    def set_trailing_metadata(self, md: tuple[tuple[str, str], ...]) -> None:
        self.trailing = md

    async def abort(self, code: grpc.StatusCode, details: str) -> None:
        self.aborted = (code, details)
        raise _AbortedError


class _Details:
    def __init__(self, method: str) -> None:
        self.method = method
        self.invocation_metadata = ()


def _handler() -> tuple[grpc.RpcMethodHandler, list[str]]:
    calls: list[str] = []

    async def behaviour(request: Any, context: Any) -> str:
        calls.append("ran")
        return "ok"

    return grpc.unary_unary_rpc_method_handler(behaviour), calls


async def _run(
    interceptor: NodeCertInterceptor, method: str, ctx: _Context
) -> tuple[Any, list[str]]:
    handler, calls = _handler()

    async def continuation(_details: Any) -> grpc.RpcMethodHandler:
        return handler

    guarded = await interceptor.intercept_service(continuation, _Details(method))
    try:
        result = await guarded.unary_unary(None, ctx)
    except _AbortedError:
        result = None
    return result, calls


@pytest.mark.asyncio
async def test_interceptor_refuses_without_a_cert_and_runs_with_one(
    ca: pki.CertificateAuthority,
) -> None:
    interceptor = NodeCertInterceptor(fabric_id=ca.fabric_id, clock=lambda: NOW)
    ctx = _Context(None)
    result, calls = await _run(interceptor, "/meshforge.v1.AgentControl/Session", ctx)
    assert result is None and calls == []
    assert ctx.aborted is not None and ctx.aborted[0] is grpc.StatusCode.UNAUTHENTICATED
    assert dict(ctx.trailing)["meshforge-reason"] == "no_cert"
    assert interceptor.refused == 1
    _, pem = _node_cert(ca)
    result, calls = await _run(
        interceptor, "/meshforge.v1.AgentControl/Session", _Context(pem)
    )
    assert result == "ok" and calls == ["ran"]


@pytest.mark.asyncio
async def test_interceptor_allow_list_and_revocation(
    ca: pki.CertificateAuthority,
) -> None:
    _, pem = _node_cert(ca)
    serial = format(pki.cert_from_pem(pem).serial_number, "x")
    revoked: set[str] = set()
    interceptor = NodeCertInterceptor(
        fabric_id=ca.fabric_id,
        revoked=lambda: revoked,
        allow_without_cert={"/meshforge.v1.Enrollment/Enroll"},
        clock=lambda: NOW,
    )
    result, calls = await _run(
        interceptor, "/meshforge.v1.Enrollment/Enroll", _Context(None)
    )
    assert result == "ok"  # allow-listed: no certificate needed
    result, _ = await _run(
        interceptor, "/meshforge.v1.Enrollment/Other", _Context(None)
    )
    assert result is None  # only the exact method is allowed
    revoked.add(serial)  # read at every call, not cached
    ctx = _Context(pem)
    result, calls = await _run(interceptor, "/meshforge.v1.AgentControl/Session", ctx)
    assert result is None and calls == []
    assert (
        ctx.aborted is not None and ctx.aborted[0] is grpc.StatusCode.PERMISSION_DENIED
    )


# ------------------------------------------------------------- agent side


def test_agent_refuses_to_start_unenrolled_or_expired(tmp_path: Path) -> None:
    identity = load_or_create(tmp_path / "identity")
    with pytest.raises(SessionTlsError) as info:
        load_session_tls(identity)
    assert "meshforge-agent join" in info.value.message
    assert info.value.details["reason"] == "not_enrolled"
    ca = pki.create_ca(new_fabric_id(), now=NOW - dt.timedelta(days=2))
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(
            identity.private_key, fabric_id=ca.fabric_id, node_id=identity.node_id
        ),
        node_id=identity.node_id,
        fabric_id=ca.fabric_id,
        now=NOW - dt.timedelta(days=1),
        lifetime=dt.timedelta(hours=2),
    )
    enrollment.store(identity, cert, ca.cert, force=False)
    with pytest.raises(SessionTlsError) as info:
        load_session_tls(identity)
    assert "join --force" in info.value.message
    assert info.value.details["reason"] == "cert_not_valid_now"
    tls = load_session_tls(
        identity, now=NOW - dt.timedelta(days=1) + dt.timedelta(hours=1)
    )
    assert tls.fabric_id == ca.fabric_id
    assert tls.serial == format(cert.serial_number, "x")
