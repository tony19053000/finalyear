"""Per-attempt upload grants: what an agent may write to the blob store (V0.4 D73).

The fetch token an agent gets in ``StartAttempt`` is **download-only** (its bundle
digest). Uploads - the packed outputs and the two final log files - use a separate
**upload grant**, minted with the attempt:

* at most ``max_blobs`` blobs and ``max_bytes`` bytes in total per attempt. Space
  is **reserved** from the request's ``Content-Length`` before a byte is read
  (a grant upload without one is refused), so concurrent uploads cannot overshoot
  the quota together; a failed upload gives its reservation back;
* re-uploading a digest this grant already stored is free (a retry after a
  network error must not burn quota);
* only while the attempt is live: :meth:`UploadGrants.close` at the attempt's end
  refuses every later ``PUT`` (an ended attempt cannot poison the store);
* the controller accepts a terminal report's ``output_digest`` / log digests only
  if :meth:`UploadGrants.uploaded` says this attempt's grant stored and verified
  that blob - a digest merely *named* in a report proves nothing.

Grants live in memory like fetch tokens, keyed by the SHA-256 of the token,
bounded by ``capacity`` and pruned on expiry (controller monotonic clock). A
controller restart forgets them; after the agent's Reconcile the dispatcher
mints a new grant for every held attempt that has none here and sends it in
``RefreshUploadGrant`` (V0.6 G1, D114). A re-mint keeps what the attempt
already stored, and an upload still in flight under the replaced grant is
credited to its successor when it finishes (never after a revoke).

**Resumable uploads under a grant (V0.6 G1, D114).** The agent uploads through
the parts API (``PUT /v1/blobs/{d}/parts/{i}``, ``GET .../parts``, ``POST
.../complete``) so an upload interrupted by a restart resumes from the parts the
controller already stored. :meth:`UploadGrants.begin_parts` reserves the whole
blob once per digest (later parts reuse it); :meth:`UploadGrants.credit` records
the blob as uploaded under the grant only when a ``complete`` call **assembled
and verified** it. A ``complete`` that finds the blob already stored proves
nothing about this attempt: the grant is not credited (the answer says
``credited: false``) and the agent uploads the bytes again with ``PUT``. Parts
received before a restart count for the new grant once it completes them: the
bytes are verified against the digest at completion, but which credential sent
each part is not recorded (parts outlive a restart, their uploader does not).
"""

from __future__ import annotations

import hashlib
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from pydantic import SecretStr

from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger

__all__ = [
    "GrantRefusal",
    "UploadGrant",
    "UploadGrants",
    "UploadTicket",
]

log = get_logger(__name__)

_MAX_TTL_S: Final = 7 * 24 * 3600 + 3600


