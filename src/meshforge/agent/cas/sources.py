"""Blob sources and the HTTP requests made to them (V0.5 G1 D80, G4 D89).

A *source* is the controller or a peer that holds the tree
(``ExecutionSpec.sources``). This module owns what is per source: its identity
(:class:`BlobSource`, never showing the token), its counters
(:class:`SourceStats`), the typed errors a request to it can end with
(:class:`FetchError`) and the two GETs every transfer is made of - a whole body
streamed somewhere, and one ``Range`` chunk. Tokens travel only in the
``Authorization`` header and are never logged.

The scheduling of units across sources lives in :mod:`.scheduler`; the tree
manifest download in :mod:`.manifest`; the facade in :mod:`.fetcher`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

import httpx

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.hashing import Digest

__all__ = [
    "CONTROLLER",
    "STREAM",
    "BlobSource",
    "Corrupt",
    "FetchError",
    "FetchStats",
    "FetchTuning",
    "Source",
    "SourceStats",
    "get_range",
    "status_error",
]

STREAM: Final = 1024 * 1024
"""Read size of a streamed response body."""
CONTROLLER: Final = "controller"


class FetchError(MeshForgeError):
    default_failure_type = FailureType.TRANSFER_ERROR

    @property
    def source(self) -> str:
        """``controller`` or a peer's node id ("" when not source-specific)."""
        return str(self.details.get("source", ""))


@dataclass(frozen=True, slots=True)
class BlobSource:
    base_url: str
    """``http(s)://host:port`` (no trailing slash)."""
    token: str
    node_id: str = ""
    """Empty = the controller."""

    def __repr__(self) -> str:  # never show the token
        return f"BlobSource(base_url={self.base_url!r}, node_id={self.node_id!r})"

    @property
    def name(self) -> str:
        return self.node_id or CONTROLLER


@dataclass(frozen=True, slots=True)
class FetchTuning:
    """``[agent.fetch]`` knobs (see :class:`~meshforge.agent.settings.FetchSettings`)."""

    per_source: int = 2
    slow_source_factor: float = 4.0
    slow_min_units: int = 2
    endgame_after_s: float = 0.5


@dataclass(slots=True)
class SourceStats:
    bytes: int = 0
    """Payload bytes received from the source (wasted duplicates included)."""
    duration_s: float = 0.0
    """Time spent in requests to it (monotonic; overlapping requests add up)."""
    units: int = 0
    """Units it won (chunks or whole blobs)."""
    failures: int = 0
    dropped: str | None = None
    """Why it was dropped for this transfer (None = it stayed)."""


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
    refetches: int = 0
    """Whole blobs fetched again after a mismatch (cached or in flight)."""
    duplicated_units: int = 0
    """End-game duplicates started (a unit asked from a second source)."""
    sources: dict[str, SourceStats] = field(default_factory=dict)
    """Per source (``controller`` or the peer's node id)."""


def status_error(status: int, what: str, source: str) -> FetchError:
    details = {"source": source, "status": status}
    if status in (401, 403):
        return FetchError(
            f"the source refused this attempt's fetch token for {what}",
            failure_type=FailureType.UNAUTHORIZED,
            details=details,
        )
    if status == 404:
        return FetchError(
            f"the source does not have {what}",
            failure_type=FailureType.NOT_FOUND,
            details=details,
        )
    return FetchError(f"fetching {what} answered HTTP {status}", details=details)


class Corrupt(Exception):  # noqa: N818 - internal control flow
    """Bytes from a source did not match their digest (nothing was kept)."""


@dataclass(eq=False, slots=True)
class Source:
    """One listed source during one transfer."""

    src: BlobSource
    stats: SourceStats
    dropped: FetchError | None = None

    @property
    def name(self) -> str:
        return self.src.name

    @property
    def controller(self) -> bool:
        return not self.src.node_id

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        return {"authorization": f"Bearer {self.src.token}", **(extra or {})}

    def url(self, digest: Digest) -> str:
        return f"{self.src.base_url}/v1/blobs/{digest.hex}"


async def get_range(
    client: httpx.AsyncClient,
    source: Source,
    digest: Digest,
    start: int,
    length: int,
    count: Callable[[Source, int], None],
) -> bytes:
    """``Range: bytes=start-(start+length-1)`` of ``digest``; exactly ``length``
    bytes or a typed :class:`FetchError` (``count`` sees every received piece)."""
    headers = source.headers({"range": f"bytes={start}-{start + length - 1}"})
    try:
        async with client.stream(
            "GET", source.url(digest), headers=headers
        ) as response:
            if response.status_code != 206:
                raise status_error(response.status_code, f"blob {digest}", source.name)
            data = bytearray()
            async for piece in response.aiter_raw(STREAM):
                data += piece
                count(source, len(piece))
                if len(data) > length:
                    raise FetchError(
                        "a range answer is longer than asked for",
                        details={"source": source.name},
                    )
    except httpx.TimeoutException:
        raise FetchError(
            f"timed out fetching blob {digest}",
            failure_type=FailureType.TIMEOUT,
            details={"source": source.name},
        ) from None
    except httpx.HTTPError as exc:
        raise FetchError(
            f"chunk fetch failed: {type(exc).__name__}",
            details={"source": source.name},
        ) from None
    if len(data) != length:
        raise FetchError("a range answer ended early", details={"source": source.name})
    return bytes(data)
