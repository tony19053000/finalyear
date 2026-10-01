"""Content-addressed blob store shared by the controller and every agent (D79, D80).

**Layout.** A blob is one whole file named by the SHA-256 of its bytes::

    <root>/sha256/<aa>/<bb>/<64 hex>     the blob (read-only once stored)
    <root>/tmp/<random>.part             a write in progress (one-shot)
    <root>/tmp/resume-<hex>.part         a resumable transfer of <hex> ...
    <root>/tmp/resume-<hex>.part.chunks  ... and its chunk bitmap (D80)

Two hex levels keep any one folder small at millions of blobs. The V0.4 layout
``sha256/<aa>/<hex>`` is moved into this one by :meth:`CasStore.prepare` (a file
directly under ``<aa>/`` named by 64 hex characters is a V0.4 blob).

**Write, verify, then rename.** Every write goes to a part file in ``tmp/`` while
its bytes are hashed and counted; only when the stream ended, the size is within
the caps and the digest is the expected one is the part fsync'ed, renamed to its
real name, made read-only and its folder fsync'ed. A crash or any error before the
rename leaves at most a part file (swept at the next start), never a half-written
blob under a real name (checklist 14). A rename that finds the blob already there
discards the part: same name, same bytes.

**Admission (checklist 14).** Before a write the store checks ``max_blob_bytes``,
the optional ``quota_bytes`` (bytes of stored blobs + reserved writes) and the free
disk floor (``free_disk_floor_bytes`` must remain free after the write). The floor
is re-checked every 64 MiB while a stream of unknown size arrives.

**Chunks are transfer units, not stored objects (D80).** :func:`chunk_digests`
hashes a file once into its whole digest and per-chunk digests; a
:class:`ResumablePart` assembles a blob from verified chunks written at their
offsets, remembering done chunks in a bitmap sidecar written *after* the chunk's
bytes are fsync'ed - so after a crash a chunk is either marked and durable or
re-fetched, never trusted half-written. The finished part is hashed whole before
it becomes a blob (checklist 15).

**Reading.** :meth:`CasStore.verify` re-hashes a stored blob; a mismatch (disk
corruption, a hand edit) evicts it and answers ``False`` so the caller fetches it
again instead of using it. :meth:`CasStore.open_read` gives the raw file; callers
that hand bytes onward hash while they copy (agent materialisation, D84).

This module is synchronous and blocking (callers on an event loop use
``asyncio.to_thread``). Counters are thread-safe; two *resumable* writers for one
digest at once are the caller's to prevent (the controller and the agent each
serialise per digest).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final, Self

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest

__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "MAX_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "CasError",
    "CasFullError",
    "CasIntegrityError",
    "CasNotFoundError",
    "CasStore",
    "CasTooLargeError",
    "PrepareReport",
    "PutResult",
    "ResumablePart",
    "chunk_count",
    "chunk_digests",
    "chunk_span",
    "iter_file",
]

DEFAULT_CHUNK_SIZE: Final = 8 * 1024 * 1024
"""Transfer chunk (D80); S3's numbers may change it (V0.5 brief, transport note)."""
MIN_CHUNK_SIZE: Final = 64 * 1024
MAX_CHUNK_SIZE: Final = 64 * 1024 * 1024
MAX_CHUNKS: Final = 1 << 20
"""Bitmap bound: a blob may have at most this many chunks (1 Mi bits = 128 KiB)."""

_ALGO_DIR: Final = "sha256"
_TMP_DIR: Final = "tmp"
_RESUME_PREFIX: Final = "resume-"
_SIDECAR_SUFFIX: Final = ".chunks"
_DISK_CHECK_EVERY: Final = 64 * 1024 * 1024
_READ: Final = 1024 * 1024
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
_HEX2_RE: Final = re.compile(r"[0-9a-f]{2}")
_READ_ONLY: Final = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_RESUME_TTL_S: Final = 24 * 3600.0


class CasError(MeshForgeError):
    """Base of CAS failures (``transfer_error`` unless a subclass says otherwise)."""

    default_failure_type = FailureType.TRANSFER_ERROR


