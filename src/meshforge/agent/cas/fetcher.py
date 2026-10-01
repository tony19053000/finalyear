"""Fetch blobs into the agent's CAS (V0.5 G1, D80/D84).

For each blob a tree names that the cache does not already hold:

* a **one-chunk** file (``size <= chunk_size``) is one ``GET``, streamed into a
  :class:`~meshforge.common.cas.BlobWriter` that hashes as it writes and installs
  the blob only if the digest matches;
* a **chunked** file is fetched chunk by chunk with ``Range: bytes=a-b`` into a
  :class:`~meshforge.common.cas.ResumablePart`: every chunk is verified against
  the tree manifest's chunk digest before it is written, the bitmap sidecar
  records it, and the assembled file is hashed whole before it becomes a blob
  (checklist 15). A crash or cancellation keeps the verified chunks: the next
  fetch asks only for the missing ones (criterion 3).

A chunk whose bytes do not match is re-requested up to ``chunk_retries`` times
(a flaky link); after that the fetch fails ``integrity_error`` and nothing of
that blob is ever materialised. Statuses map to typed failures like the V0.4
bundle fetch (``unauthorized`` 401/403, ``not_found`` 404, ``timeout``,
``transfer_error`` otherwise). Tokens travel only in the ``Authorization``
header and are never logged.

Wave 1 fetches from the first listed source; several sources in parallel with
work stealing is V0.5 wave 2 (D89). Blobs are fetched ``parallel`` at a time;
the chunks of one blob in order.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

import httpx

from meshforge.agent.cas.store import AgentCas
from meshforge.common.cas import CasError, CasIntegrityError, chunk_span
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.tree import Tree, TreeEntry, TreeError, TreeLimits, parse_tree

__all__ = ["BlobFetcher", "BlobSource", "FetchError", "FetchStats"]

log = get_logger(__name__)

_STREAM: Final = 1024 * 1024
DEFAULT_PARALLEL: Final = 4
DEFAULT_CHUNK_RETRIES: Final = 2


class FetchError(MeshForgeError):
    default_failure_type = FailureType.TRANSFER_ERROR


@dataclass(frozen=True, slots=True)
class BlobSource:
    base_url: str
    """``http(s)://host:port`` (no trailing slash)."""
    token: str
    node_id: str = ""
    """Empty = the controller."""

    def __repr__(self) -> str:  # never show the token
        return f"BlobSource(base_url={self.base_url!r}, node_id={self.node_id!r})"


@dataclass(slots=True)
class FetchStats:
    blobs_hit: int = 0
    blobs_fetched: int = 0
    bytes_fetched: int = 0
    """Payload bytes received (chunks re-requested after corruption included)."""
    chunks_fetched: int = 0
    chunks_resumed: int = 0
    """Chunks a previous, interrupted fetch had already verified and kept."""
    corrupt_chunks: int = 0
    manifest_hit: bool = False
    sources: dict[str, int] = field(default_factory=dict)
    """Bytes per source node (``controller`` for the controller)."""


def _status_error(status: int, what: str) -> FetchError:
    if status in (401, 403):
        return FetchError(
            f"the source refused this attempt's fetch token for {what}",
            failure_type=FailureType.UNAUTHORIZED,
        )
    if status == 404:
        return FetchError(
            f"the source does not have {what}", failure_type=FailureType.NOT_FOUND
        )
    return FetchError(f"fetching {what} answered HTTP {status}")


