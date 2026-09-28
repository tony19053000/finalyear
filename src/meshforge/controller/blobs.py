"""The controller's blob store and per-attempt fetch tokens (brief D48, ADR-0003).

**Store.** Immutable blobs named by SHA-256 under ``<root>/sha256/<2 hex>/<64 hex>``
- ADR-0003's layout from day one, so V0.5 adds chunking, peers and GC without
moving data. An upload streams into ``<root>/tmp/<random>.part`` while it is hashed
and counted; only when the stream ended, the size is within the cap and the digest
matches the name is the file fsync'ed and renamed into place (**verify, then
rename**). Anything else - a wrong digest, an oversized body, a client that went
away, a cancelled request, a full disk - deletes the part file, so a half-written
blob can never appear under a real name. The rename is atomic, which makes
concurrent uploads of one digest safe (both write identical bytes; the second
rename replaces the first with the same content). An upload of a digest that
already exists is still hashed (a client sending other bytes under that name is
told ``integrity_error``) but nothing is written: idempotent, ``created=False``.
Part files left by a crash are swept at start (:meth:`BlobStore.prepare`).

**Fetch tokens.** An agent must never hold the fabric token (ADR-0014 §3). For each
attempt the controller mints a random token bound to that attempt's digests and a
lifetime; G4 hands it to the agent in ``StartAttempt`` and calls
:meth:`FetchTokens.expire_after` when the attempt ends. A token may also allow
*uploads* (outputs, D54) - of any digest, still size-capped. Tokens live in memory
(a controller restart invalidates them; G4 re-mints on re-dispatch), keyed by the
SHA-256 of the token so the table never holds a usable secret, bounded by
``capacity`` (oldest dropped) and pruned on expiry. Time is the controller's
monotonic clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import hmac
import os
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from pydantic import SecretStr

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger

__all__ = [
    "MAX_FETCH_TOKEN_DIGESTS",
    "MAX_FETCH_TOKEN_TTL_S",
    "BlobError",
    "BlobIntegrityError",
    "BlobNotFoundError",
    "BlobStore",
    "BlobTooLargeError",
    "FetchGrant",
    "FetchTokens",
    "PutResult",
]

log = get_logger(__name__)

MAX_FETCH_TOKEN_TTL_S: Final = 7 * 24 * 3600 + 3600
"""The longest job limit (7 days) plus an hour of grace."""
MAX_FETCH_TOKEN_DIGESTS: Final = 1024
_TMP_DIR: Final = "tmp"
_ALGO_DIR: Final = "sha256"


class BlobError(MeshForgeError):
    """Base of blob-store failures."""


class BlobTooLargeError(BlobError):
    default_failure_type = FailureType.INVALID_INPUT


class BlobIntegrityError(BlobError):
    """The uploaded bytes do not hash to the name they were sent under."""

    default_failure_type = FailureType.INTEGRITY_ERROR


class BlobNotFoundError(BlobError):
    default_failure_type = FailureType.NOT_FOUND


@dataclass(frozen=True, slots=True)
class PutResult:
    digest: Digest
    size: int
    created: bool
    """False when the blob already existed (nothing was written)."""


class BlobStore:
    """See module docstring. Safe for concurrent use from one event loop."""

    def __init__(self, root: Path, *, max_blob_bytes: int) -> None:
        self.root = root
        self.max_blob_bytes = max_blob_bytes
        self._tmp = root / _TMP_DIR

    # ------------------------------------------------------------- layout

    def prepare(self) -> int:
        """Create the layout and delete part files left by a crash (blocking).

        Returns how many part files were swept.
        """
        (self.root / _ALGO_DIR).mkdir(parents=True, exist_ok=True)
        self._tmp.mkdir(parents=True, exist_ok=True)
        swept = 0
        for leftover in self._tmp.glob("*.part"):
            with contextlib.suppress(OSError):
                leftover.unlink()
                swept += 1
        return swept

    def path_for(self, digest: Digest) -> Path:
        return self.root / _ALGO_DIR / digest.hex[:2] / digest.hex

    def size(self, digest: Digest) -> int | None:
        """Size of the stored blob, or ``None`` when it is not here."""
        try:
            return self.path_for(digest).stat().st_size
        except FileNotFoundError:
            return None

    def open_path(self, digest: Digest) -> Path:
        path = self.path_for(digest)
        if not path.is_file():
            raise BlobNotFoundError("no such blob", details={"digest": str(digest)})
        return path

    # ---------------------------------------------------------------- put

    async def put(
        self,
        digest: Digest,
        chunks: AsyncIterator[bytes],
        *,
        declared_size: int | None = None,
    ) -> PutResult:
        """Store the streamed bytes under ``digest`` (see module docstring).

        Raises :class:`BlobTooLargeError`, :class:`BlobIntegrityError`, or a
        :class:`BlobError` typed ``disk_full`` / ``transfer_error``. Whatever the
        exception (including cancellation), no part file is left behind.
        """
        if declared_size is not None and declared_size > self.max_blob_bytes:
            raise BlobTooLargeError(
                f"blob larger than the {self.max_blob_bytes}-byte limit",
                details={"digest": str(digest), "declared": declared_size},
            )
        exists = self.size(digest) is not None
        hasher = hashlib.sha256()
        count = 0
        part: Path | None = None
        fh: BinaryIO | None = None
        try:
            if not exists:
                part = self._tmp / f"{secrets.token_hex(12)}.part"
                fh = part.open("xb")
            async for chunk in chunks:
                if not chunk:
                    continue
                count += len(chunk)
                if count > self.max_blob_bytes:
                    raise BlobTooLargeError(
                        f"blob larger than the {self.max_blob_bytes}-byte limit",
                        details={"digest": str(digest)},
                    )
                hasher.update(chunk)
                if fh is not None:
                    # Page-cache writes are microseconds; the fsync below is the
                    # slow part and runs in a thread.
                    fh.write(chunk)
            if declared_size is not None and count != declared_size:
                raise BlobError(
                    "upload ended early (body shorter than Content-Length)",
                    failure_type=FailureType.TRANSFER_ERROR,
                    details={"digest": str(digest)},
                )
            actual = hasher.hexdigest()
            if not hmac.compare_digest(actual, digest.hex):
                raise BlobIntegrityError(
                    "uploaded bytes do not match the digest they were sent under",
                    details={"expected": str(digest), "actual": f"sha256:{actual}"},
                )
            if fh is not None and part is not None:
                await asyncio.to_thread(self._commit, fh, part, digest)
                fh = None
                part = None
                log.info("controller.blobs.stored", digest=str(digest), size=count)
                return PutResult(digest=digest, size=count, created=True)
            return PutResult(digest=digest, size=count, created=False)
        except OSError as exc:
            if exc.errno == errno.ENOSPC:
                raise BlobError(
                    "the controller's disk is full",
                    failure_type=FailureType.DISK_FULL,
                    details={"digest": str(digest)},
                ) from None
            raise
        finally:
            if fh is not None:
                with contextlib.suppress(OSError):
                    fh.close()
            if part is not None:
                with contextlib.suppress(OSError):
                    part.unlink()

    def _commit(self, fh: BinaryIO, part: Path, digest: Digest) -> None:
        """fsync, close, rename into place (blocking; runs in a thread)."""
        try:
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fh.close()
        final = self.path_for(digest)
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, final)
        if os.name == "posix":
            fd = os.open(final.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


# ================================================================ fetch tokens


@dataclass(frozen=True, slots=True)
class FetchGrant:
    attempt_id: str
    digests: frozenset[str]
    """Digest hex strings this token may read."""
    allow_upload: bool
    expires_at: float
    """Controller monotonic time."""


class FetchTokens:
    """Mint and check per-attempt blob tokens (see module docstring)."""

    def __init__(self, *, clock: Callable[[], float], capacity: int = 10_000) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._clock = clock
        self._capacity = capacity
        self._grants: OrderedDict[str, FetchGrant] = OrderedDict()
        self.evicted = 0

    def __len__(self) -> int:
        return len(self._grants)

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8", "surrogateescape")).hexdigest()

    def _prune(self, now: float) -> None:
        expired = [k for k, g in self._grants.items() if g.expires_at <= now]
        for key in expired:
            del self._grants[key]

    def mint(
        self,
        attempt_id: str,
        digests: Iterable[Digest],
        *,
        ttl_s: float,
        allow_upload: bool = False,
    ) -> SecretStr:
        """A new token for ``attempt_id`` that may GET/HEAD exactly ``digests``."""
        if not 0 < ttl_s <= MAX_FETCH_TOKEN_TTL_S:
            raise ValueError(f"ttl_s must be in (0, {MAX_FETCH_TOKEN_TTL_S}]")
        allowed = frozenset(d.hex for d in digests)
        if len(allowed) > MAX_FETCH_TOKEN_DIGESTS:
            raise ValueError(f"at most {MAX_FETCH_TOKEN_DIGESTS} digests per token")
        now = self._clock()
        self._prune(now)
        token = secrets.token_urlsafe(32)
        self._grants[self._key(token)] = FetchGrant(
            attempt_id=attempt_id,
            digests=allowed,
            allow_upload=allow_upload,
            expires_at=now + ttl_s,
        )
        while len(self._grants) > self._capacity:
            self._grants.popitem(last=False)
            self.evicted += 1
            log.warning("controller.blobs.fetch_token_evicted", capacity=self._capacity)
        return SecretStr(token)

    def check(self, token: str | None, digest: Digest, *, upload: bool) -> bool:
        """True when ``token`` is live and allows this read (or upload)."""
        if not token:
            return False
        grant = self._grants.get(self._key(token))
        if grant is None:
            return False
        if grant.expires_at <= self._clock():
            self._grants.pop(self._key(token), None)
            return False
        if upload:
            return grant.allow_upload
        return digest.hex in grant.digests

    def expire_after(self, attempt_id: str, grace_s: float) -> int:
        """Shorten every token of ``attempt_id`` to ``now + grace_s``; returns count."""
        deadline = self._clock() + max(grace_s, 0.0)
        changed = 0
        for key, grant in list(self._grants.items()):
            if grant.attempt_id == attempt_id and grant.expires_at > deadline:
                self._grants[key] = FetchGrant(
                    attempt_id=grant.attempt_id,
                    digests=grant.digests,
                    allow_upload=grant.allow_upload,
                    expires_at=deadline,
                )
                changed += 1
        return changed

    def revoke(self, attempt_id: str) -> int:
        """Invalidate every token of ``attempt_id`` now; returns count."""
        keys = [k for k, g in self._grants.items() if g.attempt_id == attempt_id]
        for key in keys:
            del self._grants[key]
        return len(keys)