class GrantRefusal(Exception):  # noqa: N818 - a refusal, not a crash
    """The grant does not allow this upload. ``reason`` is a stable code:
    ``unknown`` / ``expired`` / ``closed`` / ``length_required`` /
    ``blob_quota`` / ``byte_quota`` / ``size_mismatch`` (D114 parts)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(slots=True)
class UploadGrant:
    attempt_id: str
    max_blobs: int
    max_bytes: int
    expires_at: float
    uploaded: dict[str, int] = field(default_factory=dict)
    """Digest hex -> size of blobs stored and verified under this grant."""
    reserved_bytes: int = 0
    pending: dict[str, int] = field(default_factory=dict)
    """Digest hex -> bytes reserved by an upload in flight."""
    closed: bool = False

    @property
    def used_bytes(self) -> int:
        return sum(self.uploaded.values()) + self.reserved_bytes

    @property
    def used_blobs(self) -> int:
        return len(self.uploaded) + len(self.pending)


@dataclass(frozen=True, slots=True)
class UploadTicket:
    """One reserved upload; hand it back to :meth:`UploadGrants.finish`."""

    key: str
    attempt_id: str
    digest: str
    reserved: int
    free: bool
    """The digest was already uploaded under this grant: nothing reserved."""


class UploadGrants:
    """Mint and enforce upload grants (see module docstring)."""

    def __init__(self, *, clock: Callable[[], float], capacity: int = 10_000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._clock = clock
        self._capacity = capacity
        self._grants: OrderedDict[str, UploadGrant] = OrderedDict()
        self._by_attempt: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._grants)

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8", "surrogateescape")).hexdigest()

    def _prune(self, now: float) -> None:
        for key, grant in list(self._grants.items()):
            if grant.expires_at <= now:
                self._drop(key)

    def _drop(self, key: str) -> None:
        grant = self._grants.pop(key, None)
        if grant is not None and self._by_attempt.get(grant.attempt_id) == key:
            del self._by_attempt[grant.attempt_id]

    # ------------------------------------------------------------- lifecycle

    def mint(
        self, attempt_id: str, *, ttl_s: float, max_blobs: int, max_bytes: int
    ) -> SecretStr:
        """A new grant for ``attempt_id`` (replacing any earlier one of it)."""
        if not 0 < ttl_s <= _MAX_TTL_S:
            raise ValueError(f"ttl_s must be in (0, {_MAX_TTL_S}]")
        if max_blobs < 1 or max_bytes < 1:
            raise ValueError("max_blobs and max_bytes must be positive")
        now = self._clock()
        self._prune(now)
        previous = self._by_attempt.get(attempt_id)
        carried: dict[str, int] = {}
        if previous is not None:
            # A re-send of the same attempt (new token): what it already stored
            # stays stored - and still counts.
            old = self._grants[previous]
            carried = dict(old.uploaded)
            self._drop(previous)
        token = secrets.token_urlsafe(32)
        key = self._key(token)
        self._grants[key] = UploadGrant(
            attempt_id=attempt_id,
            max_blobs=max_blobs,
            max_bytes=max_bytes,
            expires_at=now + ttl_s,
            uploaded=carried,
        )
        self._by_attempt[attempt_id] = key
        while len(self._grants) > self._capacity:
            oldest = next(iter(self._grants))
            self._drop(oldest)
            log.warning(
                "controller.blobs.upload_grant_evicted", capacity=self._capacity
            )
        return SecretStr(token)

    def close(self, attempt_id: str, grace_s: float = 0.0) -> None:
        """The attempt ended: no new upload starts; the record of what it uploaded
        is kept ``grace_s`` longer (a replayed terminal report is checked on it)."""
        key = self._by_attempt.get(attempt_id)
        if key is None:
            return
        grant = self._grants[key]
        grant.closed = True
        grant.expires_at = min(grant.expires_at, self._clock() + max(grace_s, 0.0))

    def revoke(self, attempt_id: str) -> None:
        key = self._by_attempt.get(attempt_id)
        if key is not None:
            self._drop(key)

    # --------------------------------------------------------------- uploads

    def has(self, attempt_id: str) -> bool:
        """Does ``attempt_id`` hold a usable grant in this process (D114)?"""
        key = self._by_attempt.get(attempt_id)
        if key is None:
            return False
        grant = self._grants[key]
        return not grant.closed and grant.expires_at > self._clock()

    def _live(self, token: str) -> tuple[str, UploadGrant]:
        key = self._key(token)
        grant = self._grants.get(key)
        if grant is None:
            raise GrantRefusal("unknown", "not an upload grant")
        if grant.expires_at <= self._clock():
            self._drop(key)
            raise GrantRefusal("expired", "the upload grant expired")
        if grant.closed:
            raise GrantRefusal("closed", "the attempt already ended")
        return key, grant

    def check(self, token: str) -> str:
        """The attempt of a usable grant (raises :class:`GrantRefusal`)."""
        return self._live(token)[1].attempt_id

    def begin_parts(self, token: str, digest: Digest, size: int) -> UploadTicket:
        """D114: reserve ``size`` bytes for a resumable upload of ``digest``
        once; a later part (or ``complete``) of the same digest reuses it."""
        key, grant = self._live(token)
        if digest.hex in grant.uploaded:
            return UploadTicket(key, grant.attempt_id, digest.hex, 0, free=True)
        if digest.hex in grant.pending:
            reserved = grant.pending[digest.hex]
            if reserved != size:
                # The reservation is the blob's size: a part declaring another
                # size would write past what the quota reserved.
                raise GrantRefusal(
                    "size_mismatch",
                    "this upload was reserved with another blob size",
                )
            return UploadTicket(key, grant.attempt_id, digest.hex, reserved, False)
        if grant.used_blobs + 1 > grant.max_blobs:
            raise GrantRefusal(
                "blob_quota",
                f"this attempt may upload at most {grant.max_blobs} blobs",
            )
        if grant.used_bytes + size > grant.max_bytes:
            raise GrantRefusal(
                "byte_quota",
                f"this attempt may upload at most {grant.max_bytes} bytes",
            )
        grant.pending[digest.hex] = size
        grant.reserved_bytes += size
        return UploadTicket(key, grant.attempt_id, digest.hex, size, free=False)

    def credit(self, ticket: UploadTicket, size: int) -> None:
        """D114: a ``complete`` assembled and verified the blob: it counts as
        uploaded under this grant (its reservation becomes the stored size)."""
        grant = self._grants.get(ticket.key)
        if ticket.free:
            return
        if grant is None:
            self._credit_successor(ticket, size)
            return
        reserved = grant.pending.pop(ticket.digest, None)
        if reserved is not None:
            grant.reserved_bytes -= reserved
        grant.uploaded[ticket.digest] = size

    def _credit_successor(self, ticket: UploadTicket, size: int) -> None:
        """The ticket's grant was replaced while its upload was in flight (a
        refresh minted for the same attempt, D114): the verified blob counts for
        the attempt's current grant. A revoked attempt has none - nothing."""
        key = self._by_attempt.get(ticket.attempt_id)
        successor = None if key is None else self._grants.get(key)
        if successor is not None:
            successor.uploaded[ticket.digest] = size

    def release(self, ticket: UploadTicket) -> None:
        """Give a resumable reservation back (the blob was not credited)."""
        grant = self._grants.get(ticket.key)
        if grant is None or ticket.free:
            return
        reserved = grant.pending.pop(ticket.digest, None)
        if reserved is not None:
            grant.reserved_bytes -= reserved

    def begin(self, token: str, digest: Digest, declared: int | None) -> UploadTicket:
        """Reserve room for one upload or raise :class:`GrantRefusal`."""
        key, grant = self._live(token)
        if digest.hex in grant.uploaded:
            return UploadTicket(key, grant.attempt_id, digest.hex, 0, free=True)
        if declared is None:
            raise GrantRefusal(
                "length_required", "an upload under a grant needs Content-Length"
            )
        if digest.hex in grant.pending or grant.used_blobs + 1 > grant.max_blobs:
            raise GrantRefusal(
                "blob_quota",
                f"this attempt may upload at most {grant.max_blobs} blobs",
            )
        if grant.used_bytes + declared > grant.max_bytes:
            raise GrantRefusal(
                "byte_quota",
                f"this attempt may upload at most {grant.max_bytes} bytes",
            )
        grant.pending[digest.hex] = declared
        grant.reserved_bytes += declared
        return UploadTicket(key, grant.attempt_id, digest.hex, declared, free=False)

    def finish(self, ticket: UploadTicket, *, stored: bool, size: int = 0) -> None:
        """Settle a reservation: ``stored`` = the blob is in place and verified."""
        grant = self._grants.get(ticket.key)
        if ticket.free:
            return
        if grant is None:
            if stored:
                self._credit_successor(ticket, size)
            return
        if grant.pending.pop(ticket.digest, None) is not None:
            grant.reserved_bytes -= ticket.reserved
        if stored:
            grant.uploaded[ticket.digest] = size

    def uploaded(self, attempt_id: str, digest: Digest | str) -> bool:
        """Did this attempt's grant store and verify ``digest``?"""
        key = self._by_attempt.get(attempt_id)
        if key is None:
            return False
        text = digest.hex if isinstance(digest, Digest) else Digest.parse(digest).hex
        return text in self._grants[key].uploaded