class BlobFetcher:
    """See module docstring. One per staging; ``client`` is the caller's."""

    def __init__(
        self,
        cas: AgentCas,
        sources: Iterable[BlobSource],
        client: httpx.AsyncClient,
        *,
        parallel: int = DEFAULT_PARALLEL,
        chunk_retries: int = DEFAULT_CHUNK_RETRIES,
        tree_limits: TreeLimits | None = None,
    ) -> None:
        listed = list(sources)
        if not listed:
            raise ValueError("at least one blob source is needed")
        if parallel < 1 or chunk_retries < 0:
            raise ValueError("parallel >= 1 and chunk_retries >= 0")
        self._cas = cas
        self._source = listed[0]
        self._client = client
        self._parallel = parallel
        self._retries = chunk_retries
        self._limits = tree_limits or TreeLimits()
        self.stats = FetchStats()

    @property
    def _who(self) -> str:
        return self._source.node_id or "controller"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"authorization": f"Bearer {self._source.token}", **(extra or {})}

    def _url(self, digest: Digest) -> str:
        return f"{self._source.base_url}/v1/blobs/{digest.hex}"

    def _count(self, n: int) -> None:
        self.stats.bytes_fetched += n
        self.stats.sources[self._who] = self.stats.sources.get(self._who, 0) + n

    # ------------------------------------------------------------- manifest

    async def tree(self, tree_id: Digest) -> Tree:
        """The verified tree ``tree_id`` (from the cache, else fetched + cached)."""
        store = self._cas.store
        async with self._cas.holding(tree_id):
            if store.has(tree_id):
                data = await asyncio.to_thread(store.path_for(tree_id).read_bytes)
                try:
                    tree = parse_tree(data, expected=tree_id, limits=self._limits)
                except TreeError:
                    # A cached manifest that no longer verifies: evict, refetch.
                    await asyncio.to_thread(store.evict, tree_id)
                else:
                    self.stats.manifest_hit = True
                    return tree
            data = await self._get_whole(tree_id, self._limits.max_manifest_bytes)
            if hashlib.sha256(data).hexdigest() != tree_id.hex:
                raise FetchError(
                    "the tree manifest does not match its id; nothing was kept",
                    failure_type=FailureType.INTEGRITY_ERROR,
                )
            tree = parse_tree(data, expected=tree_id, limits=self._limits)
            try:
                await asyncio.to_thread(store.put_bytes, data, tree_id)
            except CasError as exc:
                raise FetchError(exc.message, failure_type=exc.failure_type) from None
            return tree

    async def _get_whole(self, digest: Digest, max_bytes: int) -> bytes:
        try:
            async with self._client.stream(
                "GET", self._url(digest), headers=self._headers()
            ) as response:
                if response.status_code != 200:
                    raise _status_error(response.status_code, "the tree manifest")
                data = bytearray()
                async for piece in response.aiter_raw(_STREAM):
                    data += piece
                    if len(data) > max_bytes:
                        raise FetchError(
                            f"tree manifest larger than {max_bytes} bytes",
                            failure_type=FailureType.INVALID_INPUT,
                        )
        except httpx.TimeoutException:
            raise FetchError(
                "timed out fetching the tree manifest", failure_type=FailureType.TIMEOUT
            ) from None
        except httpx.HTTPError as exc:
            raise FetchError(f"manifest fetch failed: {type(exc).__name__}") from None
        self._count(len(data))
        return bytes(data)

    # ---------------------------------------------------------------- blobs

    async def ensure_all(self, tree: Tree) -> FetchStats:
        """Every file blob of ``tree`` is in the cache (fetched if missing)."""
        unique: dict[str, TreeEntry] = {}
        for entry in tree.files():
            unique.setdefault(entry.sha256, entry)
        gate = asyncio.Semaphore(self._parallel)

        async def one(entry: TreeEntry) -> None:
            async with gate:
                await self.ensure(entry, tree.chunk_size)

        try:
            async with asyncio.TaskGroup() as group:
                for entry in unique.values():
                    group.create_task(one(entry))
        except BaseExceptionGroup as group_error:
            # One typed error for the attempt: the first fetch failure (the
            # TaskGroup already cancelled the other fetches).
            first = _first_leaf(group_error)
            raise first from None
        return self.stats

    async def ensure(self, entry: TreeEntry, chunk_size: int) -> bool:
        """Fetch ``entry``'s blob unless cached; True = it was fetched now."""
        digest = entry.digest
        store = self._cas.store
        async with self._cas.holding(digest):
            if store.has(digest):
                self.stats.blobs_hit += 1
                return False
            if entry.chunks:
                await self._fetch_chunked(entry, chunk_size)
            else:
                await self._fetch_whole(entry)
            self.stats.blobs_fetched += 1
            return True

    async def _fetch_whole(self, entry: TreeEntry) -> None:
        digest = entry.digest
        store = self._cas.store
        try:
            with store.writer(digest, entry.size) as writer:
                async with self._client.stream(
                    "GET", self._url(digest), headers=self._headers()
                ) as response:
                    if response.status_code != 200:
                        raise _status_error(response.status_code, f"blob {digest}")
                    async for piece in response.aiter_raw(_STREAM):
                        self._count(len(piece))
                        writer.write(piece)
                await asyncio.to_thread(writer.commit)
        except CasIntegrityError:
            raise FetchError(
                f"blob {digest} does not match its digest; nothing was kept",
                failure_type=FailureType.INTEGRITY_ERROR,
            ) from None
        except CasError as exc:
            raise FetchError(exc.message, failure_type=exc.failure_type) from None
        except httpx.TimeoutException:
            raise FetchError(
                f"timed out fetching blob {digest}", failure_type=FailureType.TIMEOUT
            ) from None
        except httpx.HTTPError as exc:
            raise FetchError(f"blob fetch failed: {type(exc).__name__}") from None

    async def _fetch_chunked(self, entry: TreeEntry, chunk_size: int) -> None:
        digest = entry.digest
        store = self._cas.store
        try:
            part = await asyncio.to_thread(
                store.resumable, digest, entry.size, chunk_size
            )
            self.stats.chunks_resumed += part.resumed_chunks
            for index in part.missing():
                data = await self._chunk(entry, index, chunk_size)
                await asyncio.to_thread(
                    part.write_chunk, index, data, entry.chunks[index]
                )
                self.stats.chunks_fetched += 1
            await asyncio.to_thread(part.complete)
        except CasIntegrityError:
            raise FetchError(
                f"blob {digest} does not match its digest after assembly; the "
                "partial download was discarded",
                failure_type=FailureType.INTEGRITY_ERROR,
            ) from None
        except CasError as exc:
            raise FetchError(exc.message, failure_type=exc.failure_type) from None

    async def _chunk(self, entry: TreeEntry, index: int, chunk_size: int) -> bytes:
        start, length = chunk_span(index, entry.size, chunk_size)
        expected = entry.chunks[index]
        for attempt in range(self._retries + 1):
            data = await self._range(entry.digest, start, length)
            if hmac.compare_digest(hashlib.sha256(data).hexdigest(), expected):
                return data
            self.stats.corrupt_chunks += 1
            log.warning(
                "agent.cas.chunk_corrupt",
                digest=str(entry.digest),
                chunk=index,
                attempt=attempt + 1,
                source=self._who,
            )
        raise FetchError(
            f"chunk {index} of blob {entry.digest} arrived corrupt "
            f"{self._retries + 1} times; nothing of it was kept",
            failure_type=FailureType.INTEGRITY_ERROR,
        )

    async def _range(self, digest: Digest, start: int, length: int) -> bytes:
        headers = self._headers({"range": f"bytes={start}-{start + length - 1}"})
        try:
            async with self._client.stream(
                "GET", self._url(digest), headers=headers
            ) as response:
                if response.status_code != 206:
                    raise _status_error(response.status_code, f"blob {digest}")
                data = bytearray()
                async for piece in response.aiter_raw(_STREAM):
                    data += piece
                    self._count(len(piece))
                    if len(data) > length:
                        raise FetchError("a range answer is longer than asked for")
        except httpx.TimeoutException:
            raise FetchError(
                f"timed out fetching blob {digest}", failure_type=FailureType.TIMEOUT
            ) from None
        except httpx.HTTPError as exc:
            raise FetchError(f"chunk fetch failed: {type(exc).__name__}") from None
        if len(data) != length:
            raise FetchError("a range answer ended early")
        return bytes(data)


def _first_leaf(group: BaseExceptionGroup[BaseException]) -> BaseException:
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            return _first_leaf(exc)
        if isinstance(exc, FetchError):
            return exc
    leaf = group.exceptions[0]
    return _first_leaf(leaf) if isinstance(leaf, BaseExceptionGroup) else leaf
