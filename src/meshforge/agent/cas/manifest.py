"""Download one tree manifest from a source (V0.5 G1 D81, G4 D89).

The manifest is the first thing a staging needs and it is small and bounded
(``TreeLimits.max_manifest_bytes``), so it is one plain ``GET`` streamed into
memory: a body over the bound is ``invalid_input``, a body that does not hash to
``tree_id`` is ``integrity_error`` and nothing of it is kept. Which source is
asked, in which order, and what a failure means for that source is decided by
:meth:`meshforge.agent.cas.fetcher.BlobFetcher.tree`.

Hashing and parsing a manifest is CPU work proportional to its size; the bound
is 64 MiB by default (seconds of JSON parsing), so anything over
:data:`INLINE_BYTES` is hashed and parsed in a worker thread - the event loop
(heartbeats, the peer server, other stagings) never waits for it (G4 reviewer
carry-over 3). Small manifests stay inline: a thread hop would cost more.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from typing import Final

import httpx

from meshforge.agent.cas.sources import STREAM, FetchError, Source, status_error
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.tree import Tree, TreeLimits, parse_tree

__all__ = ["INLINE_BYTES", "get_manifest", "load_tree"]

INLINE_BYTES: Final = 64 * 1024
"""Manifests up to this size are hashed/parsed on the loop; bigger ones in a
thread."""


def _sha256_hex(data: bytes | bytearray) -> str:
    return hashlib.sha256(data).hexdigest()


async def _off_loop_if_large[T](
    size: int, fn: Callable[..., T], *args: object, **kwargs: object
) -> T:
    if size > INLINE_BYTES:
        return await asyncio.to_thread(fn, *args, **kwargs)
    return fn(*args, **kwargs)


async def load_tree(data: bytes, tree_id: Digest, limits: TreeLimits) -> Tree:
    """:func:`~meshforge.common.tree.parse_tree` (verifying ``tree_id``), off
    the loop for a large manifest; raises ``TreeError``."""
    return await _off_loop_if_large(
        len(data), parse_tree, data, expected=tree_id, limits=limits
    )


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
    if await _off_loop_if_large(len(data), _sha256_hex, data) != tree_id.hex:
        raise FetchError(
            "the tree manifest does not match its id; nothing was kept",
            failure_type=FailureType.INTEGRITY_ERROR,
            details={"source": source.name},
        )
    return bytes(data)
