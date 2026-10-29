"""V1.0 G8 F1 tester: the refusal heuristic must not fire for ordinary
network events on a connection whose certificate the server ACCEPTED."""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest
from test_v10_g8_f1_cert_refused import Pki, material  # noqa: F401

from meshforge.agent.http_refusal import transport_failure
from meshforge.agent.http_tls import AgentHttpTls
from meshforge.common.tls_refusal import CLOSED_AFTER_HANDSHAKE, find_refusal

pytestmark = [pytest.mark.component, pytest.mark.timeout(120)]

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@contextlib.asynccontextmanager
async def _serve(material: Pki, handler: Handler) -> AsyncIterator[str]:  # noqa: F811
    ctx = material.server_tls.ssl_context(client_certs="required")

    async def wrapped(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            await handler(r, w)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            w.close()

    srv = await asyncio.start_server(wrapped, "127.0.0.1", 0, ssl=ctx)
    try:
        yield f"https://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    finally:
        srv.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), 5)


def _client(material: Pki, **kw: object) -> httpx.AsyncClient:  # noqa: F811
    t = AgentHttpTls(context=material.agent_context("good")).transport()
    return httpx.AsyncClient(transport=t, timeout=10, **kw)  # type: ignore[arg-type]


OK = b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok"


@pytest.mark.asyncio
async def test_idle_close_after_answer_is_not_a_refusal(material: Pki) -> None:  # noqa: F811
    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        w.write(OK)
        await w.drain()  # then close (handler returns)

    async with _serve(material, h) as url, _client(material) as c:
        assert (await c.get(f"{url}/a")).text == "ok"
        await asyncio.sleep(0.2)  # server closed the pooled connection
        # httpcore detects the dead pooled connection and reconnects, or errors
        # without a refusal marker; either is acceptable, a refusal is not.
        failure: httpx.TransportError | None = None
        try:
            await c.get(f"{url}/b")
        except httpx.TransportError as exc:
            failure = exc
        if failure is not None:
            assert find_refusal(failure) is None, transport_failure(failure)


@pytest.mark.asyncio
async def test_partial_response_then_reset_is_not_a_refusal(material: Pki) -> None:  # noqa: F811
    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        w.write(b"HTTP/1.1 200 OK\r\ncontent-length: 100\r\n\r\nab")
        await w.drain()
        w.transport.abort()

    async with _serve(material, h) as url, _client(material) as c:
        with pytest.raises(httpx.TransportError) as caught:
            await c.get(f"{url}/a")
        assert find_refusal(caught.value) is None, transport_failure(caught.value)


@pytest.mark.asyncio
async def test_upload_rejected_early_with_413_is_not_a_refusal(material: Pki) -> None:  # noqa: F811
    """Server answers 413 and closes while the client is still writing a big body."""

    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        w.write(
            b"HTTP/1.1 413 Too Large\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
        )
        await w.drain()
        w.transport.abort()

    async with _serve(material, h) as url, _client(material) as c:
        bad = 0
        for _ in range(10):
            try:
                resp = await c.put(f"{url}/u", content=b"x" * (64 * 1024 * 1024))
                assert resp.status_code == 413
            except httpx.TransportError as exc:
                if find_refusal(exc) is not None:
                    bad += 1
        assert bad == 0, f"{bad}/10 early-413 uploads misreported as cert refusal"


@pytest.mark.asyncio
async def test_server_closing_before_any_byte_with_accepted_cert(material: Pki) -> None:  # noqa: F811
    """Known heuristic limit, ACCEPTED by the Lead (V1.0 G8 F1 D2; documented
    in ``meshforge.agent.http_refusal``): a server that accepted the
    certificate but closes before sending any byte looks exactly like an
    asyncio refusal on the wire, so it IS reported as one - with the hedged
    wording ("most likely refused"), never as a certain refusal."""

    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")  # then close without answering

    async with _serve(material, h) as url, _client(material) as c:
        with pytest.raises(httpx.TransportError) as caught:
            await c.get(f"{url}/a")
        refusal = find_refusal(caught.value)
        assert refusal is not None
        assert refusal.reason == CLOSED_AFTER_HANDSHAKE
        text = transport_failure(caught.value)
        assert (
            "without answering: it most likely refused this laptop's certificate"
            in text
        )
        assert "TLS alert" not in text  # never worded as a certain refusal