class CasIntegrityError(CasError):
    """Bytes do not hash to the name they were stored or sent under."""

    default_failure_type = FailureType.INTEGRITY_ERROR


class CasTooLargeError(CasError):
    default_failure_type = FailureType.INVALID_INPUT


class CasFullError(CasError):
    """Quota or free-disk floor: the write would not fit."""

    default_failure_type = FailureType.DISK_FULL


class CasNotFoundError(CasError):
    default_failure_type = FailureType.NOT_FOUND


@dataclass(frozen=True, slots=True)
class PutResult:
    digest: Digest
    size: int
    created: bool
    """False when the blob was already stored (nothing new was kept)."""


@dataclass(frozen=True, slots=True)
class PrepareReport:
    swept: int
    """One-shot part files a crash left behind (deleted)."""
    expired_resumes: int
    """Resumable transfers older than the TTL (deleted)."""
    migrated: int
    """V0.4-layout blobs moved into the two-level layout."""
    used_bytes: int


# ----------------------------------------------------------------- chunk maths


def _check_chunk_size(chunk_size: int) -> None:
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError(
            f"chunk_size must be in [{MIN_CHUNK_SIZE}, {MAX_CHUNK_SIZE}] bytes"
        )


def chunk_count(size: int, chunk_size: int) -> int:
    """Chunks of a ``size``-byte blob (an empty blob has one empty chunk)."""
    _check_chunk_size(chunk_size)
    if size < 0:
        raise ValueError("size must be >= 0")
    return max(1, -(-size // chunk_size))


def chunk_span(index: int, size: int, chunk_size: int) -> tuple[int, int]:
    """``(offset, length)`` of chunk ``index``; ``ValueError`` when out of range."""
    count = chunk_count(size, chunk_size)
    if not 0 <= index < count:
        raise ValueError(f"chunk index {index} out of range (0..{count - 1})")
    start = index * chunk_size
    return start, min(chunk_size, size - start)


def chunk_digests(path: Path, chunk_size: int) -> tuple[Digest, int, tuple[str, ...]]:
    """One read of ``path``: ``(whole digest, size, per-chunk hex digests)``."""
    _check_chunk_size(chunk_size)
    whole = hashlib.sha256()
    chunks: list[str] = []
    size = 0
    with path.open("rb") as fh:
        while True:
            piece = hashlib.sha256()
            got = 0
            while got < chunk_size:
                data = fh.read(min(_READ, chunk_size - got))
                if not data:
                    break
                piece.update(data)
                whole.update(data)
                got += len(data)
            if got == 0 and chunks:
                break
            chunks.append(piece.hexdigest())
            size += got
            if got < chunk_size:
                break
    return Digest(whole.hexdigest()), size, tuple(chunks)


# ----------------------------------------------------------------------- store


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # some file systems refuse directory fsync; the rename itself held
    finally:
        os.close(fd)


def _make_writable(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


class CasStore:
    """See module docstring."""

    def __init__(
        self,
        root: Path,
        *,
        max_blob_bytes: int,
        quota_bytes: int | None = None,
        free_disk_floor_bytes: int = 0,
        disk_free: Callable[[Path], int] | None = None,
        resume_ttl_s: float = _RESUME_TTL_S,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if max_blob_bytes < 0:
            raise ValueError("max_blob_bytes must be >= 0")
        if quota_bytes is not None and quota_bytes < 0:
            raise ValueError("quota_bytes must be >= 0")
        self.root = Path(root)
        self.max_blob_bytes = max_blob_bytes
        self.quota_bytes = quota_bytes
        self.free_disk_floor_bytes = free_disk_floor_bytes
        self._disk_free = disk_free or _disk_free
        self._resume_ttl_s = resume_ttl_s
        self._wall = wall_clock
        self.tmp = self.root / _TMP_DIR
        self._lock = threading.Lock()
        self._used = 0
        self._reserved = 0
        self.refused_quota = 0
        self.refused_disk = 0

    # ---------------------------------------------------------------- layout

    def prepare(self) -> PrepareReport:
        """Create the layout, sweep leftovers, migrate V0.4 blobs, count bytes."""
        algo = self.root / _ALGO_DIR
        algo.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        swept = expired = 0
        now = self._wall()
        for entry in list(self.tmp.iterdir()):
            name = entry.name
            if name.startswith(_RESUME_PREFIX):
                try:
                    age = now - entry.stat().st_mtime
                except OSError:
                    continue
                if age > self._resume_ttl_s:
                    with contextlib.suppress(OSError):
                        entry.unlink()
                        if not name.endswith(_SIDECAR_SUFFIX):
                            expired += 1
                continue
            with contextlib.suppress(OSError):
                entry.unlink()
                swept += 1
        migrated = self._migrate_legacy(algo)
        used = 0
        for path in algo.glob("*/*/*"):
            if _HEX_RE.fullmatch(path.name):
                with contextlib.suppress(OSError):
                    used += path.stat().st_size
        with self._lock:
            self._used = used
        return PrepareReport(
            swept=swept, expired_resumes=expired, migrated=migrated, used_bytes=used
        )

    def _migrate_legacy(self, algo: Path) -> int:
        moved = 0
        for top in algo.iterdir():
            if not (top.is_dir() and _HEX2_RE.fullmatch(top.name)):
                continue
            for entry in top.iterdir():
                if not (_HEX_RE.fullmatch(entry.name) and entry.is_file()):
                    continue
                final = self.path_for(Digest(entry.name))
                final.parent.mkdir(parents=True, exist_ok=True)
                if final.exists():
                    _make_writable(entry)
                    entry.unlink()
                else:
                    os.replace(entry, final)
                    with contextlib.suppress(OSError):
                        final.chmod(_READ_ONLY)
                moved += 1
        return moved

    def path_for(self, digest: Digest) -> Path:
        return self.root / _ALGO_DIR / digest.hex[:2] / digest.hex[2:4] / digest.hex

    def size(self, digest: Digest) -> int | None:
        try:
            return self.path_for(digest).stat().st_size
        except FileNotFoundError:
            return None

    def has(self, digest: Digest) -> bool:
        return self.path_for(digest).is_file()

    @property
    def used_bytes(self) -> int:
        with self._lock:
            return self._used

    # ------------------------------------------------------------- admission

    def _admit(
        self, digest: Digest | None, size: int, *, check_disk: bool = True
    ) -> None:
        """Reserve ``size`` bytes (quota + disk floor) or raise; see release."""
        shown = {"digest": str(digest)} if digest is not None else {}
        if size > self.max_blob_bytes:
            raise CasTooLargeError(
                f"blob larger than the {self.max_blob_bytes}-byte limit",
                details={**shown, "size": size},
            )
        with self._lock:
            if (
                self.quota_bytes is not None
                and self._used + self._reserved + size > self.quota_bytes
            ):
                self.refused_quota += 1
                raise CasFullError(
                    f"the store's {self.quota_bytes}-byte quota would be exceeded",
                    details={**shown, "reason": "quota"},
                )
            self._reserved += size
        try:
            if check_disk:
                self._check_disk(digest, size)
        except BaseException:
            self._release(size)
            raise

    def _release(self, size: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - size)

    def _check_disk(self, digest: Digest | None, needed: int) -> None:
        if self.free_disk_floor_bytes <= 0:
            return
        free = self._disk_free(self.root)
        if free - needed < self.free_disk_floor_bytes:
            with self._lock:
                self.refused_disk += 1
            raise CasFullError(
                "insufficient storage: the write would leave the disk below its "
                f"{self.free_disk_floor_bytes}-byte free floor",
                details={
                    **({"digest": str(digest)} if digest else {}),
                    "reason": "insufficient_storage",
                },
            )

    # ----------------------------------------------------------------- write

    def writer(
        self, digest: Digest | None, declared_size: int | None = None
    ) -> BlobWriter:
        """A one-shot streaming write (use as a context manager).

        ``digest`` None = name the blob by what was written (local puts).
        """
        return BlobWriter(self, digest, declared_size)

    def put_bytes(self, data: bytes, digest: Digest | None = None) -> PutResult:
        with self.writer(digest, len(data)) as w:
            w.write(data)
            return w.commit()

    def put_file(self, src: Path, digest: Digest | None = None) -> PutResult:
        """Copy ``src`` into the store, hashing while copying."""
        size = src.stat().st_size
        with self.writer(digest, size) as w, src.open("rb") as fh:
            while data := fh.read(_READ):
                w.write(data)
            return w.commit()

    def _install(self, part: Path, digest: Digest, size: int) -> bool:
        """Rename a verified, fsync'ed part into place; False = already there."""
        final = self.path_for(digest)
        if final.exists():
            part.unlink()
            return False
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(part, final)
        except PermissionError:
            # Windows: replacing a read-only blob someone just stored.
            if final.exists():
                part.unlink()
                return False
            raise
        with contextlib.suppress(OSError):
            final.chmod(_READ_ONLY)
        _fsync_dir(final.parent)
        with self._lock:
            self._used += size
        return True

    # ------------------------------------------------------------------ read

    def open_read(self, digest: Digest) -> BinaryIO:
        try:
            return self.path_for(digest).open("rb")
        except FileNotFoundError:
            raise CasNotFoundError(
                "no such blob", details={"digest": str(digest)}
            ) from None

    def verify(self, digest: Digest) -> bool:
        """Re-hash a stored blob; a mismatch evicts it. False = absent or bad."""
        hasher = hashlib.sha256()
        try:
            with self.open_read(digest) as fh:
                while data := fh.read(_READ):
                    hasher.update(data)
        except CasNotFoundError:
            return False
        if hmac.compare_digest(hasher.hexdigest(), digest.hex):
            return True
        self.evict(digest)
        return False

    def evict(self, digest: Digest) -> bool:
        """Delete a stored blob (a corrupt one, or GC later). True = removed."""
        path = self.path_for(digest)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        _make_writable(path)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        with self._lock:
            self._used = max(0, self._used - size)
        return True

    # ------------------------------------------------------------- resumable

    def resumable(self, digest: Digest, size: int, chunk_size: int) -> ResumablePart:
        """Open (or continue) the resumable transfer of ``digest``; see the class."""
        return ResumablePart.open(self, digest, size, chunk_size)

    def resume_paths(self, digest: Digest) -> tuple[Path, Path]:
        part = self.tmp / f"{_RESUME_PREFIX}{digest.hex}.part"
        return part, part.with_name(part.name + _SIDECAR_SUFFIX)


class BlobWriter:
    """One streaming write into a :class:`CasStore` (see module docstring)."""

    def __init__(
        self, store: CasStore, digest: Digest | None, declared_size: int | None
    ) -> None:
        self._store = store
        self._digest = digest
        self._declared = declared_size
        self._hasher = hashlib.sha256()
        self.count = 0
        self._reserved = 0
        self._part: Path | None = None
        self._fh: BinaryIO | None = None
        self._exists = digest is not None and store.has(digest)

    def __enter__(self) -> Self:
        if self._declared is not None and self._declared < 0:
            raise CasError("declared size must be >= 0")
        if not self._exists:
            reserve = self._declared or 0
            self._store._admit(self._digest, reserve)
            self._reserved = reserve
            self._part = self._store.tmp / f"{secrets.token_hex(12)}.part"
            self._fh = self._part.open("xb")
        elif self._declared is not None and self._declared > self._store.max_blob_bytes:
            raise CasTooLargeError(
                f"blob larger than the {self._store.max_blob_bytes}-byte limit"
            )
        return self

    def __exit__(self, *exc: object) -> None:
        self.abort()

    def write(self, data: bytes) -> None:
        if not data:
            return
        before = self.count
        self.count += len(data)
        if self.count > self._store.max_blob_bytes:
            raise CasTooLargeError(
                f"blob larger than the {self._store.max_blob_bytes}-byte limit",
                details={"digest": str(self._digest)} if self._digest else {},
            )
        if self._declared is not None and self.count > self._declared:
            raise CasError("more bytes than declared")
        self._hasher.update(data)
        if self._fh is None:
            return
        if self.count > self._reserved:
            # Unknown or exceeded size: reserve the growth (quota) as it comes.
            grow = self.count - self._reserved
            crossing = before // _DISK_CHECK_EVERY != self.count // _DISK_CHECK_EVERY
            self._store._admit(self._digest, grow, check_disk=crossing)
            self._reserved += grow
        elif before // _DISK_CHECK_EVERY != self.count // _DISK_CHECK_EVERY:
            self._store._check_disk(self._digest, 0)
        try:
            self._fh.write(data)
        except OSError as exc:
            raise _os_error(exc, self._digest) from None

    def commit(self) -> PutResult:
        """Verify and install; raises :class:`CasIntegrityError` on a mismatch."""
        if self._declared is not None and self.count != self._declared:
            raise CasError(
                "the stream ended early (fewer bytes than declared)",
                details={"digest": str(self._digest)} if self._digest else {},
            )
        actual = Digest(self._hasher.hexdigest())
        if self._digest is not None and not hmac.compare_digest(
            actual.hex, self._digest.hex
        ):
            raise CasIntegrityError(
                "bytes do not match the digest they were sent under",
                details={"expected": str(self._digest), "actual": str(actual)},
            )
        if self._fh is None or self._part is None:
            return PutResult(digest=actual, size=self.count, created=False)
        fh, part = self._fh, self._part
        try:
            fh.flush()
            os.fsync(fh.fileno())
        except OSError as exc:
            raise _os_error(exc, actual) from None
        finally:
            fh.close()
            self._fh = None
        try:
            created = self._store._install(part, actual, self.count)
        except OSError as exc:
            raise _os_error(exc, actual) from None
        self._part = None
        self._release()
        return PutResult(digest=actual, size=self.count, created=created)

    def _release(self) -> None:
        if self._reserved:
            self._store._release(self._reserved)
            self._reserved = 0

    def abort(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None
        if self._part is not None:
            with contextlib.suppress(OSError):
                self._part.unlink()
            self._part = None
        self._release()


def _os_error(exc: OSError, digest: Digest | None) -> CasError:
    shown = {"digest": str(digest)} if digest else {}
    if exc.errno == errno.ENOSPC:
        return CasFullError("the disk is full", details=shown)
    return CasError(
        f"cannot write the blob ({exc.strerror or type(exc).__name__})", details=shown
    )


# ------------------------------------------------------------------ resumable


class ResumablePart:
    """A blob assembled from verified chunks, resumable across crashes (D80).

    The part file has the blob's final size; the sidecar holds
    ``{"v", "size", "chunk_size", "done": <hex bitmap>}``. A sidecar that does not
    match the requested size/chunk size (or is unreadable) restarts the transfer.
    """

    def __init__(
        self,
        store: CasStore,
        digest: Digest,
        size: int,
        chunk_size: int,
        done: bytearray,
    ) -> None:
        self.store = store
        self.digest = digest
        self.size = size
        self.chunk_size = chunk_size
        self.count = chunk_count(size, chunk_size)
        self._done = done
        self.part, self.sidecar = store.resume_paths(digest)
        self.resumed_chunks = sum(1 for i in range(self.count) if self.is_done(i))

    @classmethod
    def open(
        cls, store: CasStore, digest: Digest, size: int, chunk_size: int
    ) -> ResumablePart:
        count = chunk_count(size, chunk_size)
        if count > MAX_CHUNKS:
            raise CasTooLargeError(f"more than {MAX_CHUNKS} chunks")
        part, sidecar = store.resume_paths(digest)
        done = cls._load(sidecar, size, chunk_size, count)
        if done is None or not part.is_file() or part.stat().st_size != size:
            store._admit(digest, size)
            try:
                store._release(size)
                with contextlib.suppress(FileNotFoundError):
                    sidecar.unlink()
                with part.open("wb") as fh:
                    fh.truncate(size)
                done = bytearray(-(-count // 8))
                cls._save(sidecar, size, chunk_size, done)
            except OSError as exc:
                raise _os_error(exc, digest) from None
        return cls(store, digest, size, chunk_size, done)

    @staticmethod
    def _load(
        sidecar: Path, size: int, chunk_size: int, count: int
    ) -> bytearray | None:
        try:
            data = json.loads(sidecar.read_bytes()[: 1 << 20])
            if (
                data.get("v") != 1
                or data.get("size") != size
                or data.get("chunk_size") != chunk_size
            ):
                return None
            bits = bytearray.fromhex(str(data["done"]))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None
        return bits if len(bits) == -(-count // 8) else None

    @staticmethod
    def _save(sidecar: Path, size: int, chunk_size: int, done: bytearray) -> None:
        body = json.dumps(
            {"v": 1, "size": size, "chunk_size": chunk_size, "done": done.hex()},
            separators=(",", ":"),
        ).encode()
        atomic_write_bytes(sidecar, body, mode=0o600)

    def is_done(self, index: int) -> bool:
        return bool(self._done[index // 8] & (1 << (index % 8)))

    def missing(self) -> list[int]:
        return [i for i in range(self.count) if not self.is_done(i)]

    def span(self, index: int) -> tuple[int, int]:
        return chunk_span(index, self.size, self.chunk_size)

    def write_chunk(self, index: int, data: bytes, expected_hex: str | None) -> bool:
        """Verify (when ``expected_hex``) and store chunk ``index``.

        Returns False when it was already done (nothing written). Raises
        :class:`CasIntegrityError` on a hash mismatch (nothing written) and
        :class:`CasError` on a wrong length.
        """
        offset, length = self.span(index)
        if len(data) != length:
            raise CasError(
                f"chunk {index} has {len(data)} bytes, expected {length}",
                details={"digest": str(self.digest)},
            )
        if expected_hex is not None and not hmac.compare_digest(
            hashlib.sha256(data).hexdigest(), expected_hex
        ):
            raise CasIntegrityError(
                f"chunk {index} does not match its digest; nothing was kept",
                details={"digest": str(self.digest), "chunk": index},
            )
        if self.is_done(index):
            return False
        try:
            fd = os.open(self.part, os.O_WRONLY | getattr(os, "O_BINARY", 0))
            try:
                written = 0
                while written < length:
                    if hasattr(os, "pwrite"):
                        written += os.pwrite(fd, data[written:], offset + written)
                    else:  # pragma: no cover - Windows has no pwrite
                        os.lseek(fd, offset + written, os.SEEK_SET)
                        written += os.write(fd, data[written:])
                os.fsync(fd)  # bytes durable before the bitmap says so
            finally:
                os.close(fd)
            self._done[index // 8] |= 1 << (index % 8)
            self._save(self.sidecar, self.size, self.chunk_size, self._done)
        except OSError as exc:
            raise _os_error(exc, self.digest) from None
        return True

    def complete(self) -> PutResult:
        """Hash the whole part; install it as the blob (checklist 15).

        A mismatch deletes the part and the sidecar (the next try starts over) and
        raises :class:`CasIntegrityError`.
        """
        if self.missing():
            raise CasError(
                f"{len(self.missing())} chunk(s) still missing",
                details={"digest": str(self.digest)},
            )
        hasher = hashlib.sha256()
        with self.part.open("rb") as fh:
            while data := fh.read(_READ):
                hasher.update(data)
        if not hmac.compare_digest(hasher.hexdigest(), self.digest.hex):
            self.discard()
            raise CasIntegrityError(
                "the assembled blob does not match its digest; the transfer "
                "starts over",
                details={"digest": str(self.digest)},
            )
        try:
            self.store._check_disk(self.digest, 0)
            with self.part.open("rb+") as fh:
                os.fsync(fh.fileno())
            created = self.store._install(self.part, self.digest, self.size)
        except OSError as exc:
            raise _os_error(exc, self.digest) from None
        with contextlib.suppress(OSError):
            self.sidecar.unlink()
        return PutResult(digest=self.digest, size=self.size, created=created)

    def discard(self) -> None:
        for path in (self.sidecar, self.part):
            with contextlib.suppress(OSError):
                path.unlink()


def iter_file(fh: BinaryIO, size: int = _READ) -> Iterator[bytes]:
    """Read ``fh`` in pieces (helper for streaming a stored blob)."""
    while data := fh.read(size):
        yield data
