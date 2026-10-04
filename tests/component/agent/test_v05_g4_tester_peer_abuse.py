"""V0.5 G4 tester round 1: hostile clients against the real peer blob server.

Real sockets, raw bytes (no HTTP client that would sanitise the request).
Derived from the brief (D97/D98: GET/HEAD + ONE range, token checked per
request, only CAS files, bounded head/connections/time) - not from the code.

Defects found are kept as strict xfails named ``DEFECT-n`` (they flip to a
failure the day the defect is fixed, so the marker must then be removed).
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

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
from meshforge.common.peer_token import MAX_TTL_S, SKEW_S, sign_peer_token
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

NOW = 1_800_000_000.0
POSIX = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX only")


@dataclass
class Env:
    cas: AgentCas
    key: Ed25519PrivateKey
    holder: str
    built: BuiltTree
    other: BuiltTree
    server: PeerBlobServer
    auth: PeerAuthorizer
    clock: list[float]
    small: bytes
    big: bytes

    @property
    def port(self) -> int:
        return self.server.port

    def token(self, **kw: object) -> str:
        args: dict[str, object] = {
            "tree": self.built.tree.tree_id,
            "requester": new_node_id(),
            "holder": self.holder,
            "attempt_id": new_attempt_id(),
            "now": self.clock[0],
        }
        args.update(kw)
        key = args.pop("key", self.key)
        return sign_peer_token(key, **args)  # type: ignore[arg-type, call-arg]

    def digest(self, name: str) -> Digest:
        return next(e for e in self.built.tree.files() if e.path == name).digest

    def req(
        self,
        name: str | Digest | None = None,
        *,
        token: str | None = None,
        extra: str = "",
        method: str = "GET",
        path: str | None = None,
    ) -> bytes:
        digest = name if isinstance(name, Digest) else self.digest(name or "small.txt")
        target = path if path is not None else f"/v1/blobs/{digest.hex}"
        tok = self.token() if token is None else token
        return (
            f"{method} {target} HTTP/1.1\r\nhost: x\r\n"
            f"authorization: Bearer {tok}\r\n{extra}\r\n"
        ).encode("latin-1")


def _raw_key(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


async def _make(tmp_path: Path, **overrides: object) -> Env:
    cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 30))
    cas.store.prepare()
    proj = tmp_path / "proj"
    proj.mkdir()
    small = b"hello peer\n"
    big = os.urandom(COALESCE_BYTES * 2 + 123)
    (proj / "small.txt").write_bytes(small)
    (proj / "big.bin").write_bytes(big)
    (proj / "empty.txt").write_bytes(b"")
    built = build_tree(proj, chunk_size=MIN_CHUNK_SIZE)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    (other_dir / "secret.txt").write_bytes(b"only in tree Y\n")
    other = build_tree(other_dir, chunk_size=MIN_CHUNK_SIZE)
    for b in (built, other):
        for hex_, path in b.sources.items():
            cas.store.put_file(path, Digest(hex_))
        cas.store.put_bytes(b.manifest, b.tree.tree_id)
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
    return Env(cas, key, holder, built, other, server, auth, clock, small, big)


@pytest_asyncio.fixture
async def env(tmp_path: Path) -> AsyncIterator[Env]:
    e = await _make(tmp_path)
    try:
        yield e
    finally:
        await e.server.close()


async def _exchange(port: int, data: bytes, *, wait: float = 5.0) -> bytes:
    """Send ``data``, read until the server closes or ``wait`` seconds pass."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(data)
        await writer.drain()
        out = b""
        end = time.monotonic() + wait
        while time.monotonic() < end:
            try:
                piece = await asyncio.wait_for(reader.read(1 << 20), 0.3)
            except TimeoutError:
                if out:
                    break
                continue
            except ConnectionError:
                break  # the server closed with our bytes unread: RST
            if not piece:
                break
            out += piece
        return out
    finally:
        writer.close()


def _responses(raw: bytes) -> list[tuple[int, dict[str, str], bytes]]:
    """Parse back-to-back HTTP/1.1 responses with content-length bodies."""
    out: list[tuple[int, dict[str, str], bytes]] = []
    while raw:
        head, sep, rest = raw.partition(b"\r\n\r\n")
        if not sep:
            break
        lines = head.split(b"\r\n")
        status = int(lines[0].split()[1])
        headers = {
            k.strip().lower().decode(): v.strip().decode()
            for k, _, v in (line.partition(b":") for line in lines[1:])
        }
        n = int(headers.get("content-length", "0"))
        out.append((status, headers, rest[:n]))
        raw = rest[n:]
    return out


