"""V0.7 G5 item 0, G4 reviewer N2 (D159): a renewal presented on the node's
**pending** certificate (connected with it, not yet activated by a Hello)
activates that certificate first - it is never superseded behind the next
pending one. Real PostgreSQL, the real ControllerRuntime, real gRPC/TLS."""

from __future__ import annotations

import asyncio

import grpc
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_forged_certs import forge
from test_v07_g4_renewal import (  # noqa: F401 - settings is a fixture
    _connect_once,
    _csr,
    _hello_status,
    _renew,
    _renewed,
    _sql,
    settings,
)

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
]

MIN_INTERVAL_S = 2.0  # the renewal module's settings fixture (test_v07_g4_renewal)


async def test_renewing_on_the_pending_certificate_activates_it_first(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    old = forge()
    await _connect_once(runtime, old)
    reply, code, trailing = await _renew(runtime, old, _csr(old))
    assert code is grpc.StatusCode.OK, trailing
    assert reply is not None
    pending = _renewed(old, reply)

    await asyncio.sleep(MIN_INTERVAL_S + 0.2)  # past the rate limit
    # The node renews again over a channel on its PENDING certificate.
    reply2, code2, trailing2 = await _renew(runtime, pending, _csr(pending))
    assert code2 is grpc.StatusCode.OK, trailing2
    assert reply2 is not None
    newest = _renewed(pending, reply2)

    rows = await _sql(
        pg_settings,
        "SELECT cert_serial, pending_cert_serial FROM node WHERE node_id = :n",
        n=old.node_id,
    )
    assert rows == [(pending.serial, newest.serial)]
    revoked = dict(
        await _sql(
            pg_settings,
            "SELECT serial, reason FROM revoked_cert WHERE serial IN (:a, :b)",
            a=old.serial,
            b=pending.serial,
        )
    )
    # The live (pending) certificate is NOT superseded; the old current one is.
    assert revoked == {old.serial: "superseded"}
    audit = await _sql(
        pg_settings,
        "SELECT details->>'activated_presented', details->>'replaced_pending' "
        "FROM audit_event WHERE action = 'cert_renewed' AND node_id = :n "
        "AND details->>'serial' = :s",
        n=old.node_id,
        s=newest.serial,
    )
    assert audit == [("true", None)]
    assert (
        len(
            await _sql(
                pg_settings,
                "SELECT 1 FROM audit_event WHERE action = 'cert_activated' "
                "AND node_id = :n AND details->>'serial' = :s",
                n=old.node_id,
                s=pending.serial,
            )
        )
        == 1
    )
    assert runtime.revocations is not None
    await runtime.revocations.sync()
    # The once-pending certificate still opens a session; the old one is refused.
    assert await _hello_status(runtime, pending, epoch=5) == (grpc.StatusCode.OK, None)
    code3, reason = await _hello_status(runtime, old, epoch=6)
    assert (code3, reason) == (grpc.StatusCode.PERMISSION_DENIED, "cert_superseded")
