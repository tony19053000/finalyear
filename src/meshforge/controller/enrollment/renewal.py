"""Node certificate renewal and hand-over (V0.7 G4, roadmap 0.7.7b, D140).

**Renew** (:meth:`CertRenewals.renew`, the ``RenewCertificate`` RPC on the
mutual-TLS session port). The caller is the node named by the call's client
certificate (:class:`~meshforge.controller.session.peer_cert.PeerCert`, already
checked by the interceptor: our fabric, valid now, not revoked). Checks, in
this order, each refusal a typed :class:`RenewRefused` with a stable reason:

1. the CSR (:func:`pki.verify_csr`, no I/O): P-256, SHA-256, signed by its key,
   exactly one SAN URI in **this** fabric (``csr_<pki reason>``);
2. the CSR names the certificate's node (``renew_node_mismatch``) - a node can
   only renew itself;
3. then one transaction on the locked node row: the node exists and is not
   revoked (``node_revoked`` - a renewal racing a revocation loses, either
   way round: both lock the row); the presented certificate is the node's
   current or pending one (``cert_superseded``); the last issuance is at least
   ``[controller.renewal] min_interval_s`` ago (``rate_limited``);
4. the certificate is issued (``node_cert_lifetime_s``, SAN URI, EKU
   client+server) and recorded as **pending** (``pending_cert_*``,
   ``cert_renewed_at``); an older, never-used pending certificate is listed
   ``superseded`` (it is CA-valid until it expires); a ``cert_renewed`` audit
   row. If the caller presents the **pending** certificate itself (it already
   connected with it, its Hello has not activated it yet), that one is
   activated first in the same transaction (G4 reviewer N2, D159): the live
   certificate is never superseded behind a newer pending one. A CSR with
   another public key than the presented certificate's is a key rotation
   (``meshforge-agent rotate-key``): allowed - the caller proved the old key
   in the TLS handshake and the new one by signing the CSR.

**Activate** (:meth:`CertRenewals.activate`, called by session admission). The
node's first Hello on the pending certificate makes it current: the four
``cert_*`` columns take the pending values, the previous serial is recorded
``superseded`` (from now on refused on every plane) and a ``cert_activated``
audit row is added - all in one transaction, and only if the pending serial is
still the one presented (a newer renewal may have replaced it meanwhile).

Why supersede only at activation, not at issuance: until the agent proves it
holds the new certificate (by connecting with it), the old one is still its
identity. Superseding at issuance would cut the live session and every
in-flight transfer, and an agent that crashed before writing the new file would
be locked out (re-join). If the agent never uses the new certificate, the old
one keeps working until it expires, and the next renewal supersedes the unused
pending one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common import pki
from meshforge.common.logging import get_logger
from meshforge.controller.enrollment.invites import audit
from meshforge.controller.enrollment.service import public_key_fingerprint
from meshforge.controller.fabric_ca import FabricCa
from meshforge.controller.liveness import NodeState
from meshforge.controller.revocation import supersede
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.enrollment import AuditAction
from meshforge.controller.store.models.node import Node

__all__ = [
    "ACTOR",
    "CertRenewals",
    "PresentedCert",
    "RenewReason",
    "RenewRefused",
    "RenewResult",
]

log = get_logger(__name__)

ACTOR: Final = "renew"


class RenewReason(StrEnum):
    """Stable refusal codes (gRPC trailing metadata ``meshforge-reason``).
    CSR refusals are ``csr_<PkiReason>``."""

    NODE_MISMATCH = "renew_node_mismatch"
    NODE_REVOKED = "node_revoked"
    UNKNOWN_NODE = "unknown_node"
    CERT_SUPERSEDED = "cert_superseded"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"
    NOT_AVAILABLE = "renewal_unavailable"
    """No fabric CA / ``insecure-dev``: nothing to renew."""


class RenewRefused(Exception):  # noqa: N818 - a refusal, not a bug
    """A refused renewal; ``message`` is safe to send to the caller."""

    def __init__(
        self, reason: str, message: str, *, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class PresentedCert:
    """What renewal needs of the caller's verified client certificate."""

    node_id: str
    serial: str
    public_key_fingerprint: str


@dataclass(frozen=True, slots=True)
class RenewResult:
    node_id: str
    serial: str
    not_after: dt.datetime
    node_cert_pem: bytes
    ca_cert_pem: bytes
    key_rotated: bool
    replaced_pending: str | None
    """An older pending serial this renewal superseded (never used)."""


class _Refusal(Exception):  # noqa: N818 - control flow out of the transaction
    def __init__(self, refused: RenewRefused) -> None:
        super().__init__(refused.message)
        self.refused = refused


_AUDITED_REFUSALS: Final = frozenset(
    {
        RenewReason.NODE_MISMATCH,
        RenewReason.NODE_REVOKED,
        RenewReason.CERT_SUPERSEDED,
    }
)
"""Refusals worth an audit row (a node asking for something it may not have);
rate limits and CSR shape errors are only logged."""


