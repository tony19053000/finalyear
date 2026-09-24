"""FaultLink: an in-process asyncio TCP proxy with Toxiproxy's fault vocabulary (D23).

Imported as the top-level module ``mf_faultproxy`` (``tests/conftest.py`` puts
``tests/_support`` on ``sys.path``). Used by the cluster harness
(``mf_cluster``) to put one link between every agent process and the controller,
and tested on raw sockets by ``tests/cluster/test_faultproxy.py``.

Why not the Toxiproxy binary (V0.3 brief §5f D23): it needs a download or a
Docker container with host networking, neither dependable on the team's Windows
laptops; this runs after a plain ``uv sync``, anywhere, and lets the harness
**retarget** a link when the controller restarts on a new port. Neither tool is
a real cable pull at kernel level - the 🧑 lab run (0.3.15) is that evidence.

Threading model
---------------
The harness drives *subprocesses* synchronously, so the proxy must not depend on
the test's event loop. :class:`FaultLoop` owns one event loop on a daemon thread;
every :class:`FaultLink` method is a thread-safe, synchronous call that runs a
coroutine on that loop and waits (bounded) for it. All link state is only ever
touched on the loop thread, so no lock is needed.

Fault semantics (each has its own raw-socket test)
--------------------------------------------------
``down()`` / ``up()``
    ``down`` closes the listening socket (new connections are *refused*:
    ``ECONNREFUSED``) and resets every open connection (RST both sides). ``up``
    listens again on the same port.
``reset_peer()``
    RST every open connection on both sides (``SO_LINGER`` 0 + abort), keep
    accepting new ones. Both peers see ``ECONNRESET``.
``blackhole(True)``
    The cable-pull analogue: every open connection and every new one keeps its
    sockets open but bytes in both directions are silently dropped, and neither
    a FIN nor a RST is ever forwarded (a peer that closes is not told to the
    other side). New connections are accepted but never connected upstream (a
    real SYN into a pulled cable is never answered; the proxy cannot un-accept,
    so the client sees a connection that carries nothing).
``blackhole(False)``
    New connections behave normally again. Connections that were blackholed
    lost bytes, so their byte streams cannot be resumed; they are **reset** (RST
    both sides), which is what a real host does to a connection it no longer
    knows once the cable is back.
``latency(ms, jitter_ms)``
    Every chunk is delivered ``ms`` (+ uniform ``0..jitter_ms``) after it was
    read, in each direction, never reordered (a chunk is never delivered before
    the one read before it). ``latency(0)`` clears it. Applies to chunks read
    after the call.
``bandwidth(bytes_per_s)``
    Each direction of each connection is paced to at most ``bytes_per_s``;
    ``None`` clears it.
``retarget(host, port)``
    New connections go to the new upstream; open ones keep theirs.

Upstream connect failure (refused, timeout) resets the downstream connection, so
a client of a link whose upstream is gone sees a failure promptly, like dialling
a closed port.

Buffers are bounded: each direction holds at most ``MAX_QUEUED_CHUNKS`` chunks of
at most ``CHUNK_BYTES``; a full queue stops reading from that side (TCP back-
pressure), so a slow or paced link never grows the test process.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import contextlib
import dataclasses
import itertools
import random
import socket
import struct
import threading
from collections.abc import Coroutine
from typing import Any, Final, TypeVar

__all__ = [
    "CHUNK_BYTES",
    "MAX_QUEUED_CHUNKS",
    "FaultLink",
    "FaultLoop",
    "LinkCounters",
]

T = TypeVar("T")

CHUNK_BYTES: Final = 64 * 1024
"""Largest read from one socket in one step."""

MAX_QUEUED_CHUNKS: Final = 16
"""Chunks buffered per direction per connection (<= 1 MiB) before reads pause."""

PACING_SLICE_S: Final = 0.05
"""Granularity of bandwidth pacing: one write per this much link time."""

UPSTREAM_CONNECT_TIMEOUT_S: Final = 5.0
CALL_TIMEOUT_S: Final = 10.0
"""Bound on every synchronous call into the proxy loop."""

_LINGER_RST: Final = struct.pack("ii", 1, 0)


@dataclasses.dataclass(frozen=True)
class LinkCounters:
    """A snapshot of one link's counters (taken on the loop thread)."""

    connections_total: int
    connections_active: int
    bytes_up: int
    """Bytes delivered client -> upstream."""
    bytes_down: int
    """Bytes delivered upstream -> client."""
    bytes_dropped: int
    upstream_failures: int
    faults: dict[str, int]