# ------------------------------------------------------------------ ranges

RANGE_CASES = [
    ("bytes=0-1,5-6", 400),  # multi-range
    ("bytes=0-1, 5-6", 400),
    ("bytes=0-1,", 400),
    ("bytes=-", 400),
    ("bytes=", 400),
    ("bytes=--5", 400),
    ("bytes=+1-2", 400),
    ("bytes=1_0-2", 400),
    ("bytes=0x1-5", 400),
    ("bytes=-5-", 400),
    ("items=0-1", 400),
    ("bytes=\xd9\xa1-\xd9\xa2", 400),  # UTF-8 Arabic-Indic digits on the wire
    ("bytes=99999999999999999999999999-", 400),  # absurd length
    ("bytes=18446744073709551616-", 400),  # 20 digits: > 2**64
    ("bytes=5-2", 416),  # end before start
    ("bytes=-0", 416),
    ("bytes=11-", 416),  # == size of small.txt
    ("bytes=9999999999999999999-", 416),  # 19 digits, beyond the blob
    ("bytes=0-9999999999999999999", 206),  # huge end is clamped
    ("bytes=-9999999999999999999", 206),  # huge suffix = whole blob
    ("bytes=0-0", 206),
    ("bytes=10-10", 206),
    ("bytes=10-", 206),
    ("bytes=-1", 206),
]


@pytest.mark.parametrize(("value", "status"), RANGE_CASES)
async def test_range_abuse_on_small_blob(env: Env, value: str, status: int) -> None:
    raw = await _exchange(env.port, env.req(extra=f"range: {value}\r\n"))
    (got, headers, body), *rest = _responses(raw)
    assert not rest
    assert got == status, (value, raw[:200])
    if status == 206:
        first, _, last = (
            headers["content-range"].partition("/")[0].partition(" ")[2].partition("-")
        )
        assert headers["content-range"].endswith("/11")
        assert body == env.small[int(first) : int(last) + 1]
        assert len(body) == int(headers["content-length"])
    else:
        assert body == b""


async def test_range_on_empty_blob_is_416_and_whole_get_is_200(env: Env) -> None:
    ok = _responses(await _exchange(env.port, env.req("empty.txt")))[0]
    assert ok[0] == 200 and ok[2] == b""
    for value in ("bytes=0-0", "bytes=0-", "bytes=-1"):
        raw = await _exchange(
            env.port, env.req("empty.txt", extra=f"range: {value}\r\n")
        )
        assert _responses(raw)[0][0] == 416, value


async def test_two_range_headers_are_refused(env: Env) -> None:
    raw = await _exchange(
        env.port, env.req(extra="range: bytes=0-1\r\nrange: bytes=2-3\r\n")
    )
    assert _responses(raw)[0][0] == 400


async def test_range_on_big_blob_matches_every_byte(env: Env) -> None:
    digest = env.digest("big.bin")
    size = len(env.big)
    edges = [
        (0, 0),
        (0, 1),
        (size - 1, size - 1),
        (COALESCE_BYTES - 1, COALESCE_BYTES),
        (COALESCE_BYTES, COALESCE_BYTES * 2),
        (5, size - 1),
    ]
    for a, b in edges:
        raw = await _exchange(
            env.port, env.req(digest, extra=f"range: bytes={a}-{b}\r\n")
        )
        status, headers, body = _responses(raw)[0]
        assert status == 206, (a, b)
        assert body == env.big[a : b + 1], (a, b)
        assert headers["content-range"] == f"bytes {a}-{b}/{size}"


# --------------------------------------------------------------- pipelining


async def test_pipelined_requests_answered_in_order(env: Env) -> None:
    forged = env.token(key=Ed25519PrivateKey.generate())
    data = (
        env.req("small.txt")
        + env.req("small.txt", token=forged)
        + env.req("big.bin")
        + env.req("small.txt", extra="range: bytes=0-4\r\n")
    )
    got = _responses(await _exchange(env.port, data))
    assert [g[0] for g in got] == [200, 403, 200, 206]
    assert got[0][2] == env.small
    assert got[2][2] == env.big
    assert got[3][2] == env.small[:5]


