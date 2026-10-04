"""V0.5 G4 D89: the work-stealing multi-source fetcher with fake sources.

One fake HTTP surface answers for several hosts (``ctl`` = the controller,
``pa`` / ``pb`` = peers), each with its own per-request delay and failure mode;
everything is real except the network: real CAS, real resumable parts, real
verification. Delays are a few milliseconds (no sleep > 50 ms).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import BlobFetcher, BlobSource, FetchError, FetchTuning
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.errors import FailureType
from meshforge.common.hashing import sha256_bytes
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

CS = MIN_CHUNK_SIZE
CTL = BlobSource(base_url="http://ctl:1", token="tc")
PA = BlobSource(base_url="http://pa:47120", token="ta", node_id="node_a")
PB = BlobSource(base_url="http://pb:47120", token="tb", node_id="node_b")


@dataclass
class Host:
    delay_s: float = 0.0
    status: int | None = None
    """Answer this status to every blob request."""
    corrupt: bool = False
    fail_after: int | None = None
    """Raise a transport error from the n-th request on."""
    missing: set[str] = field(default_factory=set)
    requests: int = 0
    served: int = 0


class Fabric:
    def __init__(self, built: BuiltTree, hosts: dict[str, Host]) -> None:
        self.blobs = {h: p.read_bytes() for h, p in built.sources.items()}
        self.blobs[built.tree.tree_id.hex] = built.manifest
        self.hosts = hosts
        self.tokens = {"ctl": "tc", "pa": "ta", "pb": "tb"}

    async def handler(self, request: httpx.Request) -> httpx.Response:
        host = self.hosts[request.url.host]
        host.requests += 1
        if host.delay_s:
            await asyncio.sleep(host.delay_s)
        if host.fail_after is not None and host.requests >= host.fail_after:
            raise httpx.ReadError("peer went away")
        if (
            request.headers.get("authorization")
            != f"Bearer {self.tokens[request.url.host]}"
        ):
            return httpx.Response(401)
        if host.status is not None:
            return httpx.Response(host.status)
        hex_ = request.url.path.rsplit("/", 1)[-1]
        data = self.blobs.get(hex_)
        if data is None or hex_ in host.missing:
            return httpx.Response(404)
        status = 200
        rng = request.headers.get("range")
        if rng:
            a, b = rng.removeprefix("bytes=").split("-")
            data = data[int(a) : int(b) + 1]
            status = 206
        if host.corrupt and len(data) > 8:
            data = data[:-1] + bytes([data[-1] ^ 0xFF])
        host.served += len(data)
        return httpx.Response(status, stream=httpx.ByteStream(data))

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _project(tmp_path: Path, chunks: int = 16) -> tuple[BuiltTree, bytes]:
    root = tmp_path / "proj"
    root.mkdir()
    data = os.urandom(chunks * CS)
    (root / "big.bin").write_bytes(data)
    (root / "small.txt").write_bytes(b"small file\n")
    return build_tree(root, chunk_size=CS), data


def _cas(tmp_path: Path) -> AgentCas:
    cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 30))
    cas.store.prepare()
    return cas


TUNING = FetchTuning(
    per_source=2, slow_source_factor=4.0, slow_min_units=2, endgame_after_s=0.05
)


async def _fetch(
    tmp_path: Path,
    hosts: dict[str, Host],
    sources: list[BlobSource],
    *,
    tuning: FetchTuning = TUNING,
    chunks: int = 16,
    prepare: Callable[[AgentCas, BuiltTree], None] | None = None,
) -> tuple[BlobFetcher, AgentCas, bytes, Fabric]:
    built, data = _project(tmp_path, chunks)
    fabric = Fabric(built, hosts)
    cas = _cas(tmp_path)
    if prepare is not None:
        prepare(cas, built)
    async with fabric.client() as client:
        fetcher = BlobFetcher(cas, sources, client, tuning=tuning)
        tree = await fetcher.tree(built.tree.tree_id)
        await fetcher.ensure_all(tree)
    return fetcher, cas, data, fabric


async def test_fast_source_takes_more_chunks(tmp_path: Path) -> None:
    hosts = {"ctl": Host(delay_s=0.02), "pa": Host(delay_s=0.001)}
    fetcher, cas, data, _ = await _fetch(tmp_path, hosts, [CTL, PA])
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data
    stats = fetcher.stats.sources
    assert stats["node_a"].units > stats["controller"].units
    assert stats["node_a"].bytes > stats["controller"].bytes
    assert stats["controller"].dropped is None and stats["node_a"].dropped is None


async def test_failing_peer_is_dropped_and_the_controller_completes(
    tmp_path: Path,
) -> None:
    hosts = {"ctl": Host(), "pa": Host(fail_after=3)}
    fetcher, cas, data, _ = await _fetch(tmp_path, hosts, [CTL, PA])
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data
    assert fetcher.stats.sources["node_a"].dropped == "transfer_error"


async def test_all_peers_fail_controller_completes(tmp_path: Path) -> None:
    hosts = {
        "ctl": Host(delay_s=0.005),
        "pa": Host(status=500),
        "pb": Host(fail_after=1),
    }
    fetcher, cas, data, _ = await _fetch(tmp_path, hosts, [CTL, PA, PB])
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data
    assert fetcher.stats.sources["node_a"].dropped is not None
    assert fetcher.stats.sources["node_b"].dropped is not None


async def test_peer_401_drops_it_at_once_no_waiting(tmp_path: Path) -> None:
    hosts = {"ctl": Host(), "pa": Host()}
    built, data = _project(tmp_path)
    fabric = Fabric(built, hosts)
    fabric.tokens["pa"] = "something-else"  # the peer refuses our token
    cas = _cas(tmp_path)
    async with fabric.client() as client:
        fetcher = BlobFetcher(cas, [CTL, PA], client, tuning=TUNING)
        tree = await fetcher.tree(built.tree.tree_id)
        await asyncio.wait_for(fetcher.ensure_all(tree), 5)
    assert fetcher.stats.sources["node_a"].dropped == "unauthorized"
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data


async def test_corrupt_peer_is_dropped_and_nothing_corrupt_is_kept(
    tmp_path: Path,
) -> None:
    hosts = {"ctl": Host(delay_s=0.005), "pa": Host(corrupt=True)}
    fetcher, cas, data, _ = await _fetch(tmp_path, hosts, [CTL, PA])
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data
    assert fetcher.stats.sources["node_a"].dropped == "integrity_error"
    assert fetcher.stats.corrupt_chunks >= 1
    assert cas.store.verify(sha256_bytes(data))


async def test_holder_404_is_excluded_for_that_blob_only(tmp_path: Path) -> None:
    built, data = _project(tmp_path)
    big = sha256_bytes(data).hex
    hosts = {"ctl": Host(delay_s=0.01), "pa": Host(missing={big})}
    fabric = Fabric(built, hosts)
    cas = _cas(tmp_path)
    async with fabric.client() as client:
        fetcher = BlobFetcher(cas, [CTL, PA], client, tuning=TUNING)
        await fetcher.ensure_all(await fetcher.tree(built.tree.tree_id))
    assert fetcher.stats.sources["node_a"].dropped is None  # still a source
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data


async def test_slow_peer_is_dropped(tmp_path: Path) -> None:
    hosts = {
        "ctl": Host(delay_s=0.001),
        "pa": Host(delay_s=0.001),
        "pb": Host(delay_s=0.04),
    }
    fetcher, cas, data, _ = await _fetch(
        tmp_path,
        hosts,
        [CTL, PA, PB],
        tuning=FetchTuning(
            per_source=1, slow_source_factor=4.0, slow_min_units=1, endgame_after_s=5.0
        ),
        chunks=24,
    )
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data
    assert fetcher.stats.sources["node_b"].dropped == "transfer_error"
    assert fetcher.stats.sources["controller"].dropped is None


async def test_slow_controller_is_never_dropped(tmp_path: Path) -> None:
    hosts = {
        "ctl": Host(delay_s=0.03),
        "pa": Host(delay_s=0.001),
        "pb": Host(delay_s=0.001),
    }
    fetcher, *_ = await _fetch(
        tmp_path,
        hosts,
        [CTL, PA, PB],
        tuning=FetchTuning(
            per_source=1, slow_source_factor=2.0, slow_min_units=1, endgame_after_s=5.0
        ),
    )
    assert fetcher.stats.sources["controller"].dropped is None


async def test_endgame_duplicates_the_last_chunk_held_by_a_slow_source(
    tmp_path: Path,
) -> None:
    # The controller holds a chunk for 40 ms; the fast peer re-asks it after 5 ms.
    hosts = {"ctl": Host(delay_s=0.04), "pa": Host()}
    fetcher, cas, data, _ = await _fetch(
        tmp_path,
        hosts,
        [CTL, PA],
        tuning=FetchTuning(
            per_source=1,
            slow_source_factor=100.0,
            slow_min_units=50,
            endgame_after_s=0.005,
        ),
        chunks=4,
    )
    assert fetcher.stats.duplicated_units >= 1
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data


async def test_controller_refusal_with_no_peer_left_is_unauthorized(
    tmp_path: Path,
) -> None:
    built, _data = _project(tmp_path)
    hosts = {"ctl": Host(), "pa": Host(status=500)}
    fabric = Fabric(built, hosts)
    cas = _cas(tmp_path)
    async with fabric.client() as client:
        fetcher = BlobFetcher(cas, [CTL, PA], client, tuning=TUNING)
        tree = await fetcher.tree(built.tree.tree_id)
        fabric.tokens["ctl"] = "rotated"  # e.g. the controller restarted
        with pytest.raises(FetchError) as info:
            await asyncio.wait_for(fetcher.ensure_all(tree), 5)
    assert info.value.failure_type is FailureType.UNAUTHORIZED
    assert info.value.source == "controller"


async def test_peers_alone_finish_when_the_controller_refuses(tmp_path: Path) -> None:
    built, data = _project(tmp_path)
    hosts = {"ctl": Host(), "pa": Host()}
    fabric = Fabric(built, hosts)
    fabric.tokens["ctl"] = "rotated"
    cas = _cas(tmp_path)
    async with fabric.client() as client:
        fetcher = BlobFetcher(cas, [CTL, PA], client, tuning=TUNING)
        # The manifest also comes from the peer when the controller refuses.
        await fetcher.ensure_all(await fetcher.tree(built.tree.tree_id))
    assert fetcher.stats.sources["controller"].dropped == "unauthorized"
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data


async def test_peer_killed_mid_transfer_controller_resumes_verified_chunks(
    tmp_path: Path,
) -> None:
    """Criterion 3 with peers: chunks the peer delivered are never re-sent."""
    hosts = {"ctl": Host(delay_s=0.01), "pa": Host(fail_after=6)}
    _fetcher, cas, data, fabric = await _fetch(tmp_path, hosts, [CTL, PA], chunks=12)
    assert cas.store.path_for(sha256_bytes(data)).read_bytes() == data
    total = 12 * CS + len(b"small file\n")
    sent = fabric.hosts["ctl"].served + fabric.hosts["pa"].served
    manifest = len(
        fabric.blobs[next(h for h in fabric.blobs if fabric.blobs[h].startswith(b"{"))]
    )
    # Everything was sent exactly once (plus the manifest once): no chunk twice.
    assert sent == total + manifest