class CertRenewals:
    """Renewal + activation of node certificates (see module docstring)."""

    def __init__(
        self,
        engine: AsyncEngine,
        fabric: FabricCa,
        *,
        min_interval_s: float,
        lifetime: dt.timedelta = pki.NODE_CERT_LIFETIME,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )
        self._fabric = fabric
        self._min_interval = dt.timedelta(seconds=min_interval_s)
        self._lifetime = lifetime
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self.issued = 0
        self.refused = 0
        self.activated = 0

    # ------------------------------------------------------------------ renew

    async def renew(
        self, presented: PresentedCert, csr_pem: bytes, *, source: str | None
    ) -> RenewResult:
        """See module docstring ("Renew"); raises :class:`RenewRefused`."""
        try:
            return await self._renew(presented, csr_pem, source)
        except RenewRefused as refused:
            self.refused += 1
            log.warning(
                "controller.cert.renew_refused",
                node_id=presented.node_id,
                cert_serial=presented.serial,
                reason=str(refused.reason),
            )
            if refused.reason in _AUDITED_REFUSALS:
                await self._audit_refusal(presented, refused, source)
            raise

    async def _renew(
        self, presented: PresentedCert, csr_pem: bytes, source: str | None
    ) -> RenewResult:
        if not csr_pem:
            raise RenewRefused("csr_malformed", "the request needs a CSR")
        try:
            verified = pki.verify_csr(csr_pem, fabric_id=self._fabric.fabric_id)
        except pki.PkiError as exc:
            raise RenewRefused(
                f"csr_{exc.reason.value}", f"the certificate request: {exc.message}"
            ) from None
        if verified.node_id != presented.node_id:
            raise RenewRefused(
                RenewReason.NODE_MISMATCH,
                "a node can only renew its own certificate (the CSR names "
                "another node than this connection's certificate)",
            )
        key_fp = public_key_fingerprint(verified.public_key)
        try:
            async with self._sessions() as db, db.begin():
                result = await self._issue(db, presented, csr_pem, key_fp, source)
        except _Refusal as refusal:
            raise refusal.refused from None
        except SQLAlchemyError as exc:
            log.warning(
                "controller.cert.renew_db_failed",
                node_id=presented.node_id,
                cause=type(exc).__name__,
            )
            raise RenewRefused(
                RenewReason.UNAVAILABLE,
                "the controller's database is unavailable; retry",
            ) from None
        self.issued += 1
        log.info(
            "controller.cert.renewed",
            node_id=result.node_id,
            serial=result.serial,
            presented_serial=presented.serial,
            not_after=result.not_after.isoformat(),
            key_rotated=result.key_rotated,
            replaced_pending=result.replaced_pending,
        )
        return result

    async def _issue(
        self,
        db: AsyncSession,
        presented: PresentedCert,
        csr_pem: bytes,
        key_fp: str,
        source: str | None,
    ) -> RenewResult:
        node_id = presented.node_id
        node = await db.scalar(
            select(Node)
            .where(Node.node_id == node_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if node is None:
            raise _Refusal(
                RenewRefused(RenewReason.UNKNOWN_NODE, "this node is not known")
            )
        if node.state == NodeState.REVOKED.value:
            raise _Refusal(
                RenewRefused(
                    RenewReason.NODE_REVOKED,
                    "this node was revoked from the fabric; no certificate is "
                    "issued to it",
                )
            )
        if presented.serial not in (node.cert_serial, node.pending_cert_serial):
            raise _Refusal(
                RenewRefused(
                    RenewReason.CERT_SUPERSEDED,
                    "this connection's certificate is not the node's current "
                    "one; re-join (meshforge-agent join --force <code>)",
                )
            )
        now = self._clock()
        last = node.cert_renewed_at
        if last is not None and now - last < self._min_interval:
            wait = (last + self._min_interval - now).total_seconds()
            raise _Refusal(
                RenewRefused(
                    RenewReason.RATE_LIMITED,
                    f"this node got a certificate {int((now - last).total_seconds())} s "
                    f"ago; renew again in {int(wait) + 1} s",
                    retry_after_s=wait,
                )
            )
        try:
            cert = pki.issue_node_cert(
                self._fabric.authority,
                csr_pem,
                node_id=node_id,
                fabric_id=self._fabric.fabric_id,
                now=now,
                lifetime=self._lifetime,
            )
        except pki.PkiError as exc:  # e.g. the CA itself expired meanwhile
            log.error(  # noqa: TRY400 - typed reason, no traceback needed
                "controller.cert.issue_failed", node_id=node_id, reason=exc.reason.value
            )
            raise _Refusal(
                RenewRefused(
                    RenewReason.INTERNAL, "the controller could not issue a certificate"
                )
            ) from None
        serial = format(cert.serial_number, "x")
        replaced = node.pending_cert_serial
        activated_first = False
        if replaced is not None and presented.serial == replaced:
            # G4 reviewer N2 (D159): the caller already runs on its pending
            # certificate (its Hello has not activated it yet). That pending
            # one is the live identity: activate it first (the old current one
            # is superseded, as a Hello would do), never supersede it behind
            # the new pending one.
            await self._activate_locked(db, node, replaced)
            self.activated += 1
            activated_first = True
            replaced = None
        if replaced is not None and node.pending_cert_not_after is not None:
            # Issued earlier, never used: it would stay CA-valid until it expires.
            await supersede(
                db,
                node_id=node_id,
                serial=replaced,
                not_after=node.pending_cert_not_after,
            )
        key_rotated = key_fp != presented.public_key_fingerprint
        await db.execute(
            update(Node)
            .where(Node.node_id == node_id)
            .values(
                pending_cert_serial=serial,
                pending_cert_fingerprint=pki.cert_fingerprint(cert),
                pending_cert_not_after=cert.not_valid_after_utc,
                pending_public_key_fingerprint=key_fp,
                cert_renewed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await audit(
            db,
            AuditAction.CERT_RENEWED,
            actor=ACTOR,
            node_id=node_id,
            source=source,
            details={
                "serial": serial,
                "presented_serial": presented.serial,
                "replaced_pending": replaced,
                "activated_presented": activated_first,
                "key_rotated": key_rotated,
                "not_after": cert.not_valid_after_utc.isoformat(),
            },
        )
        return RenewResult(
            node_id=node_id,
            serial=serial,
            not_after=cert.not_valid_after_utc,
            node_cert_pem=pki.cert_to_pem(cert),
            ca_cert_pem=pki.cert_to_pem(self._fabric.cert),
            key_rotated=key_rotated,
            replaced_pending=replaced,
        )

    async def _activate_locked(
        self, db: AsyncSession, node: Node, serial: str
    ) -> tuple[str | None, bool]:
        """Pending ``serial`` -> current on the locked ``node`` row, previous
        serial superseded, ``cert_activated`` audit row (same transaction).
        Returns ``(previous serial, key rotated)``."""
        node_id = node.node_id
        previous = node.cert_serial
        if previous is not None and node.cert_not_after is not None:
            await supersede(
                db, node_id=node_id, serial=previous, not_after=node.cert_not_after
            )
        key_rotated = node.pending_public_key_fingerprint != node.public_key_fingerprint
        await db.execute(
            update(Node)
            .where(Node.node_id == node_id)
            .values(
                cert_serial=node.pending_cert_serial,
                cert_fingerprint=node.pending_cert_fingerprint,
                cert_not_after=node.pending_cert_not_after,
                public_key_fingerprint=node.pending_public_key_fingerprint,
                pending_cert_serial=None,
                pending_cert_fingerprint=None,
                pending_cert_not_after=None,
                pending_public_key_fingerprint=None,
            )
            .execution_options(synchronize_session=False)
        )
        await audit(
            db,
            AuditAction.CERT_ACTIVATED,
            actor=ACTOR,
            node_id=node_id,
            details={
                "serial": serial,
                "superseded_serial": previous,
                "key_rotated": key_rotated,
            },
        )
        return previous, key_rotated

    async def _audit_refusal(
        self, presented: PresentedCert, refused: RenewRefused, source: str | None
    ) -> None:
        try:
            async with self._sessions() as db, db.begin():
                await audit(
                    db,
                    AuditAction.CERT_RENEW_REFUSED,
                    actor=ACTOR,
                    node_id=presented.node_id,
                    source=source,
                    details={
                        "reason": str(refused.reason),
                        "presented_serial": presented.serial,
                    },
                )
        except SQLAlchemyError as exc:
            log.warning(
                "controller.cert.audit_failed",
                reason=str(refused.reason),
                cause=type(exc).__name__,
            )

    # --------------------------------------------------------------- activate

    async def activate(self, node_id: str, serial: str) -> str | None:
        """Make the pending certificate ``serial`` of ``node_id`` current (see
        module docstring, "Activate"). Returns the superseded previous serial
        (``""`` when the node had none), or ``None`` when ``serial`` is not
        the node's pending certificate (nothing changed). Database errors
        propagate (admission turns them into ``INTERNAL``)."""
        async with self._sessions() as db, db.begin():
            node = await db.scalar(
                select(Node)
                .where(Node.node_id == node_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                node is None
                or node.pending_cert_serial != serial
                or node.state == NodeState.REVOKED.value
            ):
                return None
            previous, key_rotated = await self._activate_locked(db, node, serial)
        self.activated += 1
        log.info(
            "controller.cert.activated",
            node_id=node_id,
            serial=serial,
            superseded_serial=previous,
            key_rotated=key_rotated,
        )
        return previous or ""
