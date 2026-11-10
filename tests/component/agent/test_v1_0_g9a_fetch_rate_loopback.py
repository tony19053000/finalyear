"""V1.0 G9-A item 4 (D431) on real sockets: the fetch rate cap slows a transfer.

A real loopback HTTP server (:mod:`mf_blob_range_server`) serves a 4 MiB
tree; :func:`stage_tree` fetches it (real httpx, real CAS, two requests in
flight) once without a cap and once with a 24 Mbit/s cap (3 MB/s). Bounds are
generous and machine-independent: the capped run cannot finish before the
bucket allows (``(bytes - burst) / rate`` = 1.15 s; asserted >= 85 % of it), and it
is clearly slower than the uncapped one; the bytes all arrive and verify.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from mf_blob_range_server import BlobRangeServer

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import BlobSource, FetchTuning
from meshforge.agent.cas.rate import FetchRateLimiter
from meshforge.agent.execution.fetch import stage_tree
from meshforge.common.cas import CasStore
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

MIB = 1024 * 1024
SIZE = 4 * MIB
CAP_MBIT = 24.0


def _tree(tmp_path: Path) -> tuple[BuiltTree, bytes]:
    root = tmp_path / "proj"
    root.mkdir()
    data = os.urandom(SIZE)
    (root / "data.bin").write_bytes(data)
    return build_tree(root, chunk_size=MIB), data


async def _stage(
    tmp_path: Path, name: str, server: BlobRangeServer, built: BuiltTree, **kw: object
) -> float:
    cas = AgentCas(CasStore(tmp_path / f"cas-{name}", max_blob_bytes=1 << 30))
    started = time.monotonic()
    report = await stage_tree(
        cas,
        tree_id=str(built.tree.tree_id),
        sources=[BlobSource(base_url=server.url, token="tok")],
        dest=tmp_path / f"work-{name}",
        tuning=FetchTuning(per_source=2),
        **kw,  # type: ignore[arg-type]
    )
    assert report.bytes_fetched >= SIZE
    return time.monotonic() - started


async def test_a_capped_transfer_is_measurably_slower(tmp_path: Path) -> None:
    built, data = _tree(tmp_path)
    blobs = {h: p.read_bytes() for h, p in built.sources.items()}
    blobs[built.tree.tree_id.hex] = built.manifest
    server = BlobRangeServer(blobs)
    await server.start()
    try:
        free = await _stage(tmp_path, "free", server, built)
        rate = FetchRateLimiter.from_mbit(CAP_MBIT)
        capped = await _stage(tmp_path, "capped", server, built, rate=rate)
    finally:
        await server.close()
    assert (tmp_path / "work-capped" / "data.bin").read_bytes() == data
    floor = (SIZE - rate.burst) / rate.rate  # 1.15 s: what the bucket allows
    assert capped >= 0.85 * floor, (capped, floor)
    assert capped > free + 0.5, (capped, free)
    assert rate.charged_bytes >= SIZE
    assert capped < 30  # it still finishes