@pytest.mark.asyncio
async def test_read_timeout_is_not_a_refusal(material: Pki) -> None:  # noqa: F811
    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        await asyncio.sleep(5)

    async with _serve(material, h) as url:
        t = AgentHttpTls(context=material.agent_context("good")).transport()
        async with httpx.AsyncClient(transport=t, timeout=0.5) as c:
            with pytest.raises(httpx.ReadTimeout) as caught:
                await c.get(f"{url}/a")
            assert find_refusal(caught.value) is None


@pytest.mark.asyncio
async def test_cancellation_mid_request_propagates(material: Pki) -> None:  # noqa: F811
    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        await asyncio.sleep(30)

    async with _serve(material, h) as url, _client(material) as c:
        task = asyncio.ensure_future(c.get(f"{url}/a"))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_small_refused_upload_still_marked_and_big_refused_upload_is_transport_error(
    material: Pki,  # noqa: F811
) -> None:
    async def never(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        return

    ctx = material.server_tls.ssl_context(client_certs="required")
    srv = await asyncio.start_server(never, "127.0.0.1", 0, ssl=ctx)
    url = f"https://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    try:
        for cert in ("foreign", "expired"):
            t = AgentHttpTls(context=material.agent_context(cert)).transport()
            async with httpx.AsyncClient(transport=t, timeout=10) as c:
                marked = 0
                for _ in range(10):
                    with pytest.raises(httpx.TransportError) as caught:
                        await c.put(f"{url}/u", content=b"x" * 1000)
                    marked += find_refusal(caught.value) is not None
                assert marked == 10, (cert, marked)
                for _ in range(5):  # big body: type must stay a transport error
                    with pytest.raises(httpx.TransportError):
                        await c.put(f"{url}/u", content=b"x" * (8 * 1024 * 1024))
    finally:
        srv.close()


@pytest.mark.asyncio
async def test_chunked_upload_early_close_and_keepalive_counting(material: Pki) -> None:  # noqa: F811
    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        while True:
            await r.readuntil(b"\r\n\r\n")
            w.write(b"HTTP/1.1 413 X\r\ncontent-length: 0\r\n\r\n")
            await w.drain()
            if r.at_eof():
                return
            w.transport.abort()
            return

    async def gen():  # type: ignore[no-untyped-def]
        for _ in range(2000):
            yield b"y" * 65536

    async with _serve(material, h) as url, _client(material) as c:
        for _ in range(5):
            failure: httpx.TransportError | None = None
            try:
                await c.put(f"{url}/u", content=gen())
            except httpx.TransportError as exc:
                failure = exc
            if failure is not None:
                assert find_refusal(failure) is None, transport_failure(failure)


@pytest.mark.asyncio
async def test_keepalive_second_request_closed_unanswered_is_not_marked(
    material: Pki,  # noqa: F811
) -> None:
    """First request answered on the connection, then the server closes
    without answering the second one on the SAME connection: an ordinary
    keep-alive event, never a certificate refusal."""

    async def h(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        w.write(OK)
        await w.drain()
        await r.readuntil(b"\r\n\r\n")  # second request, then close silently

    async with _serve(material, h) as url, _client(material) as c:
        assert (await c.get(f"{url}/a")).text == "ok"
        with pytest.raises(httpx.TransportError) as caught:
            await c.get(f"{url}/b")
        assert find_refusal(caught.value) is None, transport_failure(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "marked"),
    [(60 * 1024, True), (64 * 1024 + 1, False), (64 * 1024 + 4096, False)],
)
async def test_refused_upload_marking_follows_total_bytes_written(
    material: Pki,  # noqa: F811
    body: int,
    marked: bool,
) -> None:
    """Total written (headers + body) <= 64 KiB is marked, above it is not.
    (Exact-byte boundary is covered in the unit test; headers make the
    network-level boundary a few hundred bytes wide.)"""

    async def never(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        return

    ctx = material.server_tls.ssl_context(client_certs="required")
    srv = await asyncio.start_server(never, "127.0.0.1", 0, ssl=ctx)
    url = f"https://127.0.0.1:{srv.sockets[0].getsockname()[1]}"
    try:
        t = AgentHttpTls(context=material.agent_context("foreign")).transport()
        async with httpx.AsyncClient(transport=t, timeout=10) as c:
            for _ in range(5):
                with pytest.raises(httpx.TransportError) as caught:
                    await c.put(f"{url}/u", content=b"x" * body)
                assert (find_refusal(caught.value) is not None) is marked, body
    finally:
        srv.close()
