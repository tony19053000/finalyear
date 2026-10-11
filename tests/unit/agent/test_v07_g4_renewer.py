"""V0.7 G4 deliverable 1 (D140): the agent's ``CertRenewer`` without a
controller.

* a fresh certificate is not due; one past 2/3 of its life is; a staged
  rotation key makes it due at once;
* not enrolled -> nothing to do (the session itself stops with the join hint);
* a refusal's trailing metadata becomes a typed reason and the controller's
  retry-after (bounded) is honoured;
* the hand-over is asked for only when the stream still uses another
  certificate than the file on disk.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import grpc
import pytest

from meshforge.agent import enrollment
from meshforge.agent.identity import NodeIdentity, load_or_create, stage_rotation_key
from meshforge.agent.session import renewal
from meshforge.agent.session.renewal import CertRenewer, RenewOutcome
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.common import pki
from meshforge.common.ids import new_fabric_id

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class Client:
    def __init__(self, identity: NodeIdentity) -> None:
        self.identity = identity
        self.node_id = str(identity.node_id)
        self.state = AgentState.ONLINE
        self.controller_target = "127.0.0.1:1"
        self.stream_cert_serial: str | None = None
        self.handovers: list[str] = []

    def request_handover(self, reason: str) -> bool:
        self.handovers.append(reason)
        return True

    def replace_identity(self, identity: NodeIdentity) -> None:
        self.identity = identity


def _enrol(
    tmp_path: Path, *, lifetime: dt.timedelta = pki.NODE_CERT_LIFETIME
) -> tuple[NodeIdentity, pki.CertificateAuthority, Any]:
    me = load_or_create(tmp_path / "identity")
    ca = pki.create_ca(new_fabric_id())
    csr = pki.build_csr(me.private_key, fabric_id=ca.fabric_id, node_id=me.node_id)
    cert = pki.issue_node_cert(
        ca, csr, node_id=me.node_id, fabric_id=ca.fabric_id, lifetime=lifetime
    )
    enrollment.store(me, cert, ca.cert, force=True)
    return me, ca, cert


def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(
        data_dir=tmp_path, agent=AgentSection(controller_address="127.0.0.1:1")
    )


async def test_fresh_certificates_are_not_due_and_old_ones_are(tmp_path: Path) -> None:
    me, _ca, cert = _enrol(tmp_path)
    client = Client(me)
    renewer = CertRenewer(_settings(tmp_path), client)
    assert (await renewer.check_once())[0] is RenewOutcome.NOT_DUE
    due_at = renewal.renewal_due_at(cert.not_valid_before_utc, cert.not_valid_after_utc)
    attempted: list[bool] = []

    async def fake_renew(*_a: Any, rotating: bool, **_k: Any) -> None:
        attempted.append(rotating)

    renewer._renew = fake_renew  # type: ignore[method-assign]
    renewer._now = lambda: due_at + dt.timedelta(seconds=1)
    assert (await renewer.check_once())[0] is RenewOutcome.RENEWED
    renewer._now = lambda: due_at - dt.timedelta(seconds=1)
    assert (await renewer.check_once())[0] is RenewOutcome.NOT_DUE
    stage_rotation_key(me.path)  # a staged key: due at once
    assert (await renewer.check_once())[0] is RenewOutcome.ROTATED
    assert attempted == [False, True]


async def test_not_enrolled_is_skipped(tmp_path: Path) -> None:
    me = load_or_create(tmp_path / "identity")
    renewer = CertRenewer(_settings(tmp_path), Client(me))
    assert (await renewer.check_once()) == (RenewOutcome.SKIPPED, None)


class _RpcError(grpc.aio.AioRpcError):
    def __init__(self, code: grpc.StatusCode, metadata: tuple[tuple[str, str], ...]):
        super().__init__(
            code,
            grpc.aio.Metadata(),
            grpc.aio.Metadata(*metadata),
            details="refused",
        )


async def test_refusals_are_typed_and_retry_after_is_bounded() -> None:
    failure = renewal._rpc_failure(
        _RpcError(
            grpc.StatusCode.RESOURCE_EXHAUSTED,
            (("meshforge-reason", "rate_limited"), ("meshforge-retry-after-s", "42")),
        )
    )
    assert (failure.reason, failure.retry_after_s) == ("rate_limited", 42.0)
    huge = renewal._rpc_failure(
        _RpcError(
            grpc.StatusCode.RESOURCE_EXHAUSTED, (("meshforge-retry-after-s", "1e12"),)
        )
    )
    assert huge.retry_after_s == 86_400.0 and huge.reason == "resource_exhausted"
    junk = renewal._rpc_failure(
        _RpcError(grpc.StatusCode.UNAVAILABLE, (("meshforge-retry-after-s", "soon"),))
    )
    assert junk.retry_after_s is None and junk.reason == "unavailable"


async def test_handover_only_when_the_stream_uses_another_certificate(
    tmp_path: Path,
) -> None:
    me, _ca, cert = _enrol(tmp_path)
    client = Client(me)
    renewer = CertRenewer(_settings(tmp_path), client)
    assert not await renewer.handover_if_needed()  # no stream
    client.stream_cert_serial = format(cert.serial_number, "x")
    assert not await renewer.handover_if_needed()  # same certificate
    client.stream_cert_serial = "abc"
    assert await renewer.handover_if_needed()
    client.state = AgentState.BACKOFF
    assert not await renewer.handover_if_needed()
    assert client.handovers == ["cert_renewed"]
