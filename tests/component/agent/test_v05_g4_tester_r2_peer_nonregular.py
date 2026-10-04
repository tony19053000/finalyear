"""V0.5 G4 tester round 2: non-regular files at a blob path (fix round 1, defect 4).

The peer server must answer every non-regular object at a CAS path promptly
(never block the loop, never serve it, never leak a descriptor), and must still
serve regular blobs completely after the O_NONBLOCK change.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sys
import time
from pathlib import Path

import pytest
from test_v05_g4_tester_peer_abuse import Env, _exchange, _responses
from test_v05_g4_tester_peer_abuse import env as env

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(60),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX only"),
]


def _fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _replace_blob(env: Env, name: str, make: object) -> Path:
    path = env.cas.store.path_for(env.digest(name))
    path.chmod(0o600)
    path.unlink()
    make(path)  # type: ignore[operator]
    return path


async def _status(env: Env, name: str) -> int:
    raw = await _exchange(env.port, env.req(name), wait=4.0)
    got = _responses(raw)
    assert got, "no answer at all"
    return got[0][0]


async def _innocent_ok(env: Env) -> None:
    started = time.monotonic()
    raw = await _exchange(env.port, env.req("big.bin"), wait=5.0)
    got = _responses(raw)
    assert got and got[0][0] == 200 and got[0][2] == env.big
    assert time.monotonic() - started < 2.0


async def test_symlink_to_a_fifo_is_refused_at_once(env: Env, tmp_path: Path) -> None:
    fifo = tmp_path / "evil.fifo"
    os.mkfifo(fifo)
    _replace_blob(env, "small.txt", lambda p: p.symlink_to(fifo))
    started = time.monotonic()
    assert await _status(env, "small.txt") == 404
    assert time.monotonic() - started < 2.0
    await _innocent_ok(env)


async def test_symlink_to_a_device_node_is_refused_and_never_streams(
    env: Env,
) -> None:
    _replace_blob(env, "small.txt", lambda p: p.symlink_to("/dev/zero"))
    raw = await _exchange(env.port, env.req("small.txt"), wait=3.0)
    got = _responses(raw)
    assert got and got[0][0] == 404 and got[0][2] == b""
    await _innocent_ok(env)


async def test_fifo_with_a_writer_attached_is_not_served_and_does_not_block(
    env: Env,
) -> None:
    path = _replace_blob(env, "small.txt", os.mkfifo)
    # A writer end open (nonblocking needs a reader; hold a reader ourselves).
    rfd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    wfd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(wfd, b"attacker bytes")
        started = time.monotonic()
        raw = await _exchange(env.port, env.req("small.txt"), wait=4.0)
        got = _responses(raw)
        assert got and got[0][0] == 404 and b"attacker" not in raw
        assert time.monotonic() - started < 2.0
        await _innocent_ok(env)
    finally:
        os.close(wfd)
        os.close(rfd)


async def test_unix_socket_and_directory_at_a_blob_path(env: Env) -> None:
    sock = socket.socket(socket.AF_UNIX)
    holder: list[Path] = []

    def make_sock(p: Path) -> None:
        cwd = os.getcwd()
        os.chdir(p.parent)  # AF_UNIX paths are short: bind by relative name
        try:
            sock.bind(p.name)
        finally:
            os.chdir(cwd)
        holder.append(p)

    try:
        _replace_blob(env, "small.txt", make_sock)
        assert await _status(env, "small.txt") == 404
        _replace_blob(env, "empty.txt", lambda p: p.mkdir())
        assert await _status(env, "empty.txt") == 404
        await _innocent_ok(env)
    finally:
        sock.close()


async def test_no_descriptor_leak_across_many_nonregular_requests(env: Env) -> None:
    path = _replace_blob(env, "small.txt", os.mkfifo)
    await _status(env, "small.txt")  # warm
    before = _fds()
    for _ in range(80):
        assert await _status(env, "small.txt") == 404
    await asyncio.sleep(0.2)
    assert _fds() <= before + 3, (before, _fds(), path)


async def test_regular_blobs_are_complete_and_ranges_work_after_nonblock_fix(
    env: Env,
) -> None:
    # Large regular file: a non-blocking fd would give short/EAGAIN reads.
    for _ in range(5):
        await _innocent_ok(env)
    digest = env.digest("big.bin")
    raw = await _exchange(
        env.port, env.req(digest, extra="range: bytes=100000-300000\r\n")
    )
    got = _responses(raw)
    assert got[0][0] == 206 and got[0][2] == env.big[100000:300001]
    # And the fd flag really is blocking again: serve concurrently 20 at once.
    results = await asyncio.gather(
        *(_exchange(env.port, env.req("big.bin"), wait=8.0) for _ in range(20))
    )
    for r in results:
        g = _responses(r)
        assert g and g[0][0] == 200 and g[0][2] == env.big