class FaultLoop:
    """One asyncio loop on a daemon thread; owns every :class:`FaultLink` on it."""

    def __init__(self, name: str = "faultproxy") -> None:
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._main, name=name, daemon=True)
        self._links: list[FaultLink] = []
        self._closed = False
        self._thread.start()
        if not self._ready.wait(CALL_TIMEOUT_S):  # pragma: no cover - defensive
            raise RuntimeError("fault proxy loop did not start")

    def _main(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        try:
            self._loop.run_forever()
        finally:
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def call(
        self, coro: Coroutine[Any, Any, T], timeout_s: float = CALL_TIMEOUT_S
    ) -> T:
        """Run ``coro`` on the proxy loop and return its result (thread-safe)."""
        if self._closed:
            coro.close()
            raise RuntimeError("FaultLoop is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout_s)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(
                f"fault proxy call timed out after {timeout_s} s"
            ) from None

    def link(
        self,
        upstream: tuple[str, int],
        *,
        name: str = "link",
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
    ) -> FaultLink:
        """Create and start a link listening on ``listen_host:listen_port``."""
        link = FaultLink(self, upstream, name=name)
        self.call(link._start(listen_host, listen_port))
        self._links.append(link)
        return link

    @property
    def links(self) -> tuple[FaultLink, ...]:
        return tuple(self._links)

    def close(self) -> None:
        """Close every link, stop the loop and join its thread (idempotent)."""
        if self._closed:
            return
        for link in self._links:
            with contextlib.suppress(Exception):
                self.call(link._close())
        with contextlib.suppress(Exception):
            self.call(_cancel_all_tasks())
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(CALL_TIMEOUT_S)

    def __enter__(self) -> FaultLoop:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


async def _cancel_all_tasks() -> None:
    """Cancel and await every other task on the loop (connection handlers)."""
    me = asyncio.current_task()
    tasks = [t for t in asyncio.all_tasks() if t is not me]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class _Conn:
    """One proxied connection: the client side and (maybe) the upstream side."""

    def __init__(self, cid: int, client: asyncio.StreamWriter) -> None:
        self.cid = cid
        self.client = client
        self.upstream: asyncio.StreamWriter | None = None
        self.poisoned = False
        """Bytes were dropped: this byte stream can never be resumed."""
        self.tasks: list[asyncio.Task[None]] = []
        self.closed = False

    def is_poisoned(self) -> bool:
        """``poisoned`` re-read after an ``await`` (a method, so it is not narrowed)."""
        return self.poisoned


def _rst(writer: asyncio.StreamWriter | None) -> None:
    """Abort ``writer``'s socket with SO_LINGER 0, so the peer gets a RST."""
    if writer is None:
        return
    sock = writer.get_extra_info("socket")
    if sock is not None and sock.fileno() >= 0:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, _LINGER_RST)
    writer.transport.abort()


