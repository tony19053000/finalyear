"""FaultLink semantics on raw sockets (V0.3 G6-B, D23).

The cluster scenarios trust these faults to mean what their names say; a proxy
that, say, forwarded a FIN during ``blackhole`` would make the cable-pull scenario
test a clean close instead. Each fault is therefore proved here against plain
``socket`` peers, with no gRPC in between.

Timing margins: "nothing arrives" is checked over a ``QUIET_S`` window. Loopback
delivery through the proxy takes well under 10 ms on an idle machine, so 0.3 s is
>30x that and still cheap; a false pass would need the proxy to *hold* bytes for
that long, which only a bug in the opposite direction (buffering instead of
dropping) could do - and the restore test then catches it, because nothing held
may arrive after the restore either.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from mf_faultproxy import FaultLink, FaultLoop

QUIET_S = 0.3
IO_TIMEOUT_S = 5.0


class Upstream:
    """A threaded TCP server: records every accepted socket; optional echo."""

    def __init__(self, *, echo: bool = False) -> None:
        self.echo = echo
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.1)
        self.port: int = self._listener.getsockname()[1]
        self.accepted: list[socket.socket] = []
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def address(self) -> tuple[str, int]:
        return ("127.0.0.1", self.port)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._cond:
                self.accepted.append(conn)
                self._cond.notify_all()
            if self.echo:
                t = threading.Thread(target=self._echo, args=(conn,), daemon=True)
                t.start()
                self._threads.append(t)

    def _echo(self, conn: socket.socket) -> None:
        with contextlib.suppress(OSError):
            while data := conn.recv(65536):
                conn.sendall(data)

    def wait_accepted(self, n: int, timeout_s: float = IO_TIMEOUT_S) -> socket.socket:
        with self._cond:
            if not self._cond.wait_for(lambda: len(self.accepted) >= n, timeout_s):
                raise AssertionError(f"upstream accepted {len(self.accepted)} < {n}")
            conn = self.accepted[n - 1]
        conn.settimeout(IO_TIMEOUT_S)
        return conn

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(2)
        for conn in self.accepted:
            conn.close()
        for t in self._threads:
            t.join(2)


@pytest.fixture
def fault_loop() -> Iterator[FaultLoop]:
    with FaultLoop("faultproxy-test") as loop:
        yield loop


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    server = Upstream()
    yield server
    server.close()


def _connect(link: FaultLink) -> socket.socket:
    sock = socket.create_connection(link.address, timeout=IO_TIMEOUT_S)
    sock.settimeout(IO_TIMEOUT_S)
    return sock


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise AssertionError(f"EOF after {len(buf)} of {n} bytes")
        buf += chunk
    return buf


def _wait_for(predicate: Callable[[], bool], timeout_s: float = IO_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        time.sleep(0.01)


def _assert_quiet(sock: socket.socket) -> None:
    """Nothing arrives within QUIET_S: no data, no FIN (b""), no RST."""
    sock.settimeout(QUIET_S)
    try:
        data = sock.recv(1024)
    except TimeoutError:
        return
    finally:
        sock.settimeout(IO_TIMEOUT_S)
    raise AssertionError(f"expected silence, got {data!r} (b'' = FIN)")


def _assert_reset(sock: socket.socket) -> None:
    """The peer reset the connection: recv raises ECONNRESET (never a clean EOF)."""
    # A RST may arrive after data already queued; drain until it surfaces.
    deadline = time.monotonic() + IO_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            data = sock.recv(65536)
        except ConnectionResetError:
            return
        if not data:
            raise AssertionError("clean EOF (FIN) where a RST was expected")
    raise AssertionError("no RST within the timeout")


def test_passthrough_both_ways_and_counters(
    fault_loop: FaultLoop, upstream: Upstream
) -> None:
    link = fault_loop.link(upstream.address, name="pass")
    with _connect(link) as client:
        server = upstream.wait_accepted(1)
        client.sendall(b"ping")
        assert _recv_exact(server, 4) == b"ping"
        server.sendall(b"pong!")
        assert _recv_exact(client, 5) == b"pong!"
        counters = link.counters()
    assert counters.connections_total == 1
    assert counters.bytes_up == 4
    assert counters.bytes_down == 5
    assert counters.bytes_dropped == 0
    assert counters.faults == {}


def test_half_close_is_forwarded_when_healthy(
    fault_loop: FaultLoop, upstream: Upstream
) -> None:
    """The contrast for blackhole: a normal FIN *is* forwarded."""
    link = fault_loop.link(upstream.address)
    with _connect(link) as client:
        server = upstream.wait_accepted(1)
        client.shutdown(socket.SHUT_WR)
        assert server.recv(10) == b""
        server.sendall(b"still-open")
        assert _recv_exact(client, 10) == b"still-open"


def test_blackhole_delivers_nothing_and_sends_no_fin_or_rst(
    fault_loop: FaultLoop, upstream: Upstream
) -> None:
    link = fault_loop.link(upstream.address)
    client = _connect(link)
    server = upstream.wait_accepted(1)
    client.sendall(b"a")
    assert _recv_exact(server, 1) == b"a"

    link.blackhole()
    client.sendall(b"lost-up")
    server.sendall(b"lost-down")
    _assert_quiet(server)
    _assert_quiet(client)
    # Either side closing is not told to the other: no FIN, no RST.
    server.shutdown(socket.SHUT_WR)
    _assert_quiet(client)
    client.shutdown(socket.SHUT_WR)
    _assert_quiet(server)

    # New connections are accepted but never reach the upstream.
    late = _connect(link)
    late.sendall(b"into-the-void")
    _assert_quiet(late)
    assert len(upstream.accepted) == 1

    counters = link.counters()
    assert counters.bytes_dropped >= len(b"lost-up") + len(b"lost-down")
    assert counters.faults == {"blackhole": 1}
    client.close()
    server.close()
    late.close()


def test_blackhole_restore_resets_poisoned_and_new_connections_work(
    fault_loop: FaultLoop, upstream: Upstream
) -> None:
    link = fault_loop.link(upstream.address)
    client = _connect(link)
    server = upstream.wait_accepted(1)
    link.blackhole()
    client.sendall(b"dropped")
    late = _connect(link)  # accepted during the blackhole, never connected upstream
    _wait_for(lambda: link.counters().connections_total == 2)

    link.blackhole(False)
    _assert_reset(client)
    _assert_reset(server)
    _assert_reset(late)

    with _connect(link) as fresh:
        server2 = upstream.wait_accepted(2)
        fresh.sendall(b"ok")
        # Only the fresh bytes arrive: what was dropped stays dropped.
        assert _recv_exact(server2, 2) == b"ok"
    assert link.counters().faults == {"blackhole": 1, "blackhole_off": 1}
    for sock in (client, server, late):
        sock.close()


def test_reset_peer_resets_both_sides_and_keeps_accepting(
    fault_loop: FaultLoop, upstream: Upstream
) -> None:
    link = fault_loop.link(upstream.address)
    client = _connect(link)
    server = upstream.wait_accepted(1)
    client.sendall(b"x")
    assert _recv_exact(server, 1) == b"x"

    link.reset_peer()
    _assert_reset(client)
    _assert_reset(server)

    with _connect(link) as again:
        server2 = upstream.wait_accepted(2)
        again.sendall(b"y")
        assert _recv_exact(server2, 1) == b"y"
    assert link.counters().faults == {"reset_peer": 1}
    client.close()
    server.close()


def test_down_refuses_new_connections_resets_open_ones_and_up_restores(
    fault_loop: FaultLoop, upstream: Upstream
) -> None:
    link = fault_loop.link(upstream.address)
    port = link.address[1]
    client = _connect(link)
    server = upstream.wait_accepted(1)

    link.down()
    _assert_reset(client)
    _assert_reset(server)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(link.address, timeout=IO_TIMEOUT_S)

    link.up()
    assert link.address[1] == port
    with _connect(link) as again:
        server2 = upstream.wait_accepted(2)
        again.sendall(b"z")
        assert _recv_exact(server2, 1) == b"z"
    client.close()
    server.close()


def test_latency_within_tolerance_and_order_preserved_under_jitter(
    fault_loop: FaultLoop,
) -> None:
    echo = Upstream(echo=True)
    try:
        link = fault_loop.link(echo.address)
        with _connect(link) as client:
            client.sendall(b"w")  # warm-up: connection + echo thread are up
            _recv_exact(client, 1)
            link.latency(100, jitter_ms=0)
            start = time.monotonic()
            client.sendall(b"r")
            _recv_exact(client, 1)
            rtt = time.monotonic() - start
            # One-way 100 ms each way -> >= 200 ms. Upper margin 150 ms: thread
            # scheduling of the echo server and the proxy loop on a loaded box.
            assert 0.2 <= rtt <= 0.35, rtt

            link.latency(20, jitter_ms=30)
            payload = [f"<{i:03d}>".encode() for i in range(60)]
            for part in payload:
                client.sendall(part)
                time.sleep(0.002)  # separate reads -> separate jittered chunks
            got = _recv_exact(client, sum(len(p) for p in payload))
            assert got == b"".join(payload), "jitter reordered the byte stream"
            link.latency(0)
            start = time.monotonic()
            client.sendall(b"q")
            _recv_exact(client, 1)
            assert time.monotonic() - start < 0.1
    finally:
        echo.close()


def test_bandwidth_paces_each_direction(
    fault_loop: FaultLoop, upstream: Upstream
) -> None:
    link = fault_loop.link(upstream.address)
    with _connect(link) as client:
        server = upstream.wait_accepted(1)
        link.bandwidth(100_000)
        blob = b"b" * 50_000
        start = time.monotonic()
        client.sendall(blob)
        assert _recv_exact(server, len(blob)) == blob
        elapsed = time.monotonic() - start
        # 50 KB at 100 KB/s = 0.5 s. Lower margin 0.05 s (one pacing slice may
        # straddle the start); upper margin generous for a loaded machine.
        assert 0.45 <= elapsed <= 2.0, elapsed
        link.bandwidth(None)
        start = time.monotonic()
        client.sendall(blob)
        _recv_exact(server, len(blob))
        assert time.monotonic() - start < 0.2


def test_retarget_affects_only_new_connections(fault_loop: FaultLoop) -> None:
    first, second = Upstream(), Upstream()
    try:
        link = fault_loop.link(first.address)
        old = _connect(link)
        old_server = first.wait_accepted(1)
        link.retarget(*second.address)
        assert link.upstream == second.address

        old.sendall(b"old")
        assert _recv_exact(old_server, 3) == b"old"
        with _connect(link) as new:
            new_server = second.wait_accepted(1)
            new.sendall(b"new")
            assert _recv_exact(new_server, 3) == b"new"
        assert len(first.accepted) == 1
        old.close()
    finally:
        first.close()
        second.close()


def test_upstream_gone_resets_the_client_promptly(fault_loop: FaultLoop) -> None:
    dead = socket.create_server(("127.0.0.1", 0))
    port = dead.getsockname()[1]
    dead.close()  # nothing listens here any more
    link = fault_loop.link(("127.0.0.1", port))
    with _connect(link) as client:
        start = time.monotonic()
        _assert_reset(client)
        assert time.monotonic() - start < 1.0
    assert link.counters().upstream_failures == 1


def test_buffers_are_bounded_under_a_stalled_link(
    fault_loop: FaultLoop, upstream: Upstream
) -> None:
    """A paced link pushes back on the sender instead of buffering everything."""
    link = fault_loop.link(upstream.address)
    with _connect(link) as client:
        upstream.wait_accepted(1)
        link.bandwidth(1_000)  # ~stalled
        client.setblocking(False)
        sent = 0
        deadline = time.monotonic() + IO_TIMEOUT_S
        chunk = b"s" * 65536
        while time.monotonic() < deadline:
            try:
                sent += client.send(chunk)
            except BlockingIOError:
                time.sleep(0.05)
                try:
                    sent += client.send(chunk)
                except BlockingIOError:
                    break
        else:
            raise AssertionError(f"sender never blocked after {sent} bytes")
        # Proxy queue (16 x 64 KiB) + kernel socket buffers on both hops; the
        # point is that it is finite, not its exact size.
        assert sent < 64 * 1024 * 1024
        link.bandwidth(None)


def test_close_releases_every_socket_and_the_thread(upstream: Upstream) -> None:
    loop = FaultLoop("faultproxy-close")
    link = loop.link(upstream.address)
    client = _connect(link)
    server = upstream.wait_accepted(1)
    loop.close()
    loop.close()  # idempotent
    assert not loop._thread.is_alive()
    # Clean close (FIN), not a reset, and the listener is gone.
    assert client.recv(10) == b""
    assert server.recv(10) == b""
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(link.address, timeout=IO_TIMEOUT_S)
    with pytest.raises(RuntimeError):
        link.reset_peer()
    client.close()
