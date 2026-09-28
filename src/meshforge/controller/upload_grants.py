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

Grants live in memory like fetch tokens (a controller restart invalidates them -
an attempt uploading across a restart ends ``output_error``; persistence is V0.6's
restart work), keyed by the SHA-256 of the token, bounded by ``capacity`` and
pruned on expiry (controller monotonic clock).
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
    ``blob_quota`` / ``byte_quota``."""

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

    def owns(self, token: str | None) -> bool:
        """True when ``token`` is an upload grant (live or not)."""
        return bool(token) and self._key(token or "") in self._grants

    def begin(self, token: str, digest: Digest, declared: int | None) -> UploadTicket:
        """Reserve room for one upload or raise :class:`GrantRefusal`."""
        key = self._key(token)
        grant = self._grants.get(key)
        if grant is None:
            raise GrantRefusal("unknown", "not an upload grant")
        if grant.expires_at <= self._clock():
            self._drop(key)
            raise GrantRefusal("expired", "the upload grant expired")
        if grant.closed:
            raise GrantRefusal("closed", "the attempt already ended")
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
        if grant is None or ticket.free:
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
