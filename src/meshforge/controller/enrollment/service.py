"""One enrolment: join token + CSR -> node certificate (V0.7 G2, 0.7.4, D136).

:meth:`Enroller.enroll` checks, in this order, and refuses with a typed
:class:`EnrollRefused` (a stable ``reason`` code; never the token or the CSR in
any message):

1. the request's shape (token 16 bytes, CSR <= 4096 bytes) - no I/O;
2. the CSR (:func:`pki.verify_csr`): P-256 key, SHA-256, a signature by that key
   (the author holds it), exactly one SAN URI naming **this** fabric and a
   ``node_id`` - no I/O, so a bad CSR never touches the database;
3. then **one transaction**:

   a. redeem the token (``UPDATE ... RETURNING``, single-use under races);
   b. a ``--replace`` invite may only re-key the node it names;
   c. the node row (inserted as ``enrolling`` if the controller never saw this
      ``node_id``) is locked; a revoked node is refused; a node already enrolled
      with **another key** is refused unless the invite replaces it;
   d. the certificate is issued (30 days, SAN URI, EKU client+server) and its
      serial / fingerprints / expiry recorded on the node; an ``enrolled`` audit
      row is added.

   Any refusal or failure inside rolls the whole transaction back: **the code is
   only consumed if a certificate is issued** (a refused or failed enrolment
   leaves it usable).

Refusals that name a real invite (used, revoked, expired, replace mismatch, key
conflict, revoked node) also write an ``enroll_refused`` audit row in a separate
transaction; unknown tokens and malformed requests are only logged (anyone can
send them; the rate limit bounds how often).

Known limit (THREAT_MODEL EN-S3, accepted): a node from before V0.7 G2 has no
key on record, so its first enrolment binds whatever key the invite holder
presents (trust on first enrolment; invites are admin-issued and single-use).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common import join_code, pki
from meshforge.common.logging import get_logger
from meshforge.controller.enrollment.invites import audit, redeem, why_unusable
from meshforge.controller.fabric_ca import FabricCa
from meshforge.controller.liveness import NodeState
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.enrollment import AuditAction
from meshforge.controller.store.models.node import Node

__all__ = [
    "ACTOR",
    "EnrollReason",
    "EnrollRefused",
    "EnrollResult",
    "Enroller",
    "public_key_fingerprint",
]

log = get_logger(__name__)

ACTOR: Final = "enroll"


class EnrollReason(StrEnum):
    """Stable refusal codes (gRPC trailing metadata ``meshforge-reason``)."""

    MALFORMED_REQUEST = "malformed_request"
    TOKEN_UNKNOWN = "token_unknown"  # noqa: S105 - a reason code
    TOKEN_USED = "token_used"  # noqa: S105 - a reason code
    TOKEN_REVOKED = "token_revoked"  # noqa: S105 - a reason code
    TOKEN_EXPIRED = "token_expired"  # noqa: S105 - a reason code
    REPLACE_MISMATCH = "replace_mismatch"
    NODE_KEY_CONFLICT = "node_key_conflict"
    NODE_REVOKED = "node_revoked"
    NO_FABRIC = "no_fabric"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    INTERNAL = "internal"
    # CSR refusals are ``csr_<PkiReason>`` (csr_bad_signature, csr_wrong_curve...).


_TOKEN_REASONS: Final = {
    "token_unknown": EnrollReason.TOKEN_UNKNOWN,
    "token_used": EnrollReason.TOKEN_USED,
    "token_revoked": EnrollReason.TOKEN_REVOKED,
    "token_expired": EnrollReason.TOKEN_EXPIRED,
}


class EnrollRefused(Exception):  # noqa: N818 - a refusal, not a bug
    """A refused enrolment. ``reason`` is a stable code; ``message`` is safe to
    send to the caller (it never contains the token or the CSR)."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        invite_id: str | None = None,
        node_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.invite_id = invite_id
        self.node_id = node_id