async def test_pipelined_burst_of_100_is_bounded_and_correct(tmp_path: Path) -> None:
    e = await _make(tmp_path, max_requests_per_conn=10)
    try:
        data = b"".join(e.req("small.txt") for _ in range(100))
        got = _responses(await _exchange(e.port, data))
        # The per-connection bound closes the connection after 10 answers.
        assert 1 <= len(got) <= 10
        assert all(g[0] == 200 and g[2] == e.small for g in got)
    finally:
        await e.server.close()


async def test_request_smuggling_attempts_do_not_serve_two_things(env: Env) -> None:
    forged = env.token(key=Ed25519PrivateKey.generate())
    # Content-Length + body that looks like a second request: refused as a body.
    inner = env.req("big.bin").decode("latin-1")
    smuggled = (
        f"GET /v1/blobs/{env.digest('small.txt').hex} HTTP/1.1\r\nhost: x\r\n"
        f"authorization: Bearer {forged}\r\ncontent-length: {len(inner)}\r\n\r\n{inner}"
    ).encode("latin-1")
    got = _responses(await _exchange(env.port, smuggled))
    assert [g[0] for g in got][:1] == [400]
    assert env.server.stats.served == 0
    # Transfer-Encoding: chunked on a GET.
    te = env.req(extra="transfer-encoding: chunked\r\n") + b"5\r\nhello\r\n0\r\n\r\n"
    got = _responses(await _exchange(env.port, te))
    assert got and got[0][0] == 400
    assert env.server.stats.served == 0


# ---------------------------------------------------------------- path abuse

PATHS = [
    "/",
    "/v1/blobs/",
    "/v1/blobs",
    "/v1/blobs/..",
    "/v1/blobs/../../etc/passwd",
    "/v1/blobs/%2e%2e/%2e%2e/etc/passwd",
    "/v1/blobs/{hex}/",
    "/v1/blobs/{hex}/../{hex}",
    "/v1/blobs/{hex}?x=1",
    "/v1/blobs/{hex}#frag",
    "/v1/blobs//{hex}",
    "//v1/blobs/{hex}",
    "/v1/blobs/{HEX}",
    "/v1/blobs/{hex63}",
    "/v1/blobs/{hex65}",
    "/v1/blobs/sha256:{hex}",
    "/v1/blobs/{hex}%00",
    "/v1/blobs/%7bhex%7d",
    "http://evil.example/v1/blobs/{hex}",
    "/v1/blobs/{hex}\t",
    "/V1/blobs/{hex}",
    "/v1/blobs/é{hex}",
    "/v1/blobs/{dothex}",
]


@pytest.mark.parametrize("template", PATHS)
async def test_path_abuse_is_never_200(env: Env, template: str) -> None:
    hex_ = env.digest("small.txt").hex
    target = (
        template.replace("{hex}", hex_)
        .replace("{HEX}", hex_.upper())
        .replace("{hex63}", hex_[:-1])
        .replace("{hex65}", hex_ + "a")
        .replace("{dothex}", "." + hex_[1:])
    )
    raw = await _exchange(env.port, env.req(path=target), wait=2.0)
    for status, _, body in _responses(raw):
        assert status in (400, 404), (target, status)
        assert body == b""
    assert env.server.stats.served == 0