class FaultLink:
    """A fault-injecting TCP proxy in front of one upstream. See the module doc.

    Create with :meth:`FaultLoop.link`. Every public method is synchronous and
    thread-safe; it returns once the fault is in effect.
    """

    def __init__(
        self, fault_loop: FaultLoop, upstream: tuple[str, int], *, name: str
    ) -> None:
        self._fl = fault_loop
        self.name = name
        self._upstream = upstream
        self._server: asyncio.Server | None = None
        self._listen: tuple[str, int] = ("127.0.0.1", 0)
        self._conns: dict[int, _Conn] = {}
        self._ids = itertools.count(1)
        self._blackholed = False
        self._latency_s = 0.0
        self._jitter_s = 0.0
        self._bytes_per_s: float | None = None
        self._rng = random.Random()  # noqa: S311 - jitter, not crypto
        self._faults: collections.Counter[str] = collections.Counter()
        self._total = 0
        self._bytes = {"up": 0, "down": 0}
        self._dropped = 0
        self._upstream_failures = 0

    # ------------------------------------------------------------ public API

    @property
    def address(self) -> tuple[str, int]:
        """``(host, port)`` clients dial."""
        return self._listen

    @property
    def target(self) -> str:
        """``host:port`` clients dial, e.g. an agent's ``controller_address``."""
        return f"{self._listen[0]}:{self._listen[1]}"

    @property
    def upstream(self) -> tuple[str, int]:
        return self._upstream

    def down(self) -> None:
        self._fl.call(self._down())

    def up(self) -> None:
        self._fl.call(self._up())

    def reset_peer(self) -> None:
        self._fl.call(self._reset_peer())

    def blackhole(self, enabled: bool = True) -> None:
        self._fl.call(self._set_blackhole(enabled))

    def latency(self, ms: float, jitter_ms: float = 0.0) -> None:
        if ms < 0 or jitter_ms < 0:
            raise ValueError("latency and jitter must be >= 0")
        self._fl.call(self._set_latency(ms / 1000.0, jitter_ms / 1000.0))

    def bandwidth(self, bytes_per_s: float | None) -> None:
        if bytes_per_s is not None and bytes_per_s <= 0:
            raise ValueError("bytes_per_s must be > 0 (or None to clear)")
        self._fl.call(self._set_bandwidth(bytes_per_s))

    def retarget(self, host: str, port: int) -> None:
        self._fl.call(self._retarget((host, port)))

    def clear(self) -> None:
        """Remove every toxic (blackhole, latency, bandwidth); ``up`` if down."""
        self._fl.call(self._clear())

    def counters(self) -> LinkCounters:
        return self._fl.call(self._counters())

    # ---------------------------------------------------------- loop side

    async def _start(self, host: str, port: int) -> None:
        self._server = await asyncio.start_server(self._accept, host, port)
        bound = self._server.sockets[0].getsockname()
        self._listen = (host, int(bound[1]))

    async def _counters(self) -> LinkCounters:
        return LinkCounters(
            connections_total=self._total,
            connections_active=len(self._conns),
            bytes_up=self._bytes["up"],
            bytes_down=self._bytes["down"],
            bytes_dropped=self._dropped,
            upstream_failures=self._upstream_failures,
            faults=dict(self._faults),
        )

    async def _down(self) -> None:
        self._faults["down"] += 1
        await self._stop_listening()
        for conn in list(self._conns.values()):
            self._reset_conn(conn)

    async def _up(self) -> None:
        if self._server is not None:
            return
        self._faults["up"] += 1
        self._server = await asyncio.start_server(
            self._accept, self._listen[0], self._listen[1]
        )

    async def _stop_listening(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            # Not wait_closed(): since 3.12 it also waits for open connections.
            server.close()

    async def _reset_peer(self) -> None:
        self._faults["reset_peer"] += 1
        for conn in list(self._conns.values()):
            self._reset_conn(conn)

    async def _set_blackhole(self, enabled: bool) -> None:
        if enabled == self._blackholed:
            return
        self._faults["blackhole" if enabled else "blackhole_off"] += 1
        self._blackholed = enabled
        for conn in list(self._conns.values()):
            if enabled:
                conn.poisoned = True
            elif conn.poisoned:
                self._reset_conn(conn)

    async def _set_latency(self, latency_s: float, jitter_s: float) -> None:
        self._faults["latency"] += 1
        self._latency_s, self._jitter_s = latency_s, jitter_s

    async def _set_bandwidth(self, bytes_per_s: float | None) -> None:
        self._faults["bandwidth"] += 1
        self._bytes_per_s = bytes_per_s

    async def _retarget(self, upstream: tuple[str, int]) -> None:
        self._faults["retarget"] += 1
        self._upstream = upstream

    async def _clear(self) -> None:
        await self._set_blackhole(False)
        self._latency_s = self._jitter_s = 0.0
        self._bytes_per_s = None
        await self._up()

    async def _close(self) -> None:
        await self._stop_listening()
        for conn in list(self._conns.values()):
            self._close_conn(conn)
        tasks = [t for c in self._conns.values() for t in c.tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._conns.clear()

    # ------------------------------------------------------- connections

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = _Conn(next(self._ids), writer)
        self._total += 1
        self._conns[conn.cid] = conn
        if self._blackholed:
            # A SYN into a pulled cable is never answered: no upstream at all.
            conn.poisoned = True
            me = asyncio.current_task()
            if me is not None:
                conn.tasks.append(me)
            await self._drain_into_void(reader)
            return
        host, port = self._upstream
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), UPSTREAM_CONNECT_TIMEOUT_S
            )
        except (OSError, TimeoutError):
            self._upstream_failures += 1
            self._reset_conn(conn)
            return
        if conn.closed:  # reset/down while we were connecting
            _rst(up_writer)
            return
        conn.upstream = up_writer
        pumps = [
            self._pump(conn, reader, up_writer, "up"),
            self._pump(conn, up_reader, writer, "down"),
        ]
        conn.tasks = [asyncio.create_task(p) for p in pumps]
        await asyncio.gather(*conn.tasks, return_exceptions=True)
        self._close_conn(conn)

    async def _drain_into_void(self, reader: asyncio.StreamReader) -> None:
        with contextlib.suppress(OSError, asyncio.CancelledError):
            while data := await reader.read(CHUNK_BYTES):
                self._dropped += len(data)
        # EOF from the client is not forwarded anywhere (there is no upstream).

    def _reset_conn(self, conn: _Conn) -> None:
        if conn.closed:
            return
        conn.closed = True
        _rst(conn.client)
        _rst(conn.upstream)
        for task in conn.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        self._conns.pop(conn.cid, None)

    def _close_conn(self, conn: _Conn) -> None:
        if conn.closed:
            return
        conn.closed = True
        for writer in (conn.client, conn.upstream):
            if writer is not None:
                writer.close()
        self._conns.pop(conn.cid, None)

    async def _pump(
        self,
        conn: _Conn,
        src: asyncio.StreamReader,
        dst: asyncio.StreamWriter,
        direction: str,
    ) -> None:
        """Copy ``src`` -> ``dst`` through the link's toxics until EOF or error."""
        queue: asyncio.Queue[tuple[float, bytes | None]] = asyncio.Queue(
            MAX_QUEUED_CHUNKS
        )
        deliver = asyncio.create_task(self._deliver(conn, queue, dst, direction))
        loop = asyncio.get_running_loop()
        last_due = 0.0
        try:
            while True:
                try:
                    data = await src.read(CHUNK_BYTES)
                except OSError:
                    # The peer reset this side: tell the other side the same way,
                    # unless the link is pretending the cable is gone.
                    if not conn.poisoned:
                        self._reset_conn(conn)
                    return
                if conn.poisoned:
                    self._dropped += len(data)
                    if not data:
                        # A FIN into a blackhole is never forwarded; keep the other
                        # side open until a restore resets it.
                        await asyncio.Event().wait()
                    continue
                due = loop.time() + self._latency_s
                if self._jitter_s:
                    due += self._rng.uniform(0.0, self._jitter_s)
                last_due = max(last_due, due)  # never reorder
                await queue.put((last_due, data or None))
                if not data:
                    break
            await deliver
        finally:
            if not deliver.done():
                deliver.cancel()
                await asyncio.gather(deliver, return_exceptions=True)

    async def _deliver(
        self,
        conn: _Conn,
        queue: asyncio.Queue[tuple[float, bytes | None]],
        dst: asyncio.StreamWriter,
        direction: str,
    ) -> None:
        loop = asyncio.get_running_loop()
        next_free = 0.0
        while True:
            due, data = await queue.get()
            delay = due - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if conn.poisoned:
                self._dropped += len(data or b"")
                continue
            if data is None:
                with contextlib.suppress(OSError, RuntimeError):
                    if dst.can_write_eof():
                        dst.write_eof()
                return
            for piece in self._paced(data):
                if self._bytes_per_s:
                    # The last byte of a piece leaves when a link of this rate
                    # would have finished sending it.
                    now = loop.time()
                    next_free = max(now, next_free) + len(piece) / self._bytes_per_s
                    await asyncio.sleep(next_free - now)
                if conn.is_poisoned():  # may have changed during the sleep
                    self._dropped += len(piece)
                    continue
                try:
                    dst.write(piece)
                    await dst.drain()
                except OSError:
                    if not conn.poisoned:
                        self._reset_conn(conn)
                    return
                self._bytes[direction] += len(piece)

    def _paced(self, data: bytes) -> list[bytes]:
        """Split ``data`` into ~50 ms slices when a bandwidth limit applies."""
        if not self._bytes_per_s:
            return [data]
        size = max(1, int(self._bytes_per_s * PACING_SLICE_S))
        return [data[i : i + size] for i in range(0, len(data), size)]
