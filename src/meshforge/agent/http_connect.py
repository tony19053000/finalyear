"""Opening HTTP connections that a cancel cannot leak (V0.7 G6 exit list).

The agent cancels HTTP requests on purpose: a job cancelled while its bundle
or tree is being fetched, an upload stopped, a probe past its deadline. Two
steps of opening a connection, in the libraries under httpx, lose the socket
when the cancel lands at the wrong moment:

* ``anyio.connect_tcp`` (anyio 4.15.1) races its connection attempts in a
  task group (happy eyeballs, even for one IP address). A cancel arriving
  just as an attempt has connected ends the group with ``CancelledError``
  and the connected stream is dropped, never closed.
* httpcore's ``AnyIOStream.start_tls`` (1.0.9) closes the TCP stream when
  the TLS handshake fails with an ``Exception``; ``CancelledError`` is not
  one, and the connection that called it has not stored the stream yet.

The socket then stays open until garbage collection finds it (with a
``ResourceWarning``). Measured: 375 of 3000 requests cancelled at random
moments against a loopback server; under 16 CPU burners the r3 cancel tests
were red in 4 of 5 runs this way (the cancelled caller: ``fetch_bundle``).

The fix follows ``DockerRuntime._owned``: the connect, and the handshake,
run in their own task that the caller's cancel does not reach. The caller is
cancelled at once, as before; the step finishes on its own - bounded by the
client's connect timeout, which httpcore applies inside both - and what it
made is then closed. Orphaned steps are kept in a set (asyncio keeps only
weak references to tasks). What is left: a step still running when the
event loop itself shuts down is cancelled by the loop and can leak as before
(the process ends right after).

Only the agent's HTTPS clients use it (:meth:`AgentHttpTls.transport`). The
controller makes no outgoing HTTP requests; the plain-HTTP dev mode keeps
httpx's default transport.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Coroutine, Iterable
from typing import Any

import httpcore
import httpx

__all__ = ["CancelSafeBackend", "cancel_safe_transport"]

#: Steps whose caller was cancelled, and the closes of what they made.
_ORPHANS: set[asyncio.Task[Any]] = set()


async def _uncancellable(
    step: Coroutine[Any, Any, httpcore.AsyncNetworkStream],
) -> httpcore.AsyncNetworkStream:
    """``await step``, but a cancel of the caller does not reach ``step``:
    the caller gets ``CancelledError`` at once, ``step`` runs to its end and
    the stream it returns is closed."""
    work = asyncio.ensure_future(step)
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        _ORPHANS.add(work)
        work.add_done_callback(_close_late)
        raise


def _close_late(done: asyncio.Task[httpcore.AsyncNetworkStream]) -> None:
    _ORPHANS.discard(done)
    if done.cancelled() or done.exception() is not None:
        return  # nothing was made (a failing step closes what it had)
    task = asyncio.ensure_future(done.result().aclose())
    _ORPHANS.add(task)
    task.add_done_callback(_forget)


def _forget(task: asyncio.Task[Any]) -> None:
    _ORPHANS.discard(task)
    if not task.cancelled():
        task.exception()  # a failed close: the stream is gone either way


class _CancelSafeStream(httpcore.AsyncNetworkStream):
    """A connected stream whose TLS handshake a cancel cannot leak."""

    def __init__(self, inner: httpcore.AsyncNetworkStream) -> None:
        self._inner = inner

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        return await self._inner.read(max_bytes, timeout)

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        await self._inner.write(buffer, timeout)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        return await _uncancellable(
            self._inner.start_tls(ssl_context, server_hostname, timeout)
        )

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class CancelSafeBackend(httpcore.AsyncNetworkBackend):
    """httpcore's anyio backend, with the connect and the TLS handshake
    shielded from the caller's cancel (module docstring). The ``timeout``
    parameters are httpcore's interface (hence ``noqa: ASYNC109``)."""

    def __init__(self) -> None:
        self._inner = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await _uncancellable(
            self._inner.connect_tcp(host, port, timeout, local_address, socket_options)
        )
        return _CancelSafeStream(stream)

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self._inner.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def cancel_safe_transport(verify: ssl.SSLContext) -> httpx.AsyncHTTPTransport:
    """An httpx transport whose connections open through
    :class:`CancelSafeBackend`.

    httpx 0.28 takes no network backend, so the one of the connection pool it
    builds is replaced (pinned by a unit test: an httpx upgrade that changes
    the pool fails there instead of silently leaking again; and by a
    component test counting this backend's ``connect_tcp`` on a real HTTPS
    fetch - ``tests/component/agent/test_v07_g7_item0_http_backend.py`` - so a
    rename of the attribute the pool *reads* fails loudly too).

    The transport is built here with ``verify=`` only. Any future pool
    option - connection ``limits``, ``http2``, retries, ``local_address`` -
    must be passed to this ``httpx.AsyncHTTPTransport(...)`` call (add a
    parameter), never by building another transport elsewhere: a transport
    made anywhere else opens its connections through httpcore's default
    backend and brings the cancel leak back."""
    transport = httpx.AsyncHTTPTransport(verify=verify)
    pool = transport._pool
    if not isinstance(pool, httpcore.AsyncConnectionPool):
        raise TypeError(f"unexpected httpx connection pool {type(pool).__name__}")
    pool._network_backend = CancelSafeBackend()
    return transport
