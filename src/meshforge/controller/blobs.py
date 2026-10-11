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
at start. Every uploader (``owner``: a user or a node) has its **own staging**
of a digest (V0.7 G4 fix round 1, D157): a part another user sent is never in
your upload, and another user's ``complete`` or part status never sees yours.

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
import contextlib
import functools
import hashlib
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

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
    "GcGuard",
    "PartStatus",
    "PutResult",
]

log = get_logger(__name__)

MAX_FETCH_TOKEN_TTL_S: Final = 7 * 24 * 3600 + 3600
"""The longest job limit (7 days) plus an hour of grace."""
MAX_FETCH_TOKEN_DIGESTS: Final = 1024
MAX_HAVE_DIGESTS: Final = 10_000
"""Digests one have/need call may ask about (the client batches above this)."""
DEFAULT_TREE_CACHE_ENTRIES: Final = 500_000
"""Cached trees are bounded by their total entries (about 100 B each), not count."""
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


class GcGuard:
    """Shared/exclusive lock on one event loop (V0.5 G5): many holders of
    :meth:`shared` at once, or one of :meth:`exclusive`. A waiting exclusive
    holder blocks new shared ones, so a GC pass is never starved by a stream of
    submissions."""

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._shared = 0
        self._exclusive = False
        self._waiting = 0

    @contextlib.asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        async with self._cond:
            await self._cond.wait_for(lambda: not self._exclusive and not self._waiting)
            self._shared += 1
        try:
            yield
        finally:
            async with self._cond:
                self._shared -= 1
                self._cond.notify_all()

    @contextlib.asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        async with self._cond:
            self._waiting += 1
            try:
                await self._cond.wait_for(
                    lambda: not self._exclusive and not self._shared
                )
            except BaseException:
                self._waiting -= 1
                self._cond.notify_all()  # readers held back by this waiter
                raise
            self._waiting -= 1
            self._exclusive = True
        try:
            yield
        finally:
            async with self._cond:
                self._exclusive = False
                self._cond.notify_all()


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
        tree_cache_entries: int = DEFAULT_TREE_CACHE_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
        resume_sweep_s: float = 3600.0,
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
        self._part_locks: dict[str, list[Any]] = {}
        self._trees: OrderedDict[str, _CachedTree] = OrderedDict()
        self._tree_weight = 0
        self._tree_cache_entries = tree_cache_entries
        self.trees_parsed = 0
        self._clock = clock
        self._resume_sweep_s = resume_sweep_s
        self._last_sweep = clock()
        self.resumes_swept = 0
        """Manifests parsed (each tree once while cached; tests read it)."""
        self.uploads_refused_busy = 0
        self.parts_received = 0
        self.part_bytes_received = 0
        self.gc_guard = GcGuard()
        """V0.5 G5: held exclusively by a GC pass and shared by whatever makes a
        blob referenced (a job submission from its tree check to its row, a
        named-input push), so a pass never removes a blob a new job is being
        created on - and submissions never wait for each other."""
        self.corrupt_evicted = 0
        self.used: Callable[[list[Digest]], Awaitable[None]] | None = None
        """V0.5 G5: told which stored blobs a client was just answered 'have'
        for (GC last use: they are about to be referenced by a job)."""

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

    @contextlib.asynccontextmanager
    async def _holding(self, digest: Digest) -> AsyncIterator[None]:
        """Exclusive per digest; the entry is dropped only when nobody holds or
        waits for it (reference counted: a waiter never ends up on a lock a
        newcomer no longer sees)."""
        entry = self._part_locks.get(digest.hex)
        if entry is None:
            entry = self._part_locks[digest.hex] = [asyncio.Lock(), 0]
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._part_locks.pop(digest.hex, None)

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

    async def part_status(
        self, digest: Digest, *, owner: str | None = None
    ) -> PartStatus | None:
        """What ``owner``'s resumable upload of ``digest`` already holds (None =
        nothing; a stored blob is ``complete`` for everyone)."""
        size = self.size(digest)
        if size is not None:
            return PartStatus(size=size, chunk_size=0, received=(), complete=True)
        _part, sidecar = self.cas.resume_paths(digest, owner)
        if not sidecar.is_file():
            return None
        async with self._holding(digest):
            if (size := self.size(digest)) is not None:
                return PartStatus(size=size, chunk_size=0, received=(), complete=True)
            return await asyncio.to_thread(self._read_status, digest, owner)

    def _read_status(self, digest: Digest, owner: str | None) -> PartStatus | None:
        _part, sidecar = self.cas.resume_paths(digest, owner)
        try:
            data = json.loads(sidecar.read_bytes()[: 1 << 20])
            size, chunk_size = int(data["size"]), int(data["chunk_size"])
            part = self.cas.resumable(digest, size, chunk_size, owner=owner)
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
        owner: str | None = None,
    ) -> bool:
        """Store part ``index`` of ``owner``'s upload of ``digest`` (verified
        against ``chunk_hex``; each uploader has its own staging, D157).

        Returns False when the part (or the whole blob) was already here. Raises
        :class:`BlobError` subclasses like :meth:`put`.
        """
        self._check_shape(digest, size, chunk_size)
        await self._maybe_sweep()
        await self._slot(digest)
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
            async with self._holding(digest):
                if self.size(digest) is not None:
                    return False
                part = await asyncio.to_thread(
                    functools.partial(
                        self.cas.resumable, digest, size, chunk_size, owner=owner
                    )
                )
                # One request = one part, so the floor is checked on every part
                # (a statvfs) against the bytes still to come (review E1).
                stored = await asyncio.to_thread(
                    functools.partial(
                        part.write_chunk, index, bytes(data), chunk_hex, check_disk=True
                    )
                )
        except CasError as exc:
            raise _translate(exc, digest) from None
        except ValueError as exc:  # index out of range
            raise BlobError(str(exc), failure_type=FailureType.INVALID_INPUT) from None
        finally:
            self._uploads.release()
        if stored:
            self.parts_received += 1
            self.part_bytes_received += len(data)
        return stored

    async def _maybe_sweep(self) -> None:
        """Abandoned resumable uploads are swept at start and then at most once
        per ``resume_sweep_s`` from the part-upload path (no extra task)."""
        now = self._clock()
        if now - self._last_sweep < self._resume_sweep_s:
            return
        self._last_sweep = now
        swept = await asyncio.to_thread(self.cas.sweep_resumes)
        if swept:
            self.resumes_swept += swept
            log.info("controller.blobs.resumes_swept", count=swept)

    def _drop_staging(self, digest: Digest, owner: str | None) -> None:
        """Discard ``owner``'s staging of a stored ``digest`` (if any)."""
        status = self._read_status(digest, owner)
        if status is None:
            return
        with contextlib.suppress(CasError, ValueError):
            self.cas.resumable(
                digest, status.size, status.chunk_size, owner=owner
            ).discard()

    async def complete_parts(
        self, digest: Digest, *, owner: str | None = None
    ) -> PutResult:
        """Hash ``owner``'s assembled parts and install the blob (see module
        docstring); another uploader's staging is never touched (D157). When
        the blob is already stored (another uploader finished first), the
        caller's own staging of it is dropped - it can never be needed again."""
        try:
            async with self._holding(digest):
                # Under the lock: a concurrent complete may have stored it
                # meanwhile - then this one succeeds too (idempotent).
                if (existing := self.size(digest)) is not None:
                    await asyncio.to_thread(self._drop_staging, digest, owner)
                    return PutResult(digest=digest, size=existing, created=False)
                status = await asyncio.to_thread(self._read_status, digest, owner)
                if status is None:
                    raise BlobNotFoundError(
                        "no part upload of this blob is in progress",
                        details={"digest": str(digest)},
                    )
                part = await asyncio.to_thread(
                    functools.partial(
                        self.cas.resumable,
                        digest,
                        status.size,
                        status.chunk_size,
                        owner=owner,
                    )
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
        log.info(
            "controller.blobs.stored", digest=str(digest), size=result.size, parts=True
        )
        return PutResult(digest=digest, size=result.size, created=result.created)

    # -------------------------------------------------------------- trees

    async def tree(self, tree_id: Digest) -> Tree | None:
        """The validated tree stored under ``tree_id``; None = absent or not a tree.

        Raises :class:`~meshforge.common.tree.TreeError` when the blob is present
        but is not an acceptable manifest (the caller reports it). Parsed once per
        tree while it stays in the cache (see :meth:`tree_members`).
        """
        cached = await self._cached_tree(tree_id)
        if cached is None:
            return None
        if cached.tree is None:  # remembered: stored, but not a valid tree
            raise TreeError("the blob is not a valid tree manifest")
        return cached.tree

    async def _cached_tree(self, tree_id: Digest) -> _CachedTree | None:
        hit = self._trees.get(tree_id.hex)
        if hit is not None:
            self._trees.move_to_end(tree_id.hex)
            return hit
        async with self._holding(tree_id):  # one parse per tree, even when racing
            hit = self._trees.get(tree_id.hex)
            if hit is not None:
                return hit
            size = self.size(tree_id)
            if size is None:
                return None
            if size > self.tree_limits.max_manifest_bytes:
                self._remember(tree_id.hex, _NOT_A_TREE)
                raise TreeError("the blob is too large to be a tree manifest")
            data = await asyncio.to_thread(self.path_for(tree_id).read_bytes)
            if data[:4] == _ZSTD_MAGIC:
                self._remember(tree_id.hex, _NOT_A_TREE)
                raise TreeError("the blob is a bundle, not a tree manifest")
            try:
                # Parsed AND its member set built in the worker thread: a tree
                # of 100 000 entries never builds a big set on the event loop.
                cached = await asyncio.to_thread(
                    _parse_cached, data, tree_id, self.tree_limits
                )
            except TreeError:
                if hashlib.sha256(data).hexdigest() != tree_id.hex:
                    # V0.5 G5 scrub on a read mismatch: the stored bytes are not
                    # the blob they are named after - removed, never served.
                    await asyncio.to_thread(self.cas.evict, tree_id)
                    self.corrupt_evicted += 1
                    log.error(  # noqa: TRY400 - typed event, no traceback wanted
                        "controller.blobs.scrub_corrupt",
                        digest=str(tree_id),
                        detail="a stored manifest does not match its name; removed",
                    )
                    return None
                self._remember(tree_id.hex, _NOT_A_TREE)
                raise
            self.trees_parsed += 1
            # Built once, next to the parsed tree: an authorised GET is then
            # O(roots) set lookups, never a rebuild.
            self._remember(tree_id.hex, cached)
            return cached

    def _remember(self, key: str, cached: _CachedTree) -> None:
        old = self._trees.pop(key, None)
        if old is not None:
            self._tree_weight -= old.weight
        self._trees[key] = cached
        self._tree_weight += cached.weight
        while self._tree_weight > self._tree_cache_entries and len(self._trees) > 1:
            _key, evicted = self._trees.popitem(last=False)
            self._tree_weight -= evicted.weight

    def forget_tree(self, digest: Digest) -> None:
        """Drop a cached tree (its manifest blob was evicted or found corrupt)."""
        old = self._trees.pop(digest.hex, None)
        if old is not None:
            self._tree_weight -= old.weight

    async def tree_members(self, root: Digest) -> frozenset[str]:
        """Hex digests a token for ``root`` may read when ``root`` is a tree.

        Empty for anything that is not a stored, valid tree (a bundle, say).
        Cached with the tree (bounded by total entries), so repeated calls cost a
        dictionary lookup.
        """
        try:
            cached = await self._cached_tree(root)
        except (TreeError, OSError):
            return frozenset()
        return frozenset() if cached is None else cached.members


@dataclass(frozen=True, slots=True)
class _CachedTree:
    tree: Tree | None
    members: frozenset[str]

    @property
    def weight(self) -> int:
        """Cache cost: the tree's entries (a negative entry costs 1)."""
        return 1 + (0 if self.tree is None else len(self.tree.entries))


_NOT_A_TREE: Final = _CachedTree(None, frozenset())


def _parse_cached(data: bytes, tree_id: Digest, limits: TreeLimits) -> _CachedTree:
    """Parse a manifest and build its member set (run in a worker thread)."""
    tree = parse_tree(data, expected=tree_id, limits=limits)
    return _CachedTree(tree, frozenset(e.sha256 for e in tree.files()))


# ================================================================ fetch tokens


@dataclass(frozen=True, slots=True)
class FetchGrant:
    attempt_id: str
    digests: frozenset[str]
    """Digest hex strings this token may read."""
    expires_at: float
    """Controller monotonic time."""
    node_id: str | None = None
    """V0.7 G3 (D139): the node the token was sent to; in mtls mode only that
    node's TLS client certificate may use it."""


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
        node_id: str | None = None,
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
            node_id=node_id,
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

    def has(self, attempt_id: str) -> bool:
        """True when ``attempt_id`` holds at least one live (unexpired) token."""
        now = self._clock()
        return any(
            g.attempt_id == attempt_id and g.expires_at > now
            for g in self._grants.values()
        )

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
                    node_id=grant.node_id,
                )
                changed += 1
        return changed

    def revoke(self, attempt_id: str) -> int:
        """Invalidate every token of ``attempt_id`` now; returns count."""
        keys = [k for k, g in self._grants.items() if g.attempt_id == attempt_id]
        for key in keys:
            del self._grants[key]
        return len(keys)
