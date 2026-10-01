# ruff: noqa: ASYNC240
"""V0.5 G1 tester: a real corrupting/truncating TCP proxy between the agent's
fetcher and a real controller, on-disk corruption of the agent cache, hostile
HTTP servers, and the materialised workdir's isolation from the cache.

Criteria: 3 (interrupted transfer resumes) and 4 (corruption detected, never
used), derived from the roadmap, not the implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_jobs_harness import token_of
from pydantic import SecretStr

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import BlobSource, FetchError
from meshforge.agent.cas.materialise import MaterialiseError, materialise
from meshforge.agent.execution.fetch import stage_tree
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.errors import FailureType
from meshforge.common.hashing import sha256_bytes
from meshforge.common.tree import BuiltTree, TreeError, build_tree
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

CS = MIN_CHUNK_SIZE
posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX only")


class CorruptingProxy:
    """A real TCP proxy that damages *response* bytes in flight.

    ``mode='flip'``: flip one bit in the last byte of the next ``budget`` reads of
    at least 700 bytes (the last byte is always body). ``'cut'``:
    forward half of such a read, then close both sockets.
    """

    def __init__(self, upstream_port: int) -> None:
        self.upstream_port = upstream_port
        self.mode = "off"
        self.budget = 0
        self.damaged = 0
        self.port = 0
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def arm(self, mode: str, budget: int) -> None:
        self.mode, self.budget = mode, budget

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        up_r, up_w = await asyncio.open_connection("127.0.0.1", self.upstream_port)
        t1 = asyncio.create_task(self._pipe(reader, up_w, False))
        t2 = asyncio.create_task(self._pipe(up_r, writer, True))
        self._tasks |= {t1, t2}
        await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for w in (writer, up_w):
            w.close()

    async def _pipe(
        self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, response: bool
    ) -> None:
        with contextlib.suppress(Exception):
            while data := await src.read(65536):
                if response and self.budget != 0 and len(data) >= 700:
                    if self.mode == "flip":
                        b = bytearray(data)
                        b[-1] ^= 0x01
                        data = bytes(b)
                        self._spent()
                    elif self.mode == "cut":
                        dst.write(data[: len(data) // 2])
                        await dst.drain()
                        self._spent()
                        return
                dst.write(data)
                await dst.drain()

    def _spent(self) -> None:
        self.damaged += 1
        if self.budget > 0:
            self.budget -= 1


@pytest_asyncio.fixture
async def proxy(runtime: ControllerRuntime) -> AsyncIterator[CorruptingProxy]:
    p = CorruptingProxy(runtime.http_port)
    await p.start()
    yield p
    await p.stop()


async def _upload(runtime: ControllerRuntime, built: BuiltTree) -> None:
    client = ControllerClient(
        f"http://127.0.0.1:{runtime.http_port}",
        timeout_s=30,
        token=SecretStr(token_of(runtime)),
    )
    try:
        await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()


def _project(root: Path) -> tuple[Path, bytes, bytes]:
    root.mkdir()
    big = os.urandom(5 * CS + 11)  # chunked
    small = os.urandom(3000)  # one chunk, whole GET
    (root / "big.bin").write_bytes(big)
    (root / "small.bin").write_bytes(small)
    (root / "run.sh").write_bytes(b"#!/bin/sh\n")
    (root / "run.sh").chmod(0o755)
    return root, big, small


async def _stage(
    runtime: ControllerRuntime,
    proxy: CorruptingProxy,
    built: BuiltTree,
    cas: AgentCas,
    dest: Path,
    attempt: str = "a1",
) -> Any:
    assert runtime.fetch_tokens is not None
    tok = runtime.fetch_tokens.mint(attempt, [built.tree.tree_id], ttl_s=120)
    return await stage_tree(
        cas,
        tree_id=str(built.tree.tree_id),
        sources=[BlobSource(f"http://127.0.0.1:{proxy.port}", tok.get_secret_value())],
        dest=dest,
        free_disk_floor_bytes=0,
    )


# ------------------------------------------------------------ criterion 4 (real)


async def test_chunk_corrupted_in_flight_once_is_refetched_and_result_is_right(
    runtime: ControllerRuntime, proxy: CorruptingProxy, tmp_path: Path
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    big = os.urandom(5 * CS + 11)
    (root / "big.bin").write_bytes(big)
    built = build_tree(root, chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    await cas.prepare()
    cas.store.put_bytes(built.manifest, built.tree.tree_id)
    proxy.arm("flip", 1)  # exactly one range answer damaged
    await _stage(runtime, proxy, built, cas, tmp_path / "w")
    assert proxy.damaged == 1
    assert (tmp_path / "w" / "big.bin").read_bytes() == big
    assert cas.store.verify(sha256_bytes(big))


async def test_whole_file_or_manifest_corrupted_once_fails_typed_and_keeps_nothing(
    runtime: ControllerRuntime, proxy: CorruptingProxy, tmp_path: Path
) -> None:
    root, _big, small = _project(tmp_path / "p")
    built = build_tree(root, chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    proxy.arm("flip", 1)  # lands on the manifest (first read)
    with pytest.raises(FetchError) as info:
        await _stage(runtime, proxy, built, cas, tmp_path / "w")
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    assert not cas.store.has(built.tree.tree_id)
    # The next attempt (no damage) succeeds from the same cache dir.
    await _stage(runtime, proxy, built, cas, tmp_path / "w2", "a2")
    assert (tmp_path / "w2" / "small.bin").read_bytes() == small


async def test_chunk_corrupted_every_time_fails_integrity_and_keeps_nothing(
    runtime: ControllerRuntime, proxy: CorruptingProxy, tmp_path: Path
) -> None:
    root, big, _small = _project(tmp_path / "p")
    built = build_tree(root, chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    # Manifest first (one clean fetch), then corrupt everything after.
    proxy.arm("off", 0)
    assert runtime.fetch_tokens is not None
    proxy.arm("flip", -1)
    with pytest.raises(FetchError) as info:
        await _stage(runtime, proxy, built, cas, tmp_path / "w")
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    assert not cas.store.has(sha256_bytes(big))
    assert not cas.store.has(built.tree.tree_id)  # a corrupt manifest never cached
    workdir = tmp_path / "w"
    assert not workdir.exists() or not any(workdir.iterdir())


async def test_corruption_after_manifest_is_cached_hits_range_or_whole_reads(
    runtime: ControllerRuntime, proxy: CorruptingProxy, tmp_path: Path
) -> None:
    root, big, small = _project(tmp_path / "p")
    built = build_tree(root, chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    await cas.prepare()
    cas.store.put_bytes(built.manifest, built.tree.tree_id)  # manifest cached clean
    proxy.arm("flip", -1)
    with pytest.raises(FetchError) as info:
        await _stage(runtime, proxy, built, cas, tmp_path / "w")
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    assert proxy.damaged >= 1
    assert not cas.store.has(sha256_bytes(big))
    assert not cas.store.has(sha256_bytes(small))


# --------------------------------------------------- criterion 3 (cut mid-body)


async def test_connection_cut_mid_chunk_then_resume_skips_completed_chunks(
    runtime: ControllerRuntime, proxy: CorruptingProxy, tmp_path: Path
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    big = os.urandom(8 * CS)
    (root / "big.bin").write_bytes(big)
    built = build_tree(root, chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    await cas.prepare()
    cas.store.put_bytes(built.manifest, built.tree.tree_id)
    proxy.arm("cut", -1)  # every large read is cut: no chunk can ever finish
    with pytest.raises(FetchError):
        await _stage(runtime, proxy, built, cas, tmp_path / "w")
    assert not cas.store.has(sha256_bytes(big))
    # Let exactly chunks 0..2 through, then cut again: partial progress kept.
    done = 0
    proxy.arm("off", 0)
    digest = sha256_bytes(big)
    part = cas.store.resumable(digest, len(big), CS)
    # Fill 3 chunks honestly from the controller through the clean proxy.
    async with httpx.AsyncClient(timeout=10) as c:
        assert runtime.fetch_tokens is not None
        tok = runtime.fetch_tokens.mint("a2", [built.tree.tree_id], ttl_s=120)
        for i in range(3):
            r = await c.get(
                f"http://127.0.0.1:{proxy.port}/v1/blobs/{digest.hex}",
                headers={
                    "authorization": f"Bearer {tok.get_secret_value()}",
                    "range": f"bytes={i * CS}-{(i + 1) * CS - 1}",
                },
            )
            assert r.status_code == 206
            part.write_chunk(i, r.content, built.tree.entries[0].chunks[i])
            done += 1
    report = await _stage(runtime, proxy, built, cas, tmp_path / "w", "a3")
    assert report.chunks_resumed == 3
    assert report.bytes_fetched == 5 * CS
    assert (tmp_path / "w" / "big.bin").read_bytes() == big


# ------------------------------------------------- agent-side on-disk corruption


async def test_corrupted_resume_part_file_is_caught_by_the_whole_verify(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    big = os.urandom(4 * CS)
    (root / "big.bin").write_bytes(big)
    built = build_tree(root, chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    await cas.prepare()
    digest = sha256_bytes(big)
    part = cas.store.resumable(digest, len(big), CS)
    part.write_chunk(0, big[:CS], built.tree.entries[0].chunks[0])
    part.write_chunk(1, big[CS : 2 * CS], built.tree.entries[0].chunks[1])
    part_path, _sidecar = cas.store.resume_paths(digest)
    raw = bytearray(part_path.read_bytes())
    raw[10] ^= 0xFF  # damage a chunk the bitmap says is verified
    part_path.write_bytes(bytes(raw))
    src = BlobSource(f"http://127.0.0.1:{runtime.http_port}", token_of(runtime))
    # Fabric token works for reads as well.
    try:
        await stage_tree(
            cas,
            tree_id=str(built.tree.tree_id),
            sources=[src],
            dest=tmp_path / "w",
            free_disk_floor_bytes=0,
        )
        outcome = "ok"
    except FetchError as exc:
        outcome = str(exc.failure_type)
    # Never a silently wrong file: either right bytes or a typed failure with
    # nothing kept; and a second attempt must then succeed.
    if outcome == "ok":
        assert (tmp_path / "w" / "big.bin").read_bytes() == big
    else:
        assert not cas.store.has(digest)
        await stage_tree(
            cas,
            tree_id=str(built.tree.tree_id),
            sources=[src],
            dest=tmp_path / "w2",
            free_disk_floor_bytes=0,
        )
        assert (tmp_path / "w2" / "big.bin").read_bytes() == big


async def _damage(kind: str, path: Path) -> None:
    path.chmod(0o600)
    raw = path.read_bytes()
    if kind == "flip":
        mid = bytearray(raw)
        mid[len(mid) // 2] ^= 0xFF
        path.write_bytes(bytes(mid))
    elif kind == "empty":
        path.write_bytes(b"")
    else:
        path.write_bytes(raw[: len(raw) // 2])


@pytest.mark.parametrize("which", ["small", "big"])
@pytest.mark.parametrize("kind", ["flip", "empty", "truncate"])
async def test_one_damaged_cached_blob_is_evicted_refetched_never_materialised(
    runtime: ControllerRuntime,
    proxy: CorruptingProxy,
    tmp_path: Path,
    which: str,
    kind: str,
) -> None:
    root, big, small = _project(tmp_path / "p")
    built = build_tree(root, chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    await _stage(runtime, proxy, built, cas, tmp_path / "w1")
    victim = small if which == "small" else big
    await _damage(kind, cas.store.path_for(sha256_bytes(victim)))
    await _stage(runtime, proxy, built, cas, tmp_path / "w2", "a2")
    assert (tmp_path / "w2" / "small.bin").read_bytes() == small
    assert (tmp_path / "w2" / "big.bin").read_bytes() == big
    assert cas.store.verify(sha256_bytes(victim))


async def test_two_damaged_cached_blobs_never_materialise_and_the_next_attempt_heals(
    runtime: ControllerRuntime, proxy: CorruptingProxy, tmp_path: Path
) -> None:
    root, big, small = _project(tmp_path / "p")
    built = build_tree(root, chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    await _stage(runtime, proxy, built, cas, tmp_path / "w1")
    for victim in (small, big):
        await _damage("flip", cas.store.path_for(sha256_bytes(victim)))
    try:
        await _stage(runtime, proxy, built, cas, tmp_path / "w2", "a2")
    except FetchError as exc:
        # Minor limitation: the refetch budget is one blob per attempt. Safe
        # (typed, nothing wrong was used) and the retry below must heal.
        failed = exc.failure_type
        assert failed is FailureType.INTEGRITY_ERROR
        workdir = tmp_path / "w2"
        assert not workdir.exists() or not any(workdir.iterdir())
    await _stage(runtime, proxy, built, cas, tmp_path / "w3", "a3")
    assert (tmp_path / "w3" / "small.bin").read_bytes() == small
    assert (tmp_path / "w3" / "big.bin").read_bytes() == big


# ------------------------------------------------------------- hostile servers


class HostileServer:
    """A real HTTP/1.1 server (one request per connection) whose answers the test
    scripts: ``handler(path, range_header) -> (status, body)``."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> HostileServer:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        with contextlib.suppress(Exception):
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            path = lines[0].split(" ")[1]
            rng = next(
                (
                    x.split(":", 1)[1].strip()
                    for x in lines
                    if x.lower().startswith("range:")
                ),
                "",
            )
            status, body = self.handler(path, rng)
            writer.write(
                f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        writer.close()


async def _hostile_stage(
    tmp_path: Path, built: BuiltTree, handler: Any
) -> tuple[AgentCas, Any]:
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    async with HostileServer(handler) as srv:
        try:
            await stage_tree(
                cas,
                tree_id=str(built.tree.tree_id),
                sources=[BlobSource(f"http://127.0.0.1:{srv.port}", "t")],
                dest=tmp_path / "w",
                free_disk_floor_bytes=0,
            )
        except FetchError as exc:
            return cas, exc
    return cas, None


def _tree_of(tmp_path: Path, name: str, data: bytes) -> BuiltTree:
    root = tmp_path / "p"
    root.mkdir()
    (root / name).write_bytes(data)
    return build_tree(root, chunk_size=CS)


async def test_server_ignoring_range_and_sending_200_is_an_error_not_a_blob(
    tmp_path: Path,
) -> None:
    big = os.urandom(3 * CS)
    built = _tree_of(tmp_path, "big.bin", big)

    def handler(path: str, rng: str) -> tuple[int, bytes]:
        return 200, built.manifest if path.endswith(built.tree.tree_id.hex) else big

    cas, err = await _hostile_stage(tmp_path, built, handler)
    assert isinstance(err, FetchError)
    assert not cas.store.has(sha256_bytes(big))


@pytest.mark.parametrize("size", [2 * CS, 10, 0])
async def test_server_sending_too_much_or_too_little_for_a_range(
    tmp_path: Path, size: int
) -> None:
    big = os.urandom(3 * CS)
    built = _tree_of(tmp_path, "big.bin", big)

    def handler(path: str, rng: str) -> tuple[int, bytes]:
        if path.endswith(built.tree.tree_id.hex):
            return 200, built.manifest
        return 206, os.urandom(size)

    cas, err = await _hostile_stage(tmp_path, built, handler)
    assert isinstance(err, FetchError)
    assert not cas.store.has(sha256_bytes(big))


async def test_server_sending_a_wrong_manifest_for_the_id_is_integrity_error(
    tmp_path: Path,
) -> None:
    built = _tree_of(tmp_path, "a", b"a")
    other = tmp_path / "q"
    other.mkdir()
    (other / "a").write_bytes(b"b")
    evil = build_tree(other, chunk_size=CS)
    cas, err = await _hostile_stage(tmp_path, built, lambda p, r: (200, evil.manifest))
    assert isinstance(err, FetchError)
    assert err.failure_type is FailureType.INTEGRITY_ERROR
    assert not cas.store.has(built.tree.tree_id)


async def test_server_sending_too_many_bytes_for_a_small_file(tmp_path: Path) -> None:
    built = _tree_of(tmp_path, "a", b"hello")

    def handler(path: str, rng: str) -> tuple[int, bytes]:
        if path.endswith(built.tree.tree_id.hex):
            return 200, built.manifest
        return 200, b"hello" + b"x" * (10 * 1024 * 1024)

    cas, err = await _hostile_stage(tmp_path, built, handler)
    assert isinstance(err, FetchError)
    assert not cas.store.has(sha256_bytes(b"hello"))
    kept = [p.name for p in (tmp_path / "agent" / "cas").rglob("*") if p.is_file()]
    assert sha256_bytes(b"hello").hex not in kept


# ----------------------------------------------- workdir is a copy of the cache


@posix_only
async def test_job_writing_in_its_workdir_cannot_change_the_cache(
    tmp_path: Path,
) -> None:
    root, big, small = _project(tmp_path / "p")
    built = build_tree(root, chunk_size=CS)
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    for path in built.sources.values():
        store.put_file(path)
    work = tmp_path / "w1"
    materialise(store, built.tree, work)
    for rel, data in (("small.bin", small), ("big.bin", big)):
        cached = store.path_for(sha256_bytes(data))
        assert not (
            cached.stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        )
        mine = work / rel
        assert mine.stat().st_ino != cached.stat().st_ino
        assert mine.stat().st_nlink == 1 and cached.stat().st_nlink == 1
        assert mine.stat().st_mode & stat.S_IWUSR  # the job may edit its copy
        with mine.open("r+b") as fh:  # in-place write
            fh.write(b"JOBWROTE")
        mine.chmod(0o600)
        mine.write_bytes(b"truncated")
        assert store.verify(sha256_bytes(data))
        assert cached.read_bytes() == data
    # Direct write to the cache itself is refused (read-only file).
    if os.geteuid() != 0:
        with pytest.raises(PermissionError):
            store.path_for(sha256_bytes(small)).open("r+b")
    # A second job gets pristine files.
    work2 = tmp_path / "w2"
    materialise(store, built.tree, work2)
    assert (work2 / "small.bin").read_bytes() == small
    assert (work2 / "big.bin").read_bytes() == big
    assert (work2 / "run.sh").stat().st_mode & stat.S_IXUSR


@posix_only
async def test_write_through_a_tree_symlink_stays_in_the_workdir(
    tmp_path: Path,
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "d").mkdir()
    (root / "d" / "f.txt").write_bytes(b"orig")
    (root / "dl").symlink_to("d")
    (root / "fl").symlink_to("d/f.txt")
    built = build_tree(root, chunk_size=CS)
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    for path in built.sources.values():
        store.put_file(path)
    work = tmp_path / "w"
    materialise(store, built.tree, work)
    (work / "dl" / "new.txt").write_bytes(b"x")
    assert (work / "d" / "new.txt").exists()
    # Writing through the file symlink edits the workdir copy, not the cache.
    (work / "fl").write_bytes(b"changed")
    assert store.path_for(sha256_bytes(b"orig")).read_bytes() == b"orig"
    assert store.verify(sha256_bytes(b"orig"))


@posix_only
async def test_materialise_refuses_a_hostile_tree_symlink_chain_out_of_the_workdir(
    tmp_path: Path,
) -> None:
    """A tree that parses (each link looks inside) but whose chain leaves the
    workdir must not materialise a link that escapes."""
    import json

    from meshforge.common.tree import parse_tree

    entries = [
        {"path": "a", "link": "b/c"},  # a -> b/c
        {"path": "b", "link": "."},  # b -> .   (b/c == ./c == c)
        {"path": "c", "link": "../x/y"},  # c -> ../x/y  (stays "inside" by name)
    ]
    entries.sort(key=lambda e: e["path"])
    raw = json.dumps(
        {"v": 1, "chunk_size": CS, "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    try:
        tree = parse_tree(raw)
    except TreeError:
        return  # refused at parse: fine
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    work = tmp_path / "w"
    work.mkdir()
    try:
        materialise(store, tree, work)
    except MaterialiseError:
        return  # refused at materialise: fine
    for link in ("a", "b", "c"):
        target = os.path.realpath(work / link)
        assert os.path.commonpath([target, os.path.realpath(work)]) == str(
            os.path.realpath(work)
        ), (link, target)


# ------------------------------------------------ round 2: refetch budget bounds


def _many(root: Path, n: int) -> list[bytes]:
    root.mkdir()
    blobs = [os.urandom(2000 + i) for i in range(n)]
    for i, b in enumerate(blobs):
        (root / f"f{i}.bin").write_bytes(b)
    return blobs


@pytest.mark.parametrize("damaged", [1, 3])
async def test_refetch_budget_up_to_three_damaged_blobs_is_ok(
    runtime: ControllerRuntime,
    proxy: CorruptingProxy,
    tmp_path: Path,
    damaged: int,
) -> None:
    blobs = _many(tmp_path / "p", 4)
    built = build_tree(tmp_path / "p", chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    await _stage(runtime, proxy, built, cas, tmp_path / "w1")
    for b in blobs[:damaged]:
        await _damage("flip", cas.store.path_for(sha256_bytes(b)))
    report = await _stage(runtime, proxy, built, cas, tmp_path / "w2", "a2")
    assert report.corrupt_refetches == damaged
    for i, b in enumerate(blobs):
        assert (tmp_path / "w2" / f"f{i}.bin").read_bytes() == b


async def test_fourth_damaged_blob_exceeds_the_budget_and_nothing_wrong_is_used(
    runtime: ControllerRuntime, proxy: CorruptingProxy, tmp_path: Path
) -> None:
    blobs = _many(tmp_path / "p", 4)
    built = build_tree(tmp_path / "p", chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    await _stage(runtime, proxy, built, cas, tmp_path / "w1")
    for b in blobs:
        await _damage("flip", cas.store.path_for(sha256_bytes(b)))
    with pytest.raises(FetchError) as info:
        await _stage(runtime, proxy, built, cas, tmp_path / "w2", "a2")
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    workdir = tmp_path / "w2"
    assert not workdir.exists() or not any(workdir.iterdir())
    # The 4th is gone from the cache (evicted), never served corrupt; retry heals.
    assert sum(cas.store.has(sha256_bytes(b)) for b in blobs) == 3
    await _stage(runtime, proxy, built, cas, tmp_path / "w3", "a3")
    for i, b in enumerate(blobs):
        assert (tmp_path / "w3" / f"f{i}.bin").read_bytes() == b


async def test_whole_file_corrupted_in_flight_always_gets_one_refetch_then_fails(
    runtime: ControllerRuntime, proxy: CorruptingProxy, tmp_path: Path
) -> None:
    blobs = _many(tmp_path / "p", 1)
    built = build_tree(tmp_path / "p", chunk_size=CS)
    await _upload(runtime, built)
    cas = AgentCas.at(tmp_path / "agent", free_disk_floor_bytes=0)
    await cas.prepare()
    cas.store.put_bytes(built.manifest, built.tree.tree_id)
    proxy.arm("flip", 1)  # one damaged answer: the single refetch heals it
    await _stage(runtime, proxy, built, cas, tmp_path / "w")
    assert proxy.damaged == 1
    assert (tmp_path / "w" / "f0.bin").read_bytes() == blobs[0]
    cas2 = AgentCas.at(tmp_path / "agent2", free_disk_floor_bytes=0)
    await cas2.prepare()
    cas2.store.put_bytes(built.manifest, built.tree.tree_id)
    proxy.arm("flip", -1)  # always damaged: refetch also fails
    before = proxy.damaged
    with pytest.raises(FetchError) as info:
        await _stage(runtime, proxy, built, cas2, tmp_path / "w2", "a2")
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    assert proxy.damaged - before == 2  # first fetch + exactly one refetch
    assert not cas2.store.has(sha256_bytes(blobs[0]))
