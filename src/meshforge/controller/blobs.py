"""The controller's blob store and per-attempt fetch tokens (D48, ADR-0003, V0.5 D79-D82).

**Store.** A :class:`~meshforge.common.cas.CasStore` (the same module agents use):
immutable whole-file blobs named by SHA-256 under ``<root>/sha256/<aa>/<bb>/<hex>``
(V0.4's one-level layout is migrated at :meth:`BlobStore.prepare`). An upload
streams into a part file in ``<root>/tmp/`` while it is hashed and counted; only
when the stream ended, the size is within the cap and the digest matches the name
is it fsync'ed and renamed into place (**verify, then rename**), then made
read-only. Anything else - a wrong digest, an oversized body, a client that went
away, a cancelled request, a full disk - deletes the part file, so a half-written
blob can never appear under a real name. An upload of a digest that already exists
is still hashed (a client sending other bytes under that name is told
``integrity_error``) but nothing is written: idempotent, ``created=False``.

**Have/need (D82).** :meth:`BlobStore.missing` answers which of a bounded list of
digests are absent, so a client uploads only those.

**Resumable part uploads (D80/D82).** A large file is sent as chunk-sized parts
(:meth:`BlobStore.put_part`), each verified against the chunk digest the client
declares (from its tree manifest) and written at its offset; a bitmap sidecar
remembers what arrived, so an interrupted upload resumes with the missing parts
only (:meth:`BlobStore.part_status`). :meth:`BlobStore.complete_parts` hashes the
whole file and installs it only if it matches its name. Parts of one digest are
serialised by a per-digest lock; resumable transfers older than a day are swept
at start.

**Trees (D81).** :meth:`BlobStore.tree` loads and validates a stored tree manifest
(cached, bounded); :meth:`BlobStore.tree_members` is what a tree-scoped fetch
token may read.

**Admission (D73).** At most ``max_concurrent_uploads`` uploads (whole blobs or
parts) stream at once (a further one waits up to ``upload_wait_s``, then is
refused ``resource_busy``), and an upload that would leave less than
``free_disk_floor_bytes`` free on the store's disk is refused ``disk_full`` before
it writes (checked again every 64 MiB while it streams).

**Fetch tokens.** An agent must never hold the fabric token (ADR-0014 §3). For each
attempt the controller mints a random token bound to that attempt's digests and a
lifetime. A digest that names a **tree manifest** also grants the blobs that tree
lists (V0.5 D83: the scope of a tree job's token is "this tree's blobs"). A fetch
token is **download-only** (D73): uploads use the attempt's quota-bound upload
grant (:mod:`meshforge.controller.upload_grants`). Tokens live in memory (a
controller restart invalidates them; G4 re-mints on re-dispatch), keyed by the
SHA-256 of the token so the table never holds a usable secret, bounded by
``capacity`` (oldest dropped) and pruned on expiry. Time is the controller's
monotonic clock.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from meshforge.common.cas import (
    CasError,
    CasFullError,
    CasIntegrityError,
    CasStore,
    CasTooLargeError,
    chunk_count,
)
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.tree import Tree, TreeError, TreeLimits, parse_tree

__all__ = [
    "MAX_FETCH_TOKEN_DIGESTS",
    "MAX_FETCH_TOKEN_TTL_S",
    "MAX_HAVE_DIGESTS",
    "BlobError",
    "BlobIntegrityError",
    "BlobNotFoundError",
    "BlobStore",
    "BlobTooLargeError",
    "FetchGrant",
    "FetchTokens",
    "PartStatus",
    "PutResult",
]

log = get_logger(__name__)

MAX_FETCH_TOKEN_TTL_S: Final = 7 * 24 * 3600 + 3600
"""The longest job limit (7 days) plus an hour of grace."""
MAX_FETCH_TOKEN_DIGESTS: Final = 1024
MAX_HAVE_DIGESTS: Final = 10_000
"""Digests one have/need call may ask about (the client batches above this)."""
_TREE_CACHE: Final = 64
_ZSTD_MAGIC: Final = b"\x28\xb5\x2f\xfd"


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


@dataclass(frozen=True, slots=True)
class PartStatus:
    size: int
    chunk_size: int
    received: tuple[int, ...]
    complete: bool
    """True when the blob is already stored (no parts needed)."""


def _translate(exc: CasError, digest: Digest) -> BlobError:
    details = {**exc.details, "digest": str(digest)}
    if isinstance(exc, CasTooLargeError):
        return BlobTooLargeError(exc.message, details=details)
    if isinstance(exc, CasIntegrityError):
        return BlobIntegrityError(exc.message, details=details)
    if isinstance(exc, CasFullError):
        reason = exc.details.get("reason")
        if reason == "insufficient_storage":
            message = (
                "insufficient storage: the upload would leave the controller's disk "
                "below its free floor"
            )
        elif reason == "quota":
            message = "the controller's blob quota is full"
        else:
            message = "the controller's disk is full"
        return BlobError(message, failure_type=FailureType.DISK_FULL, details=details)
    return BlobError(exc.message, failure_type=exc.failure_type, details=details)


class BlobStore:
    """See module docstring. Safe for concurrent use from one event loop."""

    def __init__(
        self,
        root: Path,
        *,
        max_blob_bytes: int,
        max_concurrent_uploads: int = 8,
        upload_wait_s: float = 30.0,
        free_disk_floor_bytes: int = 0,
        disk_free: Callable[[Path], int] | None = None,
        tree_limits: TreeLimits | None = None,
    ) -> None:
        if max_concurrent_uploads < 1:
            raise ValueError("max_concurrent_uploads must be positive")
        self.cas = CasStore(
            root,
            max_blob_bytes=max_blob_bytes,
            free_disk_floor_bytes=free_disk_floor_bytes,
            disk_free=disk_free,
        )
        self.root = root
        self.max_blob_bytes = max_blob_bytes
        self.free_disk_floor_bytes = free_disk_floor_bytes
        self.tree_limits = tree_limits or TreeLimits()
        self._upload_wait_s = upload_wait_s
        self._uploads = asyncio.Semaphore(max_concurrent_uploads)
        self._part_locks: dict[str, asyncio.Lock] = {}
        self._trees: OrderedDict[str, Tree | None] = OrderedDict()
        self.uploads_refused_busy = 0
        self.parts_received = 0
        self.part_bytes_received = 0

    @property
    def uploads_refused_disk(self) -> int:
        return self.cas.refused_disk

    # ------------------------------------------------------------- layout

    def prepare(self) -> int:
        """Create the layout, sweep crash leftovers, migrate V0.4 blobs (blocking).

        Returns how many one-shot part files were swept.
        """
        report = self.cas.prepare()
        if report.migrated or report.expired_resumes:
            log.info(
                "controller.blobs.prepared",
                migrated=report.migrated,
                expired_resumes=report.expired_resumes,
            )
        return report.swept

    def path_for(self, digest: Digest) -> Path:
        return self.cas.path_for(digest)

    def size(self, digest: Digest) -> int | None:
        """Size of the stored blob, or ``None`` when it is not here."""
        return self.cas.size(digest)

    def open_path(self, digest: Digest) -> Path:
        path = self.path_for(digest)
        if not path.is_file():
            raise BlobNotFoundError("no such blob", details={"digest": str(digest)})
        return path

    def missing(self, digests: Iterable[Digest]) -> list[Digest]:
        """The digests (in order, de-duplicated) this store does not hold."""
        seen: set[str] = set()
        out: list[Digest] = []
        for digest in digests:
            if digest.hex in seen:
                continue
            seen.add(digest.hex)
            if not self.cas.has(digest):
                out.append(digest)
        return out

    # ---------------------------------------------------------------- put

    async def _slot(self, digest: Digest) -> None:
        try:
            await asyncio.wait_for(self._uploads.acquire(), self._upload_wait_s)
        except TimeoutError:
            self.uploads_refused_busy += 1
            raise BlobError(
                "the controller is busy with other uploads; retry shortly",
                failure_type=FailureType.RESOURCE_BUSY,
                details={"digest": str(digest)},
            ) from None

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
        await self._slot(digest)
        try:
            with self.cas.writer(digest, declared_size) as writer:
                async for chunk in chunks:
                    # Page-cache writes are microseconds; the fsync in commit is
                    # the slow part and runs in a thread.
                    writer.write(chunk)
                result = await asyncio.to_thread(writer.commit)
        except CasError as exc:
            raise _translate(exc, digest) from None
        finally:
            self._uploads.release()
        if result.created:
            log.info("controller.blobs.stored", digest=str(digest), size=result.size)
        return PutResult(digest=digest, size=result.size, created=result.created)

    # -------------------------------------------------------------- parts

    def _lock(self, digest: Digest) -> asyncio.Lock:
        lock = self._part_locks.get(digest.hex)
        if lock is None:
            lock = self._part_locks[digest.hex] = asyncio.Lock()
        return lock

    def _drop_lock(self, digest: Digest, lock: asyncio.Lock) -> None:
        if not lock.locked() and self._part_locks.get(digest.hex) is lock:
            del self._part_locks[digest.hex]

    def _check_shape(self, digest: Digest, size: int, chunk_size: int) -> None:
        if size > self.max_blob_bytes:
            raise BlobTooLargeError(
                f"blob larger than the {self.max_blob_bytes}-byte limit",
                details={"digest": str(digest), "declared": size},
            )
        try:
            chunk_count(size, chunk_size)
        except ValueError as exc:
            raise BlobError(str(exc), failure_type=FailureType.INVALID_INPUT) from None

    async def part_status(self, digest: Digest) -> PartStatus | None:
        """What a resumable upload of ``digest`` already holds (None = nothing)."""
        size = self.size(digest)
        if size is not None:
            return PartStatus(size=size, chunk_size=0, received=(), complete=True)
        _part, sidecar = self.cas.resume_paths(digest)
        if not sidecar.is_file():
            return None
        lock = self._lock(digest)
        try:
            async with lock:
                return await asyncio.to_thread(self._read_status, digest)
        finally:
            self._drop_lock(digest, lock)

    def _read_status(self, digest: Digest) -> PartStatus | None:
        _part, sidecar = self.cas.resume_paths(digest)
        try:
            data = json.loads(sidecar.read_bytes()[: 1 << 20])
            size, chunk_size = int(data["size"]), int(data["chunk_size"])
            part = self.cas.resumable(digest, size, chunk_size)
        except (OSError, ValueError, KeyError, TypeError, CasError):
            return None
        done = tuple(i for i in range(part.count) if part.is_done(i))
        return PartStatus(
            size=size, chunk_size=chunk_size, received=done, complete=False
        )

    async def put_part(
        self,
        digest: Digest,
        index: int,
        *,
        size: int,
        chunk_size: int,
        chunk_hex: str,
        body: AsyncIterator[bytes],
    ) -> bool:
        """Store part ``index`` of ``digest`` (verified against ``chunk_hex``).

        Returns False when the part (or the whole blob) was already here. Raises
        :class:`BlobError` subclasses like :meth:`put`.
        """
        self._check_shape(digest, size, chunk_size)
        await self._slot(digest)
        lock = self._lock(digest)
        try:
            data = bytearray()
            limit = chunk_size
            async for piece in body:
                data += piece
                if len(data) > limit:
                    raise BlobError(
                        "part larger than the chunk size",
                        failure_type=FailureType.INVALID_INPUT,
                        details={"digest": str(digest), "index": index},
                    )
            if self.size(digest) is not None:
                return False
            async with lock:
                part = await asyncio.to_thread(
                    self.cas.resumable, digest, size, chunk_size
                )
                stored = await asyncio.to_thread(
                    part.write_chunk, index, bytes(data), chunk_hex
                )
        except CasError as exc:
            raise _translate(exc, digest) from None
        except ValueError as exc:  # index out of range
            raise BlobError(str(exc), failure_type=FailureType.INVALID_INPUT) from None
        finally:
            self._uploads.release()
            self._drop_lock(digest, lock)
        if stored:
            self.parts_received += 1
            self.part_bytes_received += len(data)
        return stored

    async def complete_parts(self, digest: Digest) -> PutResult:
        """Hash the assembled parts and install the blob (see module docstring)."""
        if (existing := self.size(digest)) is not None:
            return PutResult(digest=digest, size=existing, created=False)
        status = await self.part_status(digest)
        if status is None:
            raise BlobNotFoundError(
                "no part upload of this blob is in progress",
                details={"digest": str(digest)},
            )
        lock = self._lock(digest)
        try:
            async with lock:
                part = await asyncio.to_thread(
                    self.cas.resumable, digest, status.size, status.chunk_size
                )
                missing = part.missing()
                if missing:
                    raise BlobError(
                        f"{len(missing)} part(s) still missing",
                        failure_type=FailureType.CONFLICT,
                        details={"digest": str(digest), "missing": missing[:20]},
                    )
                result = await asyncio.to_thread(part.complete)
        except CasIntegrityError:
            log.warning("controller.blobs.parts_integrity_error", digest=str(digest))
            raise BlobIntegrityError(
                "the assembled parts do not hash to the blob's name; the upload "
                "starts over",
                details={"digest": str(digest)},
            ) from None
        except CasError as exc:
            raise _translate(exc, digest) from None
        finally:
            self._drop_lock(digest, lock)
        log.info(
            "controller.blobs.stored", digest=str(digest), size=result.size, parts=True
        )
        return PutResult(digest=digest, size=result.size, created=result.created)

    # -------------------------------------------------------------- trees

    async def tree(self, tree_id: Digest) -> Tree | None:
        """The validated tree stored under ``tree_id``; None = absent or not a tree.

        Raises :class:`~meshforge.common.tree.TreeError` when the blob is present
        but is not an acceptable manifest (the caller reports it).
        """
        cached = self._trees.get(tree_id.hex)
        if cached is not None:
            self._trees.move_to_end(tree_id.hex)
            return cached
        size = self.size(tree_id)
        if size is None:
            return None
        if size > self.tree_limits.max_manifest_bytes:
            raise TreeError("the blob is too large to be a tree manifest")
        data = await asyncio.to_thread(self.path_for(tree_id).read_bytes)
        if data[:4] == _ZSTD_MAGIC:
            raise TreeError("the blob is a bundle, not a tree manifest")
        tree = await asyncio.to_thread(
            parse_tree, data, expected=tree_id, limits=self.tree_limits
        )
        self._remember(tree_id.hex, tree)
        return tree

    def _remember(self, key: str, tree: Tree | None) -> None:
        self._trees[key] = tree
        self._trees.move_to_end(key)
        while len(self._trees) > _TREE_CACHE:
            self._trees.popitem(last=False)

    async def tree_members(self, root: Digest) -> frozenset[str]:
        """Hex digests a token for ``root`` may read when ``root`` is a tree.

        Empty for anything that is not a stored, valid tree (a bundle, say).
        """
        if root.hex in self._trees and self._trees[root.hex] is None:
            return frozenset()
        try:
            tree = await self.tree(root)
        except (TreeError, OSError):
            tree = None
        if tree is None:
            if self.size(root) is not None:
                self._remember(root.hex, None)  # a stored non-tree: never parse again
            return frozenset()
        return frozenset(e.sha256 for e in tree.files())


# ================================================================ fetch tokens


@dataclass(frozen=True, slots=True)
class FetchGrant:
    attempt_id: str
    digests: frozenset[str]
    """Digest hex strings this token may read."""
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
            expires_at=now + ttl_s,
        )
        while len(self._grants) > self._capacity:
            self._grants.popitem(last=False)
            self.evicted += 1
            log.warning("controller.blobs.fetch_token_evicted", capacity=self._capacity)
        return SecretStr(token)

    def grant(self, token: str | None) -> FetchGrant | None:
        """The live grant of ``token`` (None = unknown or expired)."""
        if not token:
            return None
        grant = self._grants.get(self._key(token))
        if grant is None:
            return None
        if grant.expires_at <= self._clock():
            self._grants.pop(self._key(token), None)
            return None
        return grant

    def check(self, token: str | None, digest: Digest) -> bool:
        """True when ``token`` is live and may read ``digest`` (never upload)."""
        if not token:
            return False
        grant = self._grants.get(self._key(token))
        if grant is None:
            return False
        if grant.expires_at <= self._clock():
            self._grants.pop(self._key(token), None)
            return False
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