@pytest.mark.parametrize(
    "method",
    ["POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "CONNECT", "get", "BREW"],
)
async def test_other_methods_are_refused(env: Env, method: str) -> None:
    raw = await _exchange(env.port, env.req(method=method), wait=2.0)
    got = _responses(raw)
    assert got and got[0][0] in (400, 405, 501), (method, raw[:100])
    assert env.server.stats.served == 0


async def test_garbage_and_binary_input_does_not_kill_the_server(env: Env) -> None:
    for blob in (
        b"\x00" * 100,
        os.urandom(4096),
        b"GET\r\n\r\n",
        b"\r\n" * 1000,
        b"GET /v1/blobs/x HTTP/9.9\r\n\r\n",
        b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc",
    ):
        await _exchange(env.port, blob, wait=1.0)
    ok = _responses(await _exchange(env.port, env.req("small.txt")))
    assert ok[0][0] == 200 and ok[0][2] == env.small


# ---------------------------------------------- planted CAS entries (local)


@POSIX
async def test_symlinked_blob_and_symlinked_directory_are_not_followed(
    env: Env, tmp_path: Path
) -> None:
    digest = env.digest("small.txt")
    path = env.cas.store.path_for(digest)
    outside = tmp_path / "outside"
    outside.write_bytes(env.small)
    path.chmod(0o600)
    path.unlink()
    path.symlink_to(outside)
    got = _responses(await _exchange(env.port, env.req("small.txt")))
    assert got[0][0] == 404
    # A symlinked *fan-out directory* of the CAS pointing outside it.
    big = env.digest("big.bin")
    shard = env.cas.store.path_for(big).parent
    moved = tmp_path / "moved-shard"
    shard.rename(moved)
    shard.symlink_to(moved)
    got = _responses(await _exchange(env.port, env.req(big)))
    # Either refuses (404) or serves the right bytes; it must never serve other data.
    assert got[0][0] in (200, 404)
    if got[0][0] == 200:
        assert sha256_bytes(got[0][2]) == big


@POSIX
async def test_a_directory_at_a_blob_path_is_404(env: Env) -> None:
    digest = env.digest("small.txt")
    path = env.cas.store.path_for(digest)
    path.chmod(0o600)
    path.unlink()
    path.mkdir()
    got = _responses(await _exchange(env.port, env.req("small.txt")))
    assert got[0][0] == 404


@POSIX
async def test_a_fifo_at_a_blob_path_does_not_stall_the_server(env: Env) -> None:
    """A FIFO named like a blob blocks ``os.open(O_RDONLY)`` until a writer shows
    up. The server opens on the loop, so every other client waits too. (Threads
    drive the clients and release the FIFO after 3 s so the test cannot hang.)"""
    import threading

    digest = env.digest("small.txt")
    path = env.cas.store.path_for(digest)
    path.chmod(0o600)
    path.unlink()
    os.mkfifo(path)
    other = env.req("big.bin")
    fifo_req = env.req("small.txt")
    result: dict[str, float] = {}

    def client(data: bytes, delay: float, key: str) -> None:
        time.sleep(delay)
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", env.port), timeout=10) as sock:
            sock.sendall(data)
            sock.recv(1)
        result[key] = time.monotonic() - started

    def release() -> None:
        time.sleep(3.0)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:  # ENXIO: no reader is blocked on it (the fixed server)
            return
        os.close(fd)

    threads = [
        threading.Thread(target=client, args=(fifo_req, 0.0, "fifo")),
        threading.Thread(target=client, args=(other, 0.3, "other")),
        threading.Thread(target=release),
    ]
    for t in threads:
        t.start()
    await asyncio.sleep(0)  # (the loop may be blocked right here)
    await asyncio.to_thread(lambda: [t.join() for t in threads])
    assert result["other"] < 1.5, f"an innocent client waited {result['other']:.1f}s"


async def test_blob_replaced_by_shorter_content_mid_life_never_serves_wrong_digest_silently(
    env: Env,
) -> None:
    """The server may serve what is on disk (the fetcher verifies), but a
    Range/length answer must be self-consistent with the file it read."""
    digest = env.digest("big.bin")
    path = env.cas.store.path_for(digest)
    path.chmod(0o600)
    path.write_bytes(env.big[:1000])  # truncated behind the CAS's back
    raw = await _exchange(env.port, env.req(digest, extra="range: bytes=500-999\r\n"))
    got = _responses(raw)
    assert got and got[0][0] == 206 and got[0][2] == env.big[500:1000]
    raw = await _exchange(env.port, env.req(digest, extra="range: bytes=2000-2100\r\n"))
    assert _responses(raw)[0][0] == 416


# --------------------------------------------------------------- token abuse


async def test_token_bearer_scheme_variants(env: Env) -> None:
    tok = env.token()
    digest = env.digest("small.txt").hex

    async def status(auth: str | None, target: str | None = None) -> int:
        head = f"GET {target or '/v1/blobs/' + digest} HTTP/1.1\r\nhost: x\r\n"
        if auth is not None:
            head += f"authorization: {auth}\r\n"
        got = _responses(await _exchange(env.port, (head + "\r\n").encode("latin-1")))
        return got[0][0]

    assert await status(f"Bearer {tok}") == 200
    assert await status(f"bearer {tok}") == 200
    assert await status(f"BEARER {tok}") == 200
    assert await status(None) == 401
    assert await status("Bearer") == 401
    assert await status("Bearer ") == 401
    assert await status(f"Basic {tok}") == 401
    assert await status(tok) == 401
    assert await status(f"Bearer {tok}.extra") == 403
    assert await status(f"Bearer {tok[:-2]}") == 403
    assert await status(f"Bearer {tok.replace('.', '..', 1)}") == 403
    # A token in the query string is not a credential.
    assert await status(None, f"/v1/blobs/{digest}?token={tok}") in (401, 404)


async def test_oversized_token_header_is_431_not_a_hang(env: Env) -> None:
    giant = "A" * (16 * 1024 + 10)
    got = _responses(await _exchange(env.port, env.req(token=giant)))
    assert got[0][0] == 431
    got = _responses(await _exchange(env.port, env.req(token="A" * 600)))
    assert got[0][0] == 403  # over MAX_TOKEN_CHARS but under header_bytes


async def test_token_expiry_edges_through_the_server(env: Env) -> None:
    tok = env.token(ttl_s=60)
    exp = int(NOW) + 60

    async def status() -> int:
        return _responses(await _exchange(env.port, env.req(token=tok)))[0][0]

    env.clock[0] = exp
    assert await status() == 200
    env.clock[0] = exp + SKEW_S
    assert await status() == 200
    env.clock[0] = exp + SKEW_S + 1
    assert await status() == 403
    env.clock[0] = NOW - 5
    assert await status() == 200  # issued "in the future" within skew
    env.clock[0] = NOW - MAX_TTL_S - SKEW_S - 120
    assert await status() == 403  # exp absurdly far ahead of this clock


async def test_token_expires_on_a_live_keepalive_connection(env: Env) -> None:
    """D6: authorisation is per request, so a long transfer that outlives the
    token is refused on its next request (the fetcher then drops the peer)."""
    tok = env.token(ttl_s=60)
    reader, writer = await asyncio.open_connection("127.0.0.1", env.port)
    try:
        for expected in (200, 403):
            writer.write(env.req(token=tok))
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            status = int(head.split()[1])
            n = int(
                next(
                    line.split(b":")[1]
                    for line in head.split(b"\r\n")
                    if line.lower().startswith(b"content-length")
                )
            )
            await reader.readexactly(n)
            assert status == expected
            env.clock[0] = NOW + 60 + SKEW_S + 1  # the clock jumps past expiry
    finally:
        writer.close()


async def test_key_change_old_key_token_refused_and_bad_key_refuses_all(
    env: Env,
) -> None:
    old = env.token()
    new_key = Ed25519PrivateKey.generate()
    assert env.auth.set_key(_raw_key(new_key))
    assert _responses(await _exchange(env.port, env.req(token=old)))[0][0] == 403
    fresh = env.token(key=new_key)
    assert _responses(await _exchange(env.port, env.req(token=fresh)))[0][0] == 200
    # A malformed key from a later Hello: fail closed, not "keep serving the old key".
    assert env.auth.set_key(b"short") is False
    assert _responses(await _exchange(env.port, env.req(token=fresh)))[0][0] == 503
    # The old controller key never comes back by itself.
    assert _responses(await _exchange(env.port, env.req(token=old)))[0][0] == 503


async def test_token_for_another_tree_cannot_read_even_the_other_trees_manifest(
    env: Env,
) -> None:
    y_token = env.token(tree=env.other.tree.tree_id)
    x_manifest = env.built.tree.tree_id
    got = _responses(await _exchange(env.port, env.req(x_manifest, token=y_token)))
    assert got[0][0] == 403
    secret = next(iter(env.other.tree.files())).digest
    x_token = env.token()
    assert (
        _responses(await _exchange(env.port, env.req(secret, token=x_token)))[0][0]
        == 403
    )
    assert (
        _responses(await _exchange(env.port, env.req(secret, token=y_token)))[0][0]
        == 200
    )


async def test_token_for_unknown_tree_is_404_and_does_not_poison_a_later_staging(
    env: Env, tmp_path: Path
) -> None:
    ghost = Digest("a" * 64)
    ghost_token = env.token(tree=ghost)
    got = _responses(await _exchange(env.port, env.req("small.txt", token=ghost_token)))
    assert got[0][0] == 404
    # The manifest arrives later (the holder finished staging): same token works.
    third = tmp_path / "third"
    third.mkdir()
    (third / "z.txt").write_bytes(b"zzz")
    b = build_tree(third, chunk_size=MIN_CHUNK_SIZE)
    for hex_, path in b.sources.items():
        env.cas.store.put_file(path, Digest(hex_))
    tok = env.token(tree=b.tree.tree_id)
    z = next(iter(b.tree.files())).digest
    assert _responses(await _exchange(env.port, env.req(z, token=tok)))[0][0] == 404
    env.cas.store.put_bytes(b.manifest, b.tree.tree_id)
    assert _responses(await _exchange(env.port, env.req(z, token=tok)))[0][0] == 200


# ---------------------------------------------------- connection exhaustion


def _rst(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


async def test_connection_flood_is_capped_and_slots_come_back(tmp_path: Path) -> None:
    e = await _make(tmp_path, max_connections=8, idle_timeout_s=0.5)
    try:
        socks = []
        for _ in range(200):
            s = socket.socket()
            s.setblocking(False)
            s.connect_ex(("127.0.0.1", e.port))
            socks.append(s)
        await asyncio.sleep(0.3)
        assert (
            e.server.stats.connections_rejected >= 50
        )  # the rest wait in the 100-deep kernel backlog
        # While flooded, an extra client is told 503, not hung.
        probe = await _exchange(e.port, e.req("small.txt"), wait=1.0)
        assert probe == b"" or _responses(probe)[0][0] in (503,)
        for s in socks[:100]:
            _rst(s)  # abrupt resets
        # The idle holders are cut by the idle timeout; slots must all return.
        await asyncio.sleep(1.5)
        for s in socks[100:]:
            s.close()
        await asyncio.sleep(0.2)
        for _ in range(8):  # all eight slots usable again => none leaked
            ok = _responses(await _exchange(e.port, e.req("small.txt")))
            assert ok[0][0] == 200
        held = []
        for _ in range(8):
            h = socket.socket()
            h.setblocking(False)
            h.connect_ex(("127.0.0.1", e.port))
            held.append(h)
        await asyncio.sleep(0.2)
        try:
            over = await _exchange(e.port, e.req("small.txt"), wait=1.0)
            assert _responses(over)[0][0] == 503  # exactly 8 slots, not fewer
        finally:
            for s in held:
                s.close()
    finally:
        await e.server.close()


async def test_slowloris_connections_do_not_leak_slots(tmp_path: Path) -> None:
    e = await _make(tmp_path, max_connections=4, idle_timeout_s=0.3)
    try:
        crawlers = []
        for _ in range(4):
            r, w = await asyncio.open_connection("127.0.0.1", e.port)
            w.write(b"GET /v1/blobs/")
            await w.drain()
            crawlers.append((r, w))
        await asyncio.sleep(0.1)
        assert (
            _responses(await _exchange(e.port, e.req("small.txt"), wait=1.0))[0][0]
            == 503
        )
        await asyncio.sleep(0.5)  # deadline passed: all four cut
        ok = _responses(await _exchange(e.port, e.req("small.txt")))
        assert ok[0][0] == 200
        for _r, w in crawlers:
            w.close()
    finally:
        await e.server.close()


async def test_client_that_never_reads_a_big_body_is_cut_and_frees_the_slot(
    tmp_path: Path,
) -> None:
    e = await _make(
        tmp_path, max_connections=1, idle_timeout_s=0.3, min_send_rate_bytes_s=1 << 30
    )
    try:
        # Make the blob big enough that the kernel buffers cannot swallow it.
        digest = e.digest("big.bin")
        path = e.cas.store.path_for(digest)
        path.chmod(0o600)
        path.write_bytes(os.urandom(48 * 1024 * 1024))
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.connect(("127.0.0.1", e.port))
        sock.sendall(e.req(digest))
        started = time.monotonic()
        deadline = started + 15
        status = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            raw = await _exchange(e.port, e.req("small.txt"), wait=1.0)
            got = _responses(raw)
            status = got[0][0] if got else 0
            if status == 200:
                break
        sock.close()
        assert status == 200, "the stalled reader kept the only slot"
    finally:
        await e.server.close()


async def test_peer_server_is_read_only(env: Env) -> None:
    before = sorted(str(p) for p in env.cas.store.root.rglob("*"))
    for _n in range(20):
        await _exchange(env.port, env.req("small.txt", method="PUT") + b"x", wait=0.5)
    for _ in range(5):
        await _exchange(env.port, env.req("small.txt"), wait=0.5)
    after = sorted(str(p) for p in env.cas.store.root.rglob("*"))
    assert before == after
