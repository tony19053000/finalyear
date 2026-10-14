"""V0.7 G6 exit list: a cancelled agent HTTP request leaves no socket open.

Found by the r3 cancel tests under 16 CPU burners (4 of 5 runs red with
``ResourceWarning: unclosed <socket ...>`` towards the controller's blob API;
the cancelled caller was ``fetch_bundle``): ``anyio.connect_tcp`` drops a
stream that connected just as the cancel arrived
(:mod:`meshforge.agent.http_connect`).

Real loopback servers, real httpx clients. The random-cancel probe runs in a
child process (a leaked socket is found by garbage collection at any later
moment, so the control run - httpx's default transport, which must leak to
prove the probe reaches the race - would fail unrelated tests in this one).
"""

from __future__ import annotations

import asyncio
import json
import ssl
import subprocess
import sys
import textwrap
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from meshforge.agent import http_connect
from meshforge.agent.http_connect import cancel_safe_transport

pytestmark = [
    pytest.mark.component,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="loopback timing, POSIX"),
]

#: Child process. KIND "connect": ROUNDS GETs to a loopback HTTP server, each
#: cancelled after 0-6 loop turns. KIND "handshake": ROUNDS GETs to a server
#: that accepts TCP and never answers the TLS handshake, each cancelled 50 ms
#: after the server accepted it. Prints the unclosed sockets/transports found
#: and the orphaned steps still running at the end.
_PROBE = textwrap.dedent(
    """
    import asyncio, gc, json, random, ssl, sys, warnings
    import httpx
    from meshforge.agent import http_connect

    MODE, KIND, ROUNDS = sys.argv[1], sys.argv[2], int(sys.argv[3])

    def context():
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        return c

    def transport():
        if MODE == "safe":
            return http_connect.cancel_safe_transport(context())
        return httpx.AsyncHTTPTransport(verify=context())

    accepted = None
    held = []

    async def answer(reader, writer):
        if KIND == "handshake":
            held.append(writer)
            accepted.put_nowait(None)
            return
        try:
            await reader.read(4096)
            writer.write(b"HTTP/1.1 200 OK\\r\\ncontent-length: 2\\r\\n\\r\\nok")
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def main():
        global accepted
        accepted = asyncio.Queue()
        server = await asyncio.start_server(answer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        scheme = "https" if KIND == "handshake" else "http"
        rnd = random.Random(20261014)
        for i in range(ROUNDS):
            async def one():
                async with httpx.AsyncClient(transport=transport(), timeout=1) as c:
                    await c.get(f"{scheme}://127.0.0.1:{port}/")
            task = asyncio.ensure_future(one())
            if KIND == "handshake":
                await asyncio.wait_for(accepted.get(), 10)
                await asyncio.sleep(0.05)
            else:
                for _ in range(rnd.randint(0, 6)):
                    await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        for _ in range(100):
            if not http_connect._ORPHANS:
                break
            await asyncio.sleep(0.05)
        for writer in held:
            writer.close()
        server.close()
        return len(http_connect._ORPHANS)

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        pending = asyncio.run(main())
        gc.collect()
        gc.collect()
    leaks = sum("unclosed" in str(w.message) for w in seen)
    print(json.dumps({"leaks": leaks, "pending": pending}))
    """
)


def _probe(mode: str, kind: str, rounds: int) -> dict[str, int]:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, mode, kind, str(rounds)],
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-3000:]
    result: dict[str, int] = json.loads(done.stdout.strip().splitlines()[-1])
    return result


def test_the_probe_reaches_the_connect_race_with_httpx_default_transport() -> None:
    """The control: without the fix, cancels at random moments leak."""
    assert _probe("default", "connect", 3000)["leaks"] > 0, "the probe proves nothing"


def test_a_cancel_at_any_moment_of_the_connect_leaks_nothing() -> None:
    assert _probe("safe", "connect", 3000) == {"leaks": 0, "pending": 0}


def test_the_probe_reaches_the_handshake_leak_with_httpx_default_transport() -> None:
    assert _probe("default", "handshake", 5)["leaks"] > 0, "the probe proves nothing"


def test_a_cancel_during_the_tls_handshake_leaks_nothing() -> None:
    assert _probe("safe", "handshake", 5) == {"leaks": 0, "pending": 0}


def _client_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


@pytest_asyncio.fixture
async def silent_tls_port() -> AsyncIterator[tuple[int, asyncio.Queue[None]]]:
    """Accepts TCP and never answers the TLS handshake."""
    accepted: asyncio.Queue[None] = asyncio.Queue()
    held: list[asyncio.StreamWriter] = []

    async def hold(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        held.append(writer)
        accepted.put_nowait(None)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], accepted
    finally:
        for writer in held:
            writer.close()
        server.close()


@pytest.mark.asyncio
async def test_a_cancel_during_the_tls_handshake_is_immediate(
    silent_tls_port: tuple[int, asyncio.Queue[None]],
) -> None:
    """The shielded handshake goes on in the background; the caller does not
    wait for it (2 s handshake timeout)."""
    port, accepted = silent_tls_port
    worst = 0.0
    for _ in range(5):

        async def one() -> None:
            transport = cancel_safe_transport(_client_context())
            async with httpx.AsyncClient(transport=transport, timeout=2) as c:
                await c.get(f"https://127.0.0.1:{port}/")

        task = asyncio.ensure_future(one())
        await asyncio.wait_for(accepted.get(), 10)
        await asyncio.sleep(0.05)  # connected: the client is in the handshake
        started = time.monotonic()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("the request was not cancelled")
        worst = max(worst, time.monotonic() - started)
    assert worst < 0.5, worst
    for _ in range(100):  # the orphaned handshakes end at their timeout
        if not http_connect._ORPHANS:
            break
        await asyncio.sleep(0.05)
    assert not http_connect._ORPHANS


async def _answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.read(4096)
    writer.write(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
    await writer.drain()
    writer.close()


@pytest.mark.asyncio
async def test_a_request_through_it_still_works() -> None:
    server = await asyncio.start_server(_answer, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        transport = cancel_safe_transport(_client_context())
        async with httpx.AsyncClient(transport=transport, timeout=5) as client:
            response = await client.get(f"http://127.0.0.1:{port}/")
    finally:
        server.close()
    assert (response.status_code, response.text) == (200, "ok")
