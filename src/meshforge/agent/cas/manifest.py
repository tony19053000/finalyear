"""Download one tree manifest from a source (V0.5 G1 D81, G4 D89).

The manifest is the first thing a staging needs and it is small and bounded
(``TreeLimits.max_manifest_bytes``), so it is one plain ``GET`` streamed into
memory: a body over the bound is ``invalid_input``, a body that does not hash to
``tree_id`` is ``integrity_error`` and nothing of it is kept. Which source is
asked, in which order, and what a failure means for that source is decided by
:meth:`meshforge.agent.cas.fetcher.BlobFetcher.tree`.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import httpx

from meshforge.agent.cas.sources import STREAM, FetchError, Source, status_error
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest

__all__ = ["get_manifest"]


async def get_manifest(
    client: httpx.AsyncClient,
    source: Source,
    tree_id: Digest,
    *,
    max_bytes: int,
    count: Callable[[Source, int], None],
    clock: Callable[[], float],
) -> bytes:
    """The verified manifest bytes of ``tree_id`` from ``source`` (module doc)."""
    started = clock()
    try:
        async with client.stream(
            "GET", source.url(tree_id), headers=source.headers()
        ) as response:
            if response.status_code != 200:
                raise status_error(
                    response.status_code, "the tree manifest", source.name
                )
            data = bytearray()
            async for piece in response.aiter_raw(STREAM):
                data += piece
                count(source, len(piece))
                if len(data) > max_bytes:
                    raise FetchError(
                        f"tree manifest larger than {max_bytes} bytes",
                        failure_type=FailureType.INVALID_INPUT,
                        details={"source": source.name},
                    )
    except httpx.TimeoutException:
        raise FetchError(
            "timed out fetching the tree manifest",
            failure_type=FailureType.TIMEOUT,
            details={"source": source.name},
        ) from None
    except httpx.HTTPError as exc:
        raise FetchError(
            f"manifest fetch failed: {type(exc).__name__}",
            details={"source": source.name},
        ) from None
    finally:
        source.stats.duration_s += clock() - started
    if hashlib.sha256(data).hexdigest() != tree_id.hex:
        raise FetchError(
            "the tree manifest does not match its id; nothing was kept",
            failure_type=FailureType.INTEGRITY_ERROR,
            details={"source": source.name},
        )
    return bytes(data)
