"""Fetch an attempt's bundle from the controller's blob store (V0.4 G4, D48/D67).

``GET <blob base>/v1/blobs/<hex>`` with the attempt's **fetch token** (never the
fabric token: an agent does not hold it, ADR-0014), streamed to a part file while
it is hashed and counted. Only a body whose SHA-256 is the requested digest is
renamed into place; anything else deletes the part file and raises
:class:`FetchError` with a typed failure:

* ``unauthorized`` - the token was refused (401/403: expired, revoked, wrong);
* ``not_found`` - the controller does not have the blob (404);
* ``integrity_error`` - the bytes do not hash to the digest (a corrupt or hostile
  body): nothing is kept;
* ``invalid_input`` - the body is larger than ``max_bytes``;
* ``transfer_error`` - connection refused/reset, other HTTP statuses, a body that
  ended early;
* ``timeout`` - connect/read timed out.

The token travels only in the ``Authorization`` header and is never logged.

**Trees (V0.5 G1, D83/D84).** :func:`stage_tree` replaces the bundle fetch for a
job that names a ``tree_id``: the manifest and every missing blob come into the
agent's CAS (:mod:`meshforge.agent.cas`: chunked, verified, resumable), then the
tree is copied into the workdir hashing while copying. A cached blob found
corrupt during the copy is evicted and fetched once more; corrupt twice is
``integrity_error``. :func:`tree_sources` validates ``ExecutionSpec.sources``
(network input: bounded count, URL shape, token shape); an empty list means the
controller with the attempt's fetch token.

**Uploads (V0.4 G5, D73/D74).** :func:`upload_blob` ``PUT``s one file with the
attempt's **upload grant** (never the fetch token, which is download-only),
always with ``Content-Length`` (the grant reserves quota from it). A connection
failure, a timeout or ``503`` (the controller's upload slots are busy) is
retried with backoff a few times; the final failure is an :class:`UploadError`
whose ``retryable`` says whether a later attempt may succeed: ``403`` (the
attempt ended) and ``411/413`` (quota) are not; ``507`` (the controller's disk)
and transport errors are. ``401`` (the controller does not know the grant - after
a restart it sends a new one, V0.6 D114) is retryable with ``details["refused"]``:
the caller waits for ``RefreshUploadGrant``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx

from meshforge.agent.cas import (
    AgentCas,
    BlobFetcher,
    BlobSource,
    CorruptBlobError,
    MaterialiseError,
    materialise,
)
from meshforge.agent.cas.fetcher import FetchError, FetchTuning, SourceStats
from meshforge.agent.cas.store import DEFAULT_FREE_DISK_FLOOR_BYTES
from meshforge.agent.execution.failure_evidence import DISK_FULL_ERRNOS
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.tree import Tree

__all__ = [
    "DEFAULT_MAX_BUNDLE_BYTES",
    "MAX_SOURCES",
    "FetchError",
    "StageReport",
    "UploadError",
    "blob_base_url",
    "blob_url_ok",
    "fetch_bundle",
    "stage_tree",
    "token_ok",
    "tree_sources",
    "upload_blob",
]

log = get_logger(__name__)

DEFAULT_MAX_BUNDLE_BYTES: Final = 2 * 1024**3
"""The controller's default ``max_blob_bytes``: a bigger body is not a bundle."""
_CHUNK: Final = 1024 * 1024


class UploadError(MeshForgeError):
    """An output/log upload failed; ``failure_type`` is always ``output_error``."""

    default_failure_type = FailureType.OUTPUT_ERROR


MAX_SOURCES: Final = 16
_MAX_URL_CHARS: Final = 300
_MAX_TOKEN_CHARS: Final = 512
_MAX_NODE_ID_CHARS: Final = 40
_SOURCE_URL: Final = re.compile(
    r"https?://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]{1,253}):\d{1,5}"
)
_UPLOAD_TRIES: Final = 4
_UPLOAD_BACKOFF_S: Final = 0.5


