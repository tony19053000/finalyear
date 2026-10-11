"""Node certificate renewal on the agent (V0.7 G4, roadmap 0.7.7b, D140).

:class:`CertRenewer` runs beside the session client (mtls mode only):

* **When.** Every ``[agent.cert] renew_check_interval_s`` (and right after the
  first stream is up) it reads the enrolment: a certificate past two thirds of
  its life (:func:`pki.renewal_due`) is due, and so is a key staged by
  ``meshforge-agent rotate-key`` (``node.key.next``).
* **How.** A CSR for this node signed by the key to certify (the identity key,
  or the staged one), sent as ``RenewCertificate`` over a fresh mutual-TLS
  channel to the controller this agent is connected to - authenticated by the
  current certificate. The answer is checked like a join's
  (:func:`~meshforge.agent.enrollment.check_renewed`: our pinned CA, our key,
  our node, valid now) before anything is written.
* **Swap.** A renewal replaces ``node.crt`` atomically
  (:func:`~meshforge.agent.enrollment.replace_node_cert`); a rotation commits
  key + ``node.json`` + certificate together
  (:func:`~meshforge.agent.identity.commit_rotation`). Then every TLS context
  that presents the certificate is reloaded (blob/probe HTTPS client, peer
  server) and the session is **handed over**: the client reconnects at once
  with the new certificate (``request_handover``), which the controller takes
  as the confirmation to make it current and supersede the old serial. That
  happens two thirds into the certificate's life - days before ``not_after``,
  so the controller's expiry cut never meets a renewing agent (CG-E3).
* **Failures.** Logged with their reason, retried after
  ``renew_retry_base_s`` doubling up to the check interval (a ``rate_limited``
  refusal waits at least what the controller asks). Nothing falls back: an
  expired certificate ends in the session's own "re-join needed" stop with the
  exact command (:mod:`~meshforge.agent.session.tls`).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

import grpc
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from meshforge.agent import enrollment
from meshforge.agent.identity import (
    NodeIdentity,
    commit_rotation,
    staged_rotation_key,
)
from meshforge.agent.session.state import AgentState
from meshforge.agent.session.tls import channel_options, load_session_tls
from meshforge.agent.settings import AgentSettings
from meshforge.common import pki
from meshforge.common.errors import MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc

__all__ = [
    "RENEW_TIMEOUT_S",
    "CertRenewer",
    "RenewOutcome",
    "renewal_due_at",
]

log = get_logger(__name__)

RENEW_TIMEOUT_S: Final = 15.0
_REASON_KEY: Final = "meshforge-reason"
_RETRY_AFTER_KEY: Final = "meshforge-retry-after-s"


class RenewOutcome(StrEnum):
    NOT_DUE = "not_due"
    RENEWED = "renewed"
    ROTATED = "rotated"
    FAILED = "failed"
    SKIPPED = "skipped"
    """No stream / not enrolled: nothing to do now."""


def renewal_due_at(not_before: dt.datetime, not_after: dt.datetime) -> dt.datetime:
    """When a certificate valid ``not_before .. not_after`` is due (2/3)."""
    return not_before + (not_after - not_before) * pki.RENEW_FRACTION


@dataclass(slots=True)
class _Failure:
    reason: str
    retry_after_s: float | None = None


class CertRenewer:
    """See module docstring. ``run()`` in one task; event-loop owned."""

    def __init__(
        self,
        settings: AgentSettings,
        client: Any,
        *,
        on_renewed: list[Callable[[NodeIdentity], None]] | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        """``client``: the :class:`~.client.AgentSessionClient` (its identity,
        state, controller target, stream serial and ``request_handover``)."""
        self._settings = settings
        self._client = client
        self.on_renewed: list[Callable[[NodeIdentity], None]] = list(on_renewed or [])
        self._now = now or (lambda: dt.datetime.now(dt.UTC))
        self.renewals = 0
        self.rotations = 0
        self.failures = 0
        self.last_failure: str | None = None

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        """Check, renew when due, hand over; never raises but CancelledError."""
        cfg = self._settings.agent.cert
        delay = cfg.renew_check_interval_s
        retry = cfg.renew_retry_base_s
        while True:
            await self._wait_online()
            try:
                outcome, failure = await self.check_once()
            except Exception:  # the renewer must not die
                log.exception("agent.cert.renew_crashed", node_id=self._client.node_id)
                outcome, failure = RenewOutcome.FAILED, _Failure("internal")
            if outcome is RenewOutcome.FAILED:
                wait = retry
                if failure is not None and failure.retry_after_s is not None:
                    wait = max(wait, failure.retry_after_s)
                retry = min(retry * 2, cfg.renew_check_interval_s)
                delay = wait
            else:
                retry = cfg.renew_retry_base_s
                delay = cfg.renew_check_interval_s
            await self.handover_if_needed()
            await asyncio.sleep(delay)

    async def _wait_online(self) -> None:
        while self._client.state is not AgentState.ONLINE:
            with contextlib.suppress(TimeoutError):
                await self._client.wait_for_state(AgentState.ONLINE, timeout_s=3600.0)

    # ----------------------------------------------------------------- check

    async def check_once(self) -> tuple[RenewOutcome, _Failure | None]:
        """One check (see module docstring); the outcome and a failure, if any."""
        identity: NodeIdentity = self._client.identity
        try:
            stored = await asyncio.to_thread(enrollment.load, identity)
            staged = await asyncio.to_thread(staged_rotation_key, identity.path)
        except MeshForgeError as exc:
            self._failed("unreadable_enrollment", exc.message)
            return RenewOutcome.FAILED, _Failure("unreadable_enrollment")
        if stored is None:
            return RenewOutcome.SKIPPED, None
        now = self._now()
        due_at = renewal_due_at(stored.node_cert.not_valid_before_utc, stored.not_after)
        if staged is None and now < due_at:
            return RenewOutcome.NOT_DUE, None
        if not self._client.controller_target:
            return RenewOutcome.SKIPPED, None
        rotating = staged is not None
        key = staged if staged is not None else identity.private_key
        log.info(
            "agent.cert.renewing",
            node_id=identity.node_id,
            serial=format(stored.node_cert.serial_number, "x"),
            not_after=stored.not_after.isoformat(),
            rotate_key=rotating,
        )
        failure = await self._renew(identity, stored, key, rotating=rotating)
        if failure is not None:
            return RenewOutcome.FAILED, failure
        return (RenewOutcome.ROTATED if rotating else RenewOutcome.RENEWED), None

    async def _renew(
        self,
        identity: NodeIdentity,
        stored: enrollment.Enrollment,
        key: ec.EllipticCurvePrivateKey,
        *,
        rotating: bool,
    ) -> _Failure | None:
        csr = pki.build_csr(key, fabric_id=stored.fabric_id, node_id=identity.node_id)
        request = agent_pb2.RenewCertificateRequest(
            csr_pem=csr.public_bytes(serialization.Encoding.PEM)
        )
        try:
            response = await self._call(identity, request)
        except grpc.aio.AioRpcError as exc:
            failure = _rpc_failure(exc)
            self._failed(failure.reason, exc.details() or "")
            return failure
        except MeshForgeError as exc:  # SessionTlsError: not enrolled / expired
            self._failed("tls", exc.message)
            return _Failure("tls")
        try:
            cert = enrollment.check_renewed(
                bytes(response.node_cert_pem),
                bytes(response.ca_cert_pem),
                current=stored,
                key=key,
                now=self._now(),
            )
            if rotating:
                rotated = await asyncio.to_thread(
                    commit_rotation, identity, key, pki.cert_to_pem(cert)
                )
                self._client.replace_identity(rotated)
                identity = rotated
                self.rotations += 1
            else:
                await asyncio.to_thread(enrollment.replace_node_cert, identity, cert)
                self.renewals += 1
        except MeshForgeError as exc:
            self._failed("bad_response_or_storage", exc.message)
            return _Failure("bad_response_or_storage")
        log.info(
            "agent.cert.renewed",
            node_id=identity.node_id,
            serial=format(cert.serial_number, "x"),
            not_after=cert.not_valid_after_utc.isoformat(),
            renewal_due_at=renewal_due_at(
                cert.not_valid_before_utc, cert.not_valid_after_utc
            ).isoformat(),
            key_rotated=rotating,
        )
        for hook in self.on_renewed:
            try:
                hook(identity)
            except Exception:  # a broken reload must not stop the hand-over
                log.exception("agent.cert.reload_failed", node_id=identity.node_id)
        await self.handover_if_needed()
        return None

    async def _call(
        self, identity: NodeIdentity, request: agent_pb2.RenewCertificateRequest
    ) -> agent_pb2.RenewCertificateResponse:
        tls = await asyncio.to_thread(load_session_tls, identity)
        async with grpc.aio.secure_channel(
            self._client.controller_target, tls.credentials, options=channel_options()
        ) as channel:
            stub = agent_pb2_grpc.AgentControlStub(channel)
            response: agent_pb2.RenewCertificateResponse = await stub.RenewCertificate(
                request, timeout=RENEW_TIMEOUT_S
            )
        return response

    # -------------------------------------------------------------- hand-over

    async def handover_if_needed(self) -> bool:
        """Ask the session to reconnect when its stream still uses another
        certificate than the one on disk (after a renewal, or a hand-over
        that could not happen at the time). True when one was requested."""
        stream_serial = self._client.stream_cert_serial
        if stream_serial is None or self._client.state is not AgentState.ONLINE:
            return False
        try:
            stored = await asyncio.to_thread(enrollment.load, self._client.identity)
        except MeshForgeError:
            return False
        if stored is None:
            return False
        on_disk = format(stored.node_cert.serial_number, "x")
        if on_disk == stream_serial:
            return False
        return bool(self._client.request_handover("cert_renewed"))

    def _failed(self, reason: str, detail: str) -> None:
        self.failures += 1
        self.last_failure = reason
        log.warning(
            "agent.cert.renew_failed",
            node_id=self._client.node_id,
            reason=reason,
            detail=detail[:300],
        )


def _rpc_failure(exc: grpc.aio.AioRpcError) -> _Failure:
    reason = exc.code().name.lower()
    retry_after: float | None = None
    for key, value in exc.trailing_metadata() or ():
        if key == _REASON_KEY and isinstance(value, str):
            reason = value[:64]
        elif key == _RETRY_AFTER_KEY and isinstance(value, str):
            try:
                retry_after = min(float(value), 86_400.0)
            except ValueError:
                retry_after = None
    return _Failure(reason, retry_after)
