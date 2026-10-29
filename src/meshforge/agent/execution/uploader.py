"""Resumable output uploads under an attempt's upload grant (V0.6 G1, D114).

A blob no larger than one part (``part_bytes``, the final logs usually) goes up
with one verified ``PUT`` (:func:`~.fetch.upload_blob`): an interruption loses at
most one part's worth of bytes anyway. A larger one goes up in **parts** through
the controller's parts API with the attempt's upload grant:

1. ``GET /v1/blobs/{hex}/parts`` - what the controller already holds of this
   blob (parts are files on the controller's disk: they survive a controller
   restart; the grant does not);
2. ``PUT /v1/blobs/{hex}/parts/{i}`` for every part it lacks (each with its own
   SHA-256 and an exact ``Content-Length``), read from the local file in a
   thread, in the controller's chunk size when it already has a partial upload;
3. ``POST /v1/blobs/{hex}/complete`` - the controller assembles and verifies
   the blob and **credits** it to the grant. ``credited: false`` (the blob was
   already stored before this call, so nothing proves this attempt sent it) ->
   the bytes go up once more with a plain ``PUT``, which verifies them under the
   grant.

A part already stored before an interruption is never sent again (resume).
Errors are :class:`~.fetch.UploadError`: ``details["refused"]`` = the
controller refused the grant (``401``: a restarted controller does not know it -
the caller waits for ``RefreshUploadGrant``); not retryable = the attempt ended
(``403``), the quota is exceeded (``411``/``413``) or the controller holds a
different upload under this digest; anything else (connection refused, timeouts,
``5xx``, ``507``, a part that arrived corrupt, an unreadable answer) is
retryable. The caller (the executor) owns retries, backoff and the
``upload_retry_s`` budget. The grant travels only in the ``Authorization``
header and is never logged.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx

from meshforge.agent.execution.fetch import UploadError, upload_blob
from meshforge.agent.http_refusal import CONTROLLER_LABEL, transport_failure
from meshforge.common.cas import MAX_CHUNK_SIZE, MIN_CHUNK_SIZE, chunk_span
from meshforge.common.hashing import Digest

__all__ = ["DEFAULT_PART_BYTES", "UploadProgress", "upload_parts"]

DEFAULT_PART_BYTES: Final = 8 * 1024 * 1024
_MAX_ROUNDS: Final = 3
"""``complete`` answered "parts missing / start over" this often in a row ->
a retryable error (the caller's backoff takes over)."""

type PutBlob = Callable[..., Awaitable[int]]


@dataclass(slots=True)
class UploadProgress:
    """What the uploads of one attempt sent (tests and logs count resumed parts)."""

    parts_sent: int = 0
    parts_skipped: int = 0
    bytes_sent: int = 0
    whole_puts: int = 0
    """Blobs sent with one plain ``PUT`` (small, or an uncredited ``complete``)."""
    sent_indices: list[int] = field(default_factory=list)


def _status_error(status: int, what: str) -> UploadError:
    if status == 401:
        return UploadError(
            f"the controller refused the upload grant ({what}, HTTP 401)",
            retryable=True,
            details={"refused": True},
        )
    if status == 403:
        return UploadError(
            f"the attempt already ended for the controller ({what}, HTTP 403)",
            retryable=False,
        )
    if status in (411, 413):
        return UploadError(
            f"the upload is over this attempt's output quota ({what}, HTTP {status})",
            retryable=False,
        )
    return UploadError(f"{what} answered HTTP {status}", retryable=True)


def _read_part(path: Path, start: int, length: int) -> bytes:
    with path.open("rb") as fh:
        fh.seek(start)
        data = fh.read(length)
    if len(data) != length:
        raise OSError(f"the file changed while uploading (short read at {start})")
    return data


@dataclass(frozen=True, slots=True)
class _Held:
    """The controller's answer to ``GET .../parts``."""

    stored: bool
    chunk_size: int
    received: frozenset[int]


def _parse_status(response: httpx.Response, size: int, part_bytes: int) -> _Held:
    if response.status_code == 404:
        return _Held(stored=False, chunk_size=part_bytes, received=frozenset())
    if response.status_code != 200:
        raise _status_error(response.status_code, "the part status")
    info = response.json()
    if not isinstance(info, dict):
        raise TypeError("the part status is not an object")
    if info.get("complete") is True:
        return _Held(stored=True, chunk_size=0, received=frozenset())
    if int(info["size"]) != size:
        raise UploadError(
            "the controller holds a different upload under this digest",
            retryable=False,
        )
    chunk_size = int(info["chunk_size"])
    if not MIN_CHUNK_SIZE <= chunk_size <= MAX_CHUNK_SIZE:
        raise ValueError("the part status names an impossible chunk size")
    count = -(-size // chunk_size)
    raw = info.get("received", [])
    if not isinstance(raw, list) or len(raw) > count:
        raise ValueError("the part status lists more parts than the blob has")
    return _Held(
        stored=False,
        chunk_size=chunk_size,
        received=frozenset(int(i) for i in raw if 0 <= int(i) < count),
    )


async def upload_parts(
    base_url: str,
    digest: Digest,
    token: str,
    path: Path,
    *,
    part_bytes: int = DEFAULT_PART_BYTES,
    timeout_s: float = 60.0,
    transport: httpx.AsyncBaseTransport | None = None,
    progress: UploadProgress | None = None,
    put: PutBlob = upload_blob,
) -> int:
    """Upload ``path`` (whose SHA-256 is ``digest``) resumably; returns the
    bytes sent by this call. Raises :class:`UploadError` (module docstring)."""
    if not MIN_CHUNK_SIZE <= part_bytes <= MAX_CHUNK_SIZE:
        raise ValueError(f"part_bytes must be in [{MIN_CHUNK_SIZE}, {MAX_CHUNK_SIZE}]")
    track = progress if progress is not None else UploadProgress()
    size = (await asyncio.to_thread(path.stat)).st_size
    if size <= part_bytes:  # one part at most: one verified PUT
        await put(base_url, digest, token, path, transport=transport)
        track.whole_puts += 1
        track.bytes_sent += size
        return size
    base = f"{base_url}/v1/blobs/{digest.hex}"
    auth = {"authorization": f"Bearer {token}"}
    sent_before = track.bytes_sent
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s), transport=transport, trust_env=False
        ) as client:
            for _round in range(_MAX_ROUNDS):
                held = _parse_status(
                    await client.get(f"{base}/parts", headers=auth), size, part_bytes
                )
                if not held.stored:
                    await _send_parts(client, base, auth, path, size, held, track)
                done = await client.post(f"{base}/complete", headers=auth)
                if done.status_code in (200, 201):
                    if done.json().get("credited") is True:
                        return track.bytes_sent - sent_before
                    # Stored before, by someone: prove it with the bytes.
                    await put(base_url, digest, token, path, transport=transport)
                    track.whole_puts += 1
                    track.bytes_sent += size
                    return track.bytes_sent - sent_before
                if done.status_code in (400, 404, 409):
                    # 409 parts missing (one was swept or lost), 400 the
                    # assembled bytes were wrong (the parts start over), 404
                    # the upload was swept meanwhile: look again.
                    continue
                raise _status_error(done.status_code, "complete")
    except httpx.TimeoutException:
        raise UploadError("the upload timed out", retryable=True) from None
    except httpx.HTTPError as exc:
        raise UploadError(
            f"the upload failed: {transport_failure(exc, server=CONTROLLER_LABEL)}",
            retryable=True,
        ) from None
    except OSError as exc:  # the local file (not the network): no retry helps
        raise UploadError(
            f"could not read the file to upload: {type(exc).__name__}",
            retryable=False,
        ) from None
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise UploadError(
            f"the controller's answer was unreadable: {type(exc).__name__}",
            retryable=True,
        ) from None
    raise UploadError("the controller kept reporting missing parts", retryable=True)


async def _send_parts(
    client: httpx.AsyncClient,
    base: str,
    auth: dict[str, str],
    path: Path,
    size: int,
    held: _Held,
    track: UploadProgress,
) -> None:
    chunk_size = held.chunk_size
    count = -(-size // chunk_size)
    for index in range(count):
        if index in held.received:
            track.parts_skipped += 1
            continue
        start, length = chunk_span(index, size, chunk_size)
        data = await asyncio.to_thread(_read_part, path, start, length)
        response = await client.put(
            f"{base}/parts/{index}",
            content=data,
            headers={
                **auth,
                "content-length": str(length),
                "content-type": "application/octet-stream",
                "x-meshforge-blob-size": str(size),
                "x-meshforge-chunk-size": str(chunk_size),
                "x-meshforge-chunk-sha256": hashlib.sha256(data).hexdigest(),
            },
        )
        if response.status_code not in (200, 201):
            raise _status_error(response.status_code, f"part {index}")
        track.parts_sent += 1
        track.bytes_sent += length
        track.sent_indices.append(index)