def blob_base_url(
    blob_url: str, controller_host: str | None, blob_port: int, *, scheme: str = "http"
) -> str:
    """``ExecutionSpec.blob_url`` if set, else ``<scheme>://<dialled host>:<blob_port>``.

    ``scheme`` ``https`` (mtls mode, V0.7 G3): a plaintext ``blob_url`` is
    refused (``config_error``) - there is no fallback to HTTP."""
    if blob_url:
        _require_scheme(blob_url, scheme)
        return blob_url.rstrip("/")
    if not controller_host or not 0 < blob_port < 65536:
        raise FetchError(
            "no blob URL: the controller sent none and this agent has no dialled host",
            failure_type=FailureType.CONFIG_ERROR,
        )
    host = f"[{controller_host}]" if ":" in controller_host else controller_host
    return f"{scheme}://{host}:{blob_port}"


def _require_scheme(url: str, scheme: str) -> None:
    if scheme == "https" and not url.lower().startswith("https://"):
        raise FetchError(
            "the controller named a plaintext blob URL, and this agent only "
            "speaks HTTPS ([security] mode = 'mtls'); fix [controller.api] "
            "advertise_url",
            failure_type=FailureType.CONFIG_ERROR,
        )


async def fetch_bundle(
    base_url: str,
    digest: Digest,
    token: str,
    dest: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BUNDLE_BYTES,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Download ``digest`` to ``dest`` (verified); returns its size. See module doc."""
    part = dest.with_name(f"{dest.name}.{secrets.token_hex(6)}.part")
    hasher = hashlib.sha256()
    size = 0
    url = f"{base_url}/v1/blobs/{digest.hex}"
    try:
        async with (
            httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_s), transport=transport, trust_env=False
            ) as client,
            client.stream(
                "GET", url, headers={"authorization": f"Bearer {token}"}
            ) as response,
        ):
            _check_status(response.status_code)
            declared = response.headers.get("content-length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > max_bytes
            ):
                raise FetchError(
                    f"bundle larger than {max_bytes} bytes",
                    failure_type=FailureType.INVALID_INPUT,
                )
            with part.open("xb") as fh:
                async for chunk in response.aiter_raw(_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise FetchError(
                            f"bundle larger than {max_bytes} bytes",
                            failure_type=FailureType.INVALID_INPUT,
                        )
                    hasher.update(chunk)
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
        if not hmac.compare_digest(hasher.hexdigest(), digest.hex):
            raise FetchError(
                "the fetched bundle does not match its digest; nothing was kept",
                failure_type=FailureType.INTEGRITY_ERROR,
            )
        os.replace(part, dest)
    except httpx.TimeoutException:
        raise FetchError(
            "timed out fetching the bundle", failure_type=FailureType.TIMEOUT
        ) from None
    except httpx.HTTPError as exc:
        raise FetchError(f"bundle fetch failed: {type(exc).__name__}") from None
    except OSError as exc:
        # The local side (the part file): typed, never an untyped crash (D116).
        full = exc.errno in DISK_FULL_ERRNOS
        raise FetchError(
            f"cannot store the bundle: {exc.strerror or type(exc).__name__}",
            failure_type=FailureType.DISK_FULL if full else FailureType.INTERNAL_ERROR,
        ) from None
    finally:
        with contextlib.suppress(OSError):
            part.unlink()
    return size


def _check_status(status: int) -> None:
    if status == 200:
        return
    if status in (401, 403):
        raise FetchError(
            "the controller refused this attempt's fetch token",
            failure_type=FailureType.UNAUTHORIZED,
        )
    if status == 404:
        raise FetchError(
            "the controller does not have this bundle",
            failure_type=FailureType.NOT_FOUND,
        )
    raise FetchError(f"bundle fetch answered HTTP {status}")


async def upload_blob(
    base_url: str,
    digest: Digest,
    token: str,
    path: Path,
    *,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """Upload ``path`` (whose SHA-256 is ``digest``) with the upload grant.

    Returns the size sent. Raises :class:`UploadError` (see module docstring).
    """
    size = (await asyncio.to_thread(path.stat)).st_size
    url = f"{base_url}/v1/blobs/{digest.hex}"
    headers = {
        "authorization": f"Bearer {token}",
        "content-length": str(size),
        "content-type": "application/octet-stream",
    }
    delay = _UPLOAD_BACKOFF_S
    last = "no attempt made"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s), transport=transport, trust_env=False
    ) as client:
        for attempt in range(1, _UPLOAD_TRIES + 1):
            try:
                response = await client.put(url, content=_chunks(path), headers=headers)
            except httpx.TimeoutException:
                last = "timed out"
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            else:
                status = response.status_code
                if status in (200, 201):
                    return size
                if status == 401:
                    raise UploadError(
                        "the controller refused this attempt's upload grant (HTTP 401)",
                        retryable=True,
                        details={"refused": True},
                    )
                if status == 403:
                    raise UploadError(
                        "the attempt already ended for the controller (HTTP 403)",
                        retryable=False,
                    )
                if status in (411, 413):
                    raise UploadError(
                        "the upload is over this attempt's output quota "
                        f"(HTTP {status})",
                        retryable=False,
                    )
                if status == 507:
                    raise UploadError(
                        "the controller has no room for the upload (HTTP 507)",
                        retryable=True,
                    )
                last = f"HTTP {status}"
                if status != 503:
                    break
            if attempt < _UPLOAD_TRIES:
                await asyncio.sleep(delay)
                delay *= 2
    raise UploadError(f"upload failed: {last}", retryable=True)


async def _chunks(path: Path) -> AsyncIterator[bytes]:
    fh = await asyncio.to_thread(path.open, "rb")
    try:
        while chunk := await asyncio.to_thread(fh.read, _CHUNK):
            yield chunk
    finally:
        fh.close()


# ===================================================================== trees


def blob_url_ok(url: str) -> bool:
    """Empty (= the dialled host) or a bounded ``http(s)://host:port``."""
    return not url or (len(url) <= _MAX_URL_CHARS and bool(_SOURCE_URL.fullmatch(url)))


def token_ok(token: str) -> bool:
    """A bearer token of a sane shape (bounded, printable, no whitespace)."""
    return (
        0 < len(token) <= _MAX_TOKEN_CHARS
        and token.isprintable()
        and not any(ch.isspace() for ch in token)
    )


def tree_sources(
    spec: agent_pb2.ExecutionSpec,
    controller_host: str | None,
    *,
    sources: Sequence[agent_pb2.BlobSource] | None = None,
    scheme: str = "http",
) -> list[BlobSource]:
    """The validated blob sources of ``spec`` (see module docstring), or of
    ``sources`` (a named input's list, V0.5 G5) with the spec's blob location."""
    listed = list(spec.sources if sources is None else sources)
    if len(listed) > MAX_SOURCES:
        raise FetchError(
            f"more than {MAX_SOURCES} blob sources",
            failure_type=FailureType.INVALID_INPUT,
        )
    if not listed:
        listed = [agent_pb2.BlobSource(url=spec.blob_url, token=spec.fetch_token)]
    out: list[BlobSource] = []
    for source in listed:
        url = source.url
        if not blob_url_ok(url):
            raise FetchError(
                "a blob source URL is not http(s)://host:port",
                failure_type=FailureType.INVALID_INPUT,
            )
        if not token_ok(source.token):
            raise FetchError(
                "a blob source token is missing or malformed",
                failure_type=FailureType.INVALID_INPUT,
            )
        node_id = source.node_id
        if len(node_id) > _MAX_NODE_ID_CHARS or not node_id.isprintable():
            raise FetchError(
                "a blob source node_id is malformed",
                failure_type=FailureType.INVALID_INPUT,
            )
        if url:
            _require_scheme(url, scheme)
        base = (
            url.rstrip("/")
            if url
            else blob_base_url("", controller_host, int(spec.blob_port), scheme=scheme)
        )
        out.append(BlobSource(base_url=base, token=source.token, node_id=node_id))
    return out


@dataclass(frozen=True, slots=True)
class StageReport:
    tree: Tree
    blobs_hit: int
    blobs_fetched: int
    bytes_fetched: int
    chunks_resumed: int
    corrupt_refetches: int
    duration_s: float
    sources: dict[str, SourceStats] = field(default_factory=dict)
    """Bytes / request time / units per source (V0.5 D89; telemetry in G5)."""
    bytes_hit: int = 0
    manifest_hit: bool = False

    def to_proto(self) -> agent_pb2.TransferSummary:
        """The transfer telemetry of this tree (V0.5 G5, D93): bounded - at
        most :data:`MAX_SOURCES` + 1 sources, as listed."""
        summary = agent_pb2.TransferSummary(
            tree_id=str(self.tree.tree_id),
            blobs_hit=self.blobs_hit,
            bytes_hit=self.bytes_hit,
            blobs_fetched=self.blobs_fetched,
            bytes_fetched=self.bytes_fetched,
            chunks_resumed=self.chunks_resumed,
            corrupt_refetches=self.corrupt_refetches,
            manifest_hit=self.manifest_hit,
            duration_s=max(0.0, self.duration_s),
        )
        for name, src in list(self.sources.items())[: MAX_SOURCES + 1]:
            summary.sources.append(
                agent_pb2.TransferSource(
                    node_id="" if name == "controller" else name,
                    bytes=src.bytes,
                    duration_s=max(0.0, src.duration_s),
                    units=src.units,
                    failures=src.failures,
                    dropped=src.dropped or "",
                )
            )
        return summary


async def stage_tree(
    cas: AgentCas,
    *,
    tree_id: str,
    sources: Sequence[BlobSource],
    dest: Path,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    free_disk_floor_bytes: int = DEFAULT_FREE_DISK_FLOOR_BYTES,
    clock: Callable[[], float] = time.monotonic,
    tuning: FetchTuning | None = None,
    on_tree: Callable[[Tree], Awaitable[None]] | None = None,
) -> StageReport:
    """Fetch ``tree_id`` into the cache and copy it into ``dest``.

    ``on_tree`` runs once the manifest is verified and before any blob is
    looked at (V0.5 G5: the GC lease records the tree's members there)."""
    try:
        digest = Digest.parse(tree_id)
    except ValueError:
        raise FetchError(
            "ExecutionSpec.tree_id is not sha256:<64 hex>",
            failure_type=FailureType.INVALID_INPUT,
        ) from None
    started = clock()
    await cas.prepare()
    refetches = 0
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_s), transport=transport, trust_env=False
    ) as client:
        fetcher = BlobFetcher(cas, sources, client, tuning=tuning, clock=clock)
        tree = await fetcher.tree(digest)
        if on_tree is not None:
            await on_tree(tree)
        await fetcher.ensure_all(tree)
        while True:
            try:
                await _uninterrupted(
                    asyncio.to_thread(
                        materialise,
                        cas.store,
                        tree,
                        dest,
                        free_disk_floor_bytes=free_disk_floor_bytes,
                    )
                )
                break
            except CorruptBlobError as exc:
                refetches += 1
                log.warning(
                    "agent.cas.blob_corrupt", digest=str(exc.digest), refetch=refetches
                )
                if not fetcher.allow_refetch(exc.digest):
                    raise FetchError(
                        f"blob {exc.digest} is corrupt and its re-fetch budget is "
                        "spent; it was not used",
                        failure_type=FailureType.INTEGRITY_ERROR,
                    ) from None
                entry = next(e for e in tree.files() if e.sha256 == exc.digest.hex)
                await fetcher.ensure(entry, tree.chunk_size)
            except MaterialiseError as exc:
                raise FetchError(exc.message, failure_type=exc.failure_type) from None
    # Every member was just verified while copying: the tree is complete here.
    cas.tree_staged(tree)
    stats = fetcher.stats
    return StageReport(
        tree=tree,
        blobs_hit=stats.blobs_hit,
        blobs_fetched=stats.blobs_fetched,
        bytes_fetched=stats.bytes_fetched,
        chunks_resumed=stats.chunks_resumed,
        corrupt_refetches=refetches,
        duration_s=clock() - started,
        sources=dict(stats.sources),
        bytes_hit=stats.bytes_hit,
        manifest_hit=stats.manifest_hit,
    )


async def _uninterrupted[T](work: Awaitable[T]) -> T:
    """Await ``work`` to completion even if cancelled meanwhile, then re-raise.

    The copy runs in a thread that a cancellation cannot stop; returning early
    would let the caller delete the workdir while the thread still writes in it.
    """
    future = asyncio.ensure_future(work)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not future.cancelled():
            future.exception()  # retrieved on purpose: the stop wins
        raise asyncio.CancelledError
    return future.result()
