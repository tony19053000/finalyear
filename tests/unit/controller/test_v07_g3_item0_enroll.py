"""V0.7 G3 item 0 (D153) on the controller's enrolment listener.

* (1) a ``token_used`` refusal tells the user that a timed-out join may have
  spent the code (by design: a lost reply burns it);
* (8) the listener closes idle / old connections and bounds every ``Enroll``
  server-side: a hanging enrolment is refused ``UNAVAILABLE`` / ``unavailable``
  after the deadline, whatever the client's own deadline.

Real TLS gRPC on loopback; the ``Enroller`` is a stand-in that never answers
(the database path is covered by the component tests).
"""

from __future__ import annotations

import asyncio
from typing import Any

import grpc
import pytest

from meshforge.common import pki
from meshforge.common.ids import new_fabric_id
from meshforge.common.netaddr import parse_bind
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.controller.enrollment import listener as listener_mod
from meshforge.controller.enrollment.listener import (
    CONNECTION_OPTIONS,
    EnrollListener,
    EnrollmentServicer,
    SourceRateLimiter,
)
from meshforge.controller.enrollment.service import _TOKEN_MESSAGES
from meshforge.controller.fabric_ca import ServerCert, ServerCertAction

pytestmark = pytest.mark.unit


def test_token_used_message_names_the_lost_reply_case() -> None:
    message = _TOKEN_MESSAGES["token_used"]
    assert "timed out" in message
    assert "ask for a new code" in message


class _Hanging:
    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    async def enroll(self, token: bytes, csr: bytes, *, source: str | None) -> Any:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


async def _start(
    servicer: EnrollmentServicer, monkeypatch: pytest.MonkeyPatch | None = None
) -> tuple[EnrollListener, pki.CertificateAuthority, list[Any]]:
    seen: list[Any] = []
    if monkeypatch is not None:
        real = grpc.aio.server

        def spy(*args: Any, **kwargs: Any) -> grpc.aio.Server:
            seen.append(kwargs.get("options"))
            return real(*args, **kwargs)

        monkeypatch.setattr(listener_mod.grpc.aio, "server", spy)
    ca = pki.create_ca(new_fabric_id())
    issued = pki.issue_server_cert(ca, hosts=["127.0.0.1"])
    cert = ServerCert(issued=issued, path=None, action=ServerCertAction.ISSUED)  # type: ignore[arg-type]
    lis = EnrollListener(servicer)
    await lis.start(parse_bind("127.0.0.1:0"), cert, pki.cert_to_pem(ca.cert))
    return lis, ca, seen


def _limiter() -> SourceRateLimiter:
    return SourceRateLimiter(
        rate_per_minute=600, burst=100, max_sources=16, clock=lambda: 0.0
    )


@pytest.mark.asyncio
async def test_hanging_enrolment_is_refused_unavailable_after_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hanging = _Hanging()
    servicer = EnrollmentServicer(hanging, _limiter(), deadline_s=0.3)  # type: ignore[arg-type]
    lis, ca, options = await _start(servicer, monkeypatch)
    try:
        creds = grpc.ssl_channel_credentials(root_certificates=pki.cert_to_pem(ca.cert))
        async with grpc.aio.secure_channel(
            f"127.0.0.1:{lis.port}",
            creds,
            options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
        ) as channel:
            stub = enroll_pb2_grpc.EnrollmentStub(channel)
            loop = asyncio.get_running_loop()
            started = loop.time()
            call = stub.Enroll(enroll_pb2.EnrollRequest(join_token=b"x" * 16))
            with pytest.raises(grpc.aio.AioRpcError) as info:
                await call  # no client deadline at all
            took = loop.time() - started
        assert info.value.code() is grpc.StatusCode.UNAVAILABLE
        trailing = dict(info.value.trailing_metadata() or ())
        assert trailing.get("meshforge-reason") == "unavailable"
        assert 0.25 <= took < 3.0, took
        await asyncio.wait_for(hanging.cancelled.wait(), 2.0)  # txn abandoned
    finally:
        await lis.stop(grace=0)
    # (8) idle / age limits are on the listener.
    assert options and options[0] is not None
    keys = dict(options[0])
    for key, value in CONNECTION_OPTIONS:
        assert keys[key] == value
    assert keys["grpc.max_connection_idle_ms"] <= 60_000
    assert keys["grpc.max_connection_age_ms"] <= 300_000
