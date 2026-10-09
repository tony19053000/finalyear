"""V0.5 G4 D97/D98: the agent's peer blob server on a real loopback socket.

Covers the HTTP surface (GET/HEAD, one Range, 405/404/400/416/431), the token
checks (none/forged/expired/other holder/member not in the tree/no manifest/no
key), the bounds (header bytes, slowloris idle deadline, connection cap,
requests per connection) and that only CAS files are served.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.agent.cas import AgentCas
from meshforge.agent.peer import PeerAuthorizer, PeerBlobServer
from meshforge.agent.peer.server import COALESCE_BYTES
from meshforge.agent.settings import PeerSettings
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.common.peer_token import sign_peer_token
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

NOW = 1_800_000_000.0
WILDCARD = "0.0.0.0"  # noqa: S104 - only parsed by the settings, never bound


@dataclass
class Env:
    cas: AgentCas
    key: Ed25519PrivateKey
    holder: str
    built: BuiltTree
    other: BuiltTree
    server: PeerBlobServer
    auth: PeerAuthorizer
    base: str
    clock: list[float]

    def token(
        self,
        *,
        tree: Digest | None = None,
        holder: str | None = None,
        key: Ed25519PrivateKey | None = None,
        now: float | None = None,
    ) -> str:
        return sign_peer_token(
            key or self.key,
            tree=tree or self.built.tree.tree_id,
            requester=new_node_id(),
            holder=holder or self.holder,
            attempt_id=new_attempt_id(),
            now=NOW if now is None else now,
        )

    def url(self, digest: Digest | str) -> str:
        hex_ = digest.hex if isinstance(digest, Digest) else digest
        return f"{self.base}/v1/blobs/{hex_}"


def _store(cas: AgentCas, built: BuiltTree) -> None:
    for hex_, path in built.sources.items():
        cas.store.put_file(path, Digest(hex_))
    cas.store.put_bytes(built.manifest, built.tree.tree_id)


def _raw_key(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


async def _make(tmp_path: Path, **overrides: object) -> Env:
    cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 30))
    cas.store.prepare()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "small.txt").write_bytes(b"hello peer\n")
    (proj / "big.bin").write_bytes(os.urandom(COALESCE_BYTES * 2 + 123))
    (proj / "empty.txt").write_bytes(b"")
    built = build_tree(proj, chunk_size=MIN_CHUNK_SIZE)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    (other_dir / "secret.txt").write_bytes(b"only in tree Y\n")
    other = build_tree(other_dir, chunk_size=MIN_CHUNK_SIZE)
    _store(cas, built)
    _store(cas, other)
    key = Ed25519PrivateKey.generate()
    holder = new_node_id()
    clock = [NOW]
    auth = PeerAuthorizer(cas, node_id=holder, wall_clock=lambda: clock[0])
    auth.set_key(_raw_key(key))
    cfg = PeerSettings.model_validate(
        {"bind": "127.0.0.1", "port": 0, "idle_timeout_s": 2.0, **overrides}
    )
    server = PeerBlobServer(cas, auth, cfg, node_id=holder)
    await server.start()
    return Env(
        cas,
        key,
        holder,
        built,
        other,
        server,
        auth,
        f"http://127.0.0.1:{server.port}",
        clock,
    )


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e = await _make(tmp_path)
    try:
        yield e
    finally:
        await e.server.close()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=5.0)


def _digest_of(built: BuiltTree, name: str) -> Digest:
    entry = next(e for e in built.tree.files() if e.path == name)
    return entry.digest


async def _raw(port: int, data: bytes, *, read: bool = True) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        if not read:
            return b""
        return await asyncio.wait_for(reader.read(65536), 5.0)
    finally:
        writer.close()


# ------------------------------------------------------------------ happy


async def test_get_whole_small_and_big_blobs(env: Env) -> None:
    async with _client() as client:
        for name in ("small.txt", "big.bin", "empty.txt"):
            digest = _digest_of(env.built, name)
            r = await client.get(
                env.url(digest), headers={"authorization": f"Bearer {env.token()}"}
            )
            assert r.status_code == 200, name
            assert sha256_bytes(r.content) == digest
            assert r.headers["accept-ranges"] == "bytes"
        # The manifest itself (the token's tree).
        r = await client.get(
            env.url(env.built.tree.tree_id),
            headers={"authorization": f"Bearer {env.token()}"},
        )
        assert r.status_code == 200
        assert r.content == env.built.manifest


async def test_range_requests(env: Env) -> None:
    digest = _digest_of(env.built, "big.bin")
    data = (env.cas.store.path_for(digest)).read_bytes()
    size = len(data)
    auth = {"authorization": f"Bearer {env.token()}"}
    async with _client() as client:
        for header, (start, end) in {
            "bytes=0-9": (0, 9),
            f"bytes=10-{size - 1}": (10, size - 1),  # big: sendfile path
            "bytes=100-": (100, size - 1),
            "bytes=-5": (size - 5, size - 1),
            f"bytes=5-{size + 1000}": (5, size - 1),  # clipped to the end
        }.items():
            r = await client.get(env.url(digest), headers={**auth, "range": header})
            assert r.status_code == 206, header
            assert r.content == data[start : end + 1], header
            assert r.headers["content-range"] == f"bytes {start}-{end}/{size}"


async def test_head_has_length_and_no_body(env: Env) -> None:
    digest = _digest_of(env.built, "big.bin")
    async with _client() as client:
        r = await client.head(
            env.url(digest), headers={"authorization": f"Bearer {env.token()}"}
        )
    assert r.status_code == 200
    assert int(r.headers["content-length"]) == env.cas.store.size(digest)
    assert r.content == b""


async def test_keep_alive_serves_many_requests_on_one_connection(env: Env) -> None:
    digest = _digest_of(env.built, "small.txt")
    auth = {"authorization": f"Bearer {env.token()}"}
    async with _client() as client:
        for _ in range(5):
            assert (await client.get(env.url(digest), headers=auth)).status_code == 200
    assert env.server.stats.connections == 1
    assert env.server.stats.served == 5


# ----------------------------------------------------------------- tokens


async def test_token_refusals(env: Env) -> None:
    digest = _digest_of(env.built, "small.txt")
    cases = {
        "none": (None, 401),
        "basic": ("Basic abc", 401),
        "forged": (f"Bearer {env.token(key=Ed25519PrivateKey.generate())}", 403),
        "other_holder": (f"Bearer {env.token(holder=new_node_id())}", 403),
        "expired": (f"Bearer {env.token(now=NOW - 700)}", 403),
        "garbage": ("Bearer x.y", 403),
    }
    async with _client() as client:
        for name, (value, status) in cases.items():
            headers = {} if value is None else {"authorization": value}
            r = await client.get(env.url(digest), headers=headers)
            assert r.status_code == status, name
            assert r.content == b""
    assert env.server.stats.refused["missing"] == 2
    assert env.server.stats.refused["bad_signature"] == 1
    assert env.server.stats.refused["wrong_holder"] == 1
    assert env.server.stats.refused["expired"] == 1


async def test_token_for_tree_x_cannot_read_a_blob_only_in_tree_y(env: Env) -> None:
    secret = _digest_of(env.other, "secret.txt")
    async with _client() as client:
        r = await client.get(
            env.url(secret), headers={"authorization": f"Bearer {env.token()}"}
        )
        assert r.status_code == 403
        # ... nor the other tree's manifest.
        r = await client.get(
            env.url(env.other.tree.tree_id),
            headers={"authorization": f"Bearer {env.token()}"},
        )
        assert r.status_code == 403
        # The right token works.
        r = await client.get(
            env.url(secret),
            headers={
                "authorization": f"Bearer {env.token(tree=env.other.tree.tree_id)}"
            },
        )
        assert r.status_code == 200
    assert env.server.stats.refused["not_in_tree"] == 2


async def test_no_manifest_in_cas_is_404(env: Env) -> None:
    unknown_tree = sha256_bytes(b"not here")
    digest = _digest_of(env.built, "small.txt")
    async with _client() as client:
        r = await client.get(
            env.url(digest),
            headers={"authorization": f"Bearer {env.token(tree=unknown_tree)}"},
        )
    assert r.status_code == 404
    assert env.server.stats.refused["no_tree"] == 1


async def test_member_of_tree_but_absent_blob_is_404(env: Env) -> None:
    digest = _digest_of(env.built, "small.txt")
    env.cas.store.evict(digest)
    async with _client() as client:
        r = await client.get(
            env.url(digest), headers={"authorization": f"Bearer {env.token()}"}
        )
    assert r.status_code == 404


async def test_no_key_yet_is_503_and_new_key_replaces_old(env: Env) -> None:
    digest = _digest_of(env.built, "small.txt")
    env.auth.set_key(b"")
    async with _client() as client:
        r = await client.get(
            env.url(digest), headers={"authorization": f"Bearer {env.token()}"}
        )
        assert r.status_code == 503
        new_key = Ed25519PrivateKey.generate()
        env.auth.set_key(_raw_key(new_key))
        old = await client.get(
            env.url(digest), headers={"authorization": f"Bearer {env.token()}"}
        )
        assert old.status_code == 403  # the previous session's key no longer counts
        new = await client.get(
            env.url(digest),
            headers={"authorization": f"Bearer {env.token(key=new_key)}"},
        )
        assert new.status_code == 200


async def test_membership_parsed_once_per_tree(env: Env) -> None:
    digest = _digest_of(env.built, "small.txt")
    async with _client() as client:
        for _ in range(4):
            r = await client.get(
                env.url(digest), headers={"authorization": f"Bearer {env.token()}"}
            )
            assert r.status_code == 200
    assert env.auth.trees_parsed == 1


# ------------------------------------------------------------------- http


async def test_methods_paths_and_ranges(env: Env) -> None:
    digest = _digest_of(env.built, "big.bin")
    auth = {"authorization": f"Bearer {env.token()}"}
    size = env.cas.store.size(digest)
    assert size is not None
    async with _client() as client:
        assert (await client.post(env.url(digest), headers=auth)).status_code == 405
        assert (
            await client.put(env.url(digest), headers=auth, content=b"x")
        ).status_code in (400, 405)
        assert (await client.delete(env.url(digest), headers=auth)).status_code == 405
        assert (
            await client.get(f"{env.base}/v1/blobs/{digest.hex.upper()}", headers=auth)
        ).status_code == 404
        assert (
            await client.get(f"{env.base}/v1/blobs/{digest.hex}?x=1", headers=auth)
        ).status_code == 404
        assert (
            await client.get(f"{env.base}/v1/blobs/../../etc/passwd", headers=auth)
        ).status_code == 404
        assert (await client.get(f"{env.base}/", headers=auth)).status_code == 404
        for header, status in {
            "bytes=0-1,5-6": 400,  # two ranges
            "bytes=abc": 400,
            "items=0-1": 400,
            "bytes=-": 400,
            f"bytes={size}-": 416,
            "bytes=10-5": 416,
            "bytes=-0": 416,
        }.items():
            r = await client.get(env.url(digest), headers={**auth, "range": header})
            assert r.status_code == status, header
            if status == 416:
                assert r.headers["content-range"] == f"bytes */{size}"


async def test_request_with_a_body_is_refused(env: Env) -> None:
    digest = _digest_of(env.built, "small.txt")
    raw = (
        f"GET /v1/blobs/{digest.hex} HTTP/1.1\r\nhost: x\r\ncontent-length: 5\r\n"
        f"authorization: Bearer {env.token()}\r\n\r\nhello"
    ).encode()
    answer = await _raw(env.server.port, raw)
    assert answer.startswith(b"HTTP/1.1 400")


async def test_duplicate_authorization_header_is_400(env: Env) -> None:
    digest = _digest_of(env.built, "small.txt")
    tok = env.token()
    raw = (
        f"GET /v1/blobs/{digest.hex} HTTP/1.1\r\nhost: x\r\n"
        f"authorization: Bearer {tok}\r\nauthorization: Bearer {tok}\r\n\r\n"
    ).encode()
    assert (await _raw(env.server.port, raw)).startswith(b"HTTP/1.1 400")


async def test_header_bytes_max_accepted_and_max_plus_one_refused(
    tmp_path: Path,
) -> None:
    e = await _make(tmp_path, header_bytes=2048)
    try:
        digest = _digest_of(e.built, "small.txt")
        head = (
            f"GET /v1/blobs/{digest.hex} HTTP/1.1\r\nhost: x\r\n"
            f"authorization: Bearer {e.token()}\r\nx-pad: "
        ).encode()

        def request(total: int) -> bytes:
            # h11 counts the head up to and including the blank line.
            pad = total - len(head) - 4
            return head + b"a" * pad + b"\r\n\r\n"

        ok = await _raw(e.server.port, request(2048))
        assert ok.startswith(b"HTTP/1.1 200"), ok[:40]
        too_big = await _raw(e.server.port, request(2049))
        assert too_big.startswith(b"HTTP/1.1 431"), too_big[:40]
    finally:
        await e.server.close()


async def test_slowloris_is_cut_at_the_idle_deadline(tmp_path: Path) -> None:
    e = await _make(tmp_path, idle_timeout_s=0.2)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", e.server.port)
        started = time.monotonic()
        writer.write(b"GET /v1/blobs/")
        await writer.drain()
        # Keep trickling bytes: the deadline is for the whole head, not per read.
        closed = False
        for _ in range(40):
            await asyncio.sleep(0.02)
            try:
                writer.write(b"a")
                await writer.drain()
            except ConnectionError:
                closed = True
                break
            if reader.at_eof():
                closed = True
                break
        data = await asyncio.wait_for(reader.read(), 2.0)
        assert closed or data == b""
        assert time.monotonic() - started < 0.2 + 0.6
        assert e.server.stats.refused.get("idle_timeout") == 1
        writer.close()
    finally:
        await e.server.close()


async def test_connection_cap_answers_503(tmp_path: Path) -> None:
    e = await _make(tmp_path, max_connections=1)
    try:
        held_reader, held_writer = await asyncio.open_connection(
            "127.0.0.1", e.server.port
        )
        await asyncio.sleep(0.02)  # the server has accepted it
        answer = await _raw(e.server.port, b"GET / HTTP/1.1\r\nhost: x\r\n\r\n")
        assert answer.startswith(b"HTTP/1.1 503")
        assert e.server.stats.connections_rejected == 1
        held_writer.close()
        del held_reader
    finally:
        await e.server.close()


async def test_requests_per_connection_bound(tmp_path: Path) -> None:
    e = await _make(tmp_path, max_requests_per_conn=2)
    try:
        digest = _digest_of(e.built, "small.txt")
        auth = {"authorization": f"Bearer {e.token()}"}
        async with _client() as client:
            r1 = await client.get(e.url(digest), headers=auth)
            r2 = await client.get(e.url(digest), headers=auth)
            r3 = await client.get(e.url(digest), headers=auth)
        assert [r.status_code for r in (r1, r2, r3)] == [200, 200, 200]
        assert "connection" not in r1.headers
        assert r2.headers["connection"] == "close"
        assert e.server.stats.connections == 2
    finally:
        await e.server.close()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX symlinks")
async def test_a_symlink_in_the_cas_is_never_followed(env: Env, tmp_path: Path) -> None:
    digest = _digest_of(env.built, "small.txt")
    path = env.cas.store.path_for(digest)
    path.chmod(0o600)
    path.unlink()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"hello peer\n")
    path.symlink_to(outside)
    async with _client() as client:
        r = await client.get(
            env.url(digest), headers={"authorization": f"Bearer {env.token()}"}
        )
    assert r.status_code == 404


async def test_small_authorised_request_latency_on_loopback(env: Env) -> None:
    """D97: no ~40 ms delayed-ACK stall (not a benchmark claim; generous bound)."""
    digest = _digest_of(env.built, "small.txt")
    auth = {"authorization": f"Bearer {env.token()}"}
    async with _client() as client:
        await client.get(env.url(digest), headers=auth)  # connect + parse tree
        samples = []
        for _ in range(20):
            t0 = time.perf_counter()
            r = await client.get(env.url(digest), headers=auth)
            samples.append(time.perf_counter() - t0)
            assert r.status_code == 200
    samples.sort()
    assert samples[len(samples) // 2] < 0.030, samples


async def test_endpoint_advertised(env: Env) -> None:
    assert env.server.endpoint("127.0.0.1") == f"127.0.0.1:{env.server.port}"
    await env.server.close()
    assert env.server.endpoint("127.0.0.1") == ""


async def test_endpoint_advertises_a_forwarded_port(env: Env) -> None:
    """V0.7 G3: ``[agent.peer] advertise_port`` (a port forward / the test
    harness's recording proxy) replaces the bound port, not the host."""
    env.server._cfg = env.server._cfg.model_copy(update={"advertise_port": 40999})
    assert env.server.endpoint("127.0.0.1") == "127.0.0.1:40999"


async def test_start_peer_wires_hooks_and_survives_a_taken_port(tmp_path: Path) -> None:
    import socket
    from types import SimpleNamespace

    from meshforge.agent.runtime import start_peer
    from meshforge.agent.settings import AgentSettings
    from meshforge.common.proto.meshforge.v1 import agent_pb2

    cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 20))
    client = SimpleNamespace(
        node_id=new_node_id(),
        controller_target="127.0.0.1:47100",
        on_hello_ack=None,
        peer_endpoint=None,
    )

    def settings(port: int) -> AgentSettings:
        return AgentSettings(
            data_dir=tmp_path,
            # plain HTTP wiring test (TLS wiring: component test_v07_g3_peer_tls)
            security={"mode": "insecure-dev"},
            agent={
                "controller_address": "127.0.0.1:47100",
                "peer": {"bind": "127.0.0.1", "port": port},
            },
        )

    # Off unless bind is set.
    off = AgentSettings(data_dir=tmp_path, agent={"controller_address": "127.0.0.1:1"})
    assert await start_peer(off, cas, client) is None  # type: ignore[arg-type]

    server = await start_peer(settings(0), cas, client)  # type: ignore[arg-type]
    assert server is not None
    try:
        assert client.peer_endpoint() == f"127.0.0.1:{server.port}"
        key = Ed25519PrivateKey.generate()
        client.on_hello_ack(agent_pb2.HelloAck(peer_token_key=_raw_key(key)))
        assert server._auth.ready
        client.on_hello_ack(agent_pb2.HelloAck(peer_token_key=b""))
        assert not server._auth.ready
        # A taken port: logged, the agent runs without a peer server.
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            taken = busy.getsockname()[1]
            assert await start_peer(settings(taken), cas, client) is None  # type: ignore[arg-type]
    finally:
        await server.close()


async def test_peer_settings_bounds() -> None:
    from pydantic import ValidationError

    assert PeerSettings().bind is None
    assert PeerSettings().port == 47120
    with pytest.raises(ValidationError, match="wildcard"):
        PeerSettings(bind=WILDCARD)
    assert PeerSettings(bind=WILDCARD, allow_any_bind=True).bind == WILDCARD
    with pytest.raises(ValidationError, match="non-private"):
        PeerSettings(bind="8.8.8.8")
    with pytest.raises(ValidationError, match="IP literal"):
        PeerSettings(bind="myhost")
    assert PeerSettings(bind=" ").bind is None
    PeerSettings(header_bytes=1024)
    with pytest.raises(ValidationError):
        PeerSettings(header_bytes=1023)