@dataclass(frozen=True, slots=True)
class EnrollResult:
    node_id: str
    fabric_id: str
    node_cert_pem: bytes
    ca_cert_pem: bytes
    serial: str
    not_after: dt.datetime
    invite_id: str
    key_replaced: bool


def public_key_fingerprint(key: ec.EllipticCurvePublicKey) -> str:
    """Hex SHA-256 of a public key's DER SubjectPublicKeyInfo (= the agent's
    ``node.json`` ``key_fingerprint_sha256``, ADR-0013)."""
    spki = key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(spki).hexdigest()


class _Refusal(Exception):  # noqa: N818 - internal control flow inside the txn
    def __init__(self, refused: EnrollRefused) -> None:
        super().__init__(refused.message)
        self.refused = refused


class Enroller:
    """Issues node certificates for valid join tokens (see module docstring)."""

    def __init__(self, engine: AsyncEngine, fabric: FabricCa) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )
        self._fabric = fabric

    @property
    def fabric(self) -> FabricCa:
        return self._fabric

    async def enroll(
        self, token: bytes, csr_pem: bytes, *, source: str | None
    ) -> EnrollResult:
        """Redeem ``token`` for a certificate on ``csr_pem`` (or raise
        :class:`EnrollRefused`)."""
        if len(token) != join_code.TOKEN_BYTES or not csr_pem:
            raise EnrollRefused(
                EnrollReason.MALFORMED_REQUEST,
                "the request needs a 16-byte join token and a CSR",
            )
        try:
            verified = pki.verify_csr(csr_pem, fabric_id=self._fabric.fabric_id)
        except pki.PkiError as exc:
            raise EnrollRefused(
                f"csr_{exc.reason.value}", f"the certificate request: {exc.message}"
            ) from None
        try:
            return await self._enroll(token, csr_pem, verified, source)
        except _Refusal as refusal:
            refused = refusal.refused
            if refused.invite_id is not None:
                await self._audit_refusal(refused, source)
            raise refused from None
        except SQLAlchemyError as exc:
            log.warning(
                "controller.enroll.db_failed",
                node_id=verified.node_id,
                cause=type(exc).__name__,
            )
            raise EnrollRefused(
                EnrollReason.UNAVAILABLE,
                "the controller's database is unavailable; retry",
            ) from None

    async def _enroll(
        self,
        token: bytes,
        csr_pem: bytes,
        verified: pki.VerifiedCsr,
        source: str | None,
    ) -> EnrollResult:
        node_id = verified.node_id
        key_fp = public_key_fingerprint(verified.public_key)
        digest = join_code.token_hash(token)
        async with self._sessions() as db, db.begin():
            redeemed = await redeem(db, digest, node_id=node_id)
            if redeemed is None:
                reason, invite_id = await why_unusable(db, digest)
                raise _Refusal(
                    EnrollRefused(
                        _TOKEN_REASONS[reason],
                        _TOKEN_MESSAGES[reason],
                        invite_id=invite_id,
                        node_id=node_id,
                    )
                )
            invite_id, replace = redeemed
            if replace is not None and replace != node_id:
                raise _Refusal(
                    EnrollRefused(
                        EnrollReason.REPLACE_MISMATCH,
                        "this code was made to re-key another node "
                        "(`fabric invite --replace`); it cannot enrol this one",
                        invite_id=invite_id,
                        node_id=node_id,
                    )
                )
            node = await _lock_node(db, node_id)
            if node.state == NodeState.REVOKED.value:
                raise _Refusal(
                    EnrollRefused(
                        EnrollReason.NODE_REVOKED,
                        "this node was revoked from the fabric",
                        invite_id=invite_id,
                        node_id=node_id,
                    )
                )
            known = node.public_key_fingerprint
            key_replaced = known is not None and known != key_fp
            if key_replaced and replace != node_id:
                raise _Refusal(
                    EnrollRefused(
                        EnrollReason.NODE_KEY_CONFLICT,
                        f"{node_id} is already enrolled with another key; the "
                        "fabric admin must make a code with `fabric invite "
                        f"--replace {node_id}` if this laptop really is that node",
                        invite_id=invite_id,
                        node_id=node_id,
                    )
                )
            try:
                cert = pki.issue_node_cert(
                    self._fabric.authority,
                    csr_pem,
                    node_id=node_id,
                    fabric_id=self._fabric.fabric_id,
                )
            except pki.PkiError as exc:  # e.g. the CA itself expired meanwhile
                log.error(  # noqa: TRY400 - typed reason, no traceback needed
                    "controller.enroll.issue_failed",
                    node_id=node_id,
                    reason=exc.reason.value,
                )
                raise _Refusal(
                    EnrollRefused(
                        EnrollReason.INTERNAL,
                        "the controller could not issue a certificate",
                        node_id=node_id,
                    )
                ) from None
            serial = format(cert.serial_number, "x")
            await db.execute(
                update(Node)
                .where(Node.node_id == node_id)
                .values(
                    cert_serial=serial,
                    cert_fingerprint=pki.cert_fingerprint(cert),
                    cert_not_after=cert.not_valid_after_utc,
                    public_key_fingerprint=key_fp,
                )
                .execution_options(synchronize_session=False)
            )
            await audit(
                db,
                AuditAction.ENROLLED,
                actor=ACTOR,
                node_id=node_id,
                invite_id=invite_id,
                source=source,
                details={
                    "serial": serial,
                    "key_replaced": key_replaced,
                    "first_enrolment": known is None,
                },
            )
        log.info(
            "controller.enroll.accepted",
            node_id=node_id,
            invite_id=invite_id,
            serial=serial,
            not_after=cert.not_valid_after_utc.isoformat(),
            key_replaced=key_replaced,
            key_fingerprint=key_fp[:16],
        )
        return EnrollResult(
            node_id=node_id,
            fabric_id=self._fabric.fabric_id,
            node_cert_pem=pki.cert_to_pem(cert),
            ca_cert_pem=pki.cert_to_pem(self._fabric.cert),
            serial=serial,
            not_after=cert.not_valid_after_utc,
            invite_id=invite_id,
            key_replaced=key_replaced,
        )

    async def _audit_refusal(self, refused: EnrollRefused, source: str | None) -> None:
        try:
            async with self._sessions() as db, db.begin():
                await audit(
                    db,
                    AuditAction.ENROLL_REFUSED,
                    actor=ACTOR,
                    node_id=refused.node_id,
                    invite_id=refused.invite_id,
                    source=source,
                    details={"reason": str(refused.reason)},
                )
        except SQLAlchemyError as exc:
            log.warning(
                "controller.enroll.audit_failed",
                reason=str(refused.reason),
                cause=type(exc).__name__,
            )


_TOKEN_MESSAGES: Final = {
    "token_unknown": "this join code is not known to the controller (a code from "
    "another fabric, or mistyped in a way the checksum missed)",
    "token_used": "this join code was already used; if your last join timed out "
    "or was interrupted, the code may have been spent on it - ask for a new code",
    "token_revoked": "this join code was revoked; ask for a new one",
    "token_expired": "this join code has expired; ask for a new one",
}


async def _lock_node(db: AsyncSession, node_id: str) -> Node:
    """The node row, locked; inserted as ``enrolling`` when the controller has
    never seen this ``node_id`` (its first Hello fills the rest, §12.3)."""
    now = dt.datetime.now(dt.UTC)
    await db.execute(
        pg_insert(Node)
        .values(
            node_id=node_id,
            hostname="",
            os="",
            platform="",
            hardware={},
            state=NodeState.ENROLLING.value,
            first_seen_at=now,
            last_seen_at=now,
            session_epoch=0,
        )
        .on_conflict_do_nothing(index_elements=[Node.node_id])
    )
    node = await db.scalar(
        select(Node)
        .where(Node.node_id == node_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if node is None:  # pragma: no cover - inserted or present above, same txn
        raise SQLAlchemyError("node row vanished")
    return node
