"""Tester-added adversarial tests for the S1 server (0.2.5): connection floods,
slowloris, UDP floods, and random-byte fuzzing against a *running* server.

Real sockets, in-process :class:`S1Server` (no mocks). The point is to prove
the server stays responsive, bounded in resource use, and never crashes when
handed malformed, oversized, or merely uncooperative input -- see
docs/LAB.md §6 and CLAUDE.md §9 (never unauthenticated arbitrary code
execution / must be robust to a hostile LAN).
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._s1.client import Endpoint, hello, udp_rtt  # noqa: E402
from spikes._s1.protocol import MAGIC, MAX_HEADER_BYTES, MeasureError  # noqa: E402
from spikes._s1.server import S1Server, ServerLimits  # noqa: E402

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def server() -> Iterator[S1Server]:
    srv = S1Server(
        "127.0.0.1", 0, ServerLimits(idle_s=60, io_timeout_s=2), name="victim"
    )
    thread = srv.start_thread()
    yield srv
    srv.stop()
    thread.join(timeout=10)
    assert not thread.is_alive(), "server thread leaked"


def _still_answers(port: int) -> bool:
    try:
        return bool(
            hello(Endpoint("127.0.0.1", port, connect_timeout_s=3, io_timeout_s=3))[
                "ok"
            ]
        )
    except MeasureError:
        return False


# ------------------------------------------------------------- connection cap
def test_connection_flood_is_capped_and_server_stays_responsive(
    server: S1Server,
) -> None:
    """Opening far more TCP connections than ``max_connections`` (default 32)
    must not crash the server, grow its state unboundedly, or wedge it: extra
    connections are refused, and a normal client can still be served after."""
    n = 200
    socks: list[socket.socket] = []
    try:
        for _ in range(n):
            with contextlib.suppress(OSError):
                s = socket.create_connection(("127.0.0.1", server.port), timeout=3)
                s.settimeout(1)
                socks.append(s)

        # Every connection the server's accept() returns ends up in exactly one
        # bucket: refused for not being a LAN peer, refused because every slot
        # was held by an *active* measurement, or accepted as pending. Evicting
        # an older pending connection to make room bumps both tcp_refused_busy
        # and tcp_accepted for the same accept(), so the evicted subset is
        # subtracted to keep the sum one-per-accept.
        #
        # A fixed margin on tcp_refused_busy alone (the previous assertion) is
        # wrong by design: evict-oldest-pending lets *most* accepts succeed, so
        # the refused count depends on scheduling, not on a bound this test can
        # predict (observed 157-162 for n=200 with a required >= 163).
        def accounted() -> int:
            c = server.counters
            return (
                c.tcp_accepted
                + (c.tcp_refused_busy - c.tcp_evicted_pending)
                + c.tcp_refused_peer
            )

        # Tolerance: the listen backlog (64). Linux may complete a handshake
        # (so create_connection succeeds here) while the accept queue is full
        # and never hand that connection to accept(); such sockets are stuck in
        # the kernel, not lost by the server.
        backlog = 64
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and accounted() < len(socks) - backlog:
            time.sleep(0.05)
        # bounded: never more live connections tracked than the configured cap
        assert server.open_connections <= server.limits.max_connections
        assert accounted() >= len(socks) - backlog, (
            f"{len(socks)} established connections, only {accounted()} accounted "
            f"for by the counters: {server.counters}"
        )
        # counters stay internally consistent: evictions are a subset of the
        # busy refusals, nothing is negative, and no accept is double-counted.
        counters = server.counters
        assert 0 <= counters.tcp_evicted_pending <= counters.tcp_refused_busy
        assert accounted() <= n  # never more accepts than connections attempted
        assert counters.tcp_accepted >= 1
    finally:
        for s in socks:
            with contextlib.suppress(OSError):
                s.close()
    assert _still_answers(server.port)


def test_slowloris_connections_time_out_and_do_not_wedge_server(
    server: S1Server,
) -> None:
    """Connections that complete the TCP handshake and then send nothing must be
    dropped by the server's own I/O timeout, not held open forever, and must
    not prevent a normal client being served concurrently."""
    lazy: list[socket.socket] = []
    for _ in range(server.limits.max_connections):
        s = socket.create_connection(("127.0.0.1", server.port), timeout=3)
        lazy.append(s)  # connected, never sends a byte

    try:
        # A normal client must still get served promptly even while every
        # slot may be filled with slow/hanging peers.
        started = time.monotonic()
        assert _still_answers(server.port)
        assert time.monotonic() - started < 5

        # Eventually the server's io_timeout reclaims the slow connections.
        deadline = time.monotonic() + server.limits.io_timeout_s + 10
        while time.monotonic() < deadline and server.open_connections > 0:
            time.sleep(0.1)
        assert server.open_connections == 0, (
            "slowloris connections were never reclaimed"
        )
    finally:
        for s in lazy:
            with contextlib.suppress(OSError):
                s.close()


# ------------------------------------------------------------------- fuzzing
_FUZZ_PAYLOADS: list[bytes] = [
    b"",
    b"\x00",
    (MAX_HEADER_BYTES + 1000).to_bytes(4, "big"),  # header longer than allowed
    (0xFFFFFFFF).to_bytes(4, "big"),  # max possible u32 length
    os.urandom(64),
    os.urandom(4096),
    b"\x00\x00\x10\x00" + b"{" * 4096,  # claims 4096-byte body, sends garbage
    b"\x00\x00\x00\x08" + b'{"a":1}',  # length off by one (short body)
]


@pytest.mark.parametrize("payload", _FUZZ_PAYLOADS, ids=range(len(_FUZZ_PAYLOADS)))
def test_random_and_malformed_tcp_payloads_never_crash_server(
    server: S1Server, payload: bytes
) -> None:
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as s:
        s.settimeout(3)
        with contextlib.suppress(OSError):
            s.sendall(payload)
        with contextlib.suppress(OSError):
            s.shutdown(socket.SHUT_WR)
        with contextlib.suppress(OSError):
            s.recv(4096)
    assert _still_answers(server.port)


def test_many_parallel_fuzzers_do_not_crash_or_wedge_server(server: S1Server) -> None:
    """A burst of concurrent malformed connections (not one at a time)."""

    def attack() -> None:
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", server.port), timeout=3) as s,
        ):
            s.settimeout(2)
            s.sendall(os.urandom(2000))

    threads = [threading.Thread(target=attack) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert _still_answers(server.port)


# ------------------------------------------------------------------- UDP flood
def test_udp_flood_of_garbage_is_dropped_not_amplified(server: S1Server) -> None:
    before = server.counters.udp_dropped
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(0.5)
        # Bursts of garbage, including oversized datagrams (truncated/rejected
        # by the receive-buffer bound) and well-formed-looking-but-wrong magic.
        for _ in range(500):
            s.sendto(os.urandom(32), ("127.0.0.1", server.port))
        for _ in range(20):
            s.sendto(os.urandom(4000), ("127.0.0.1", server.port))  # oversized
        for _ in range(20):
            s.sendto(MAGIC + os.urandom(4), ("127.0.0.1", server.port))  # too short
        with pytest.raises(TimeoutError):
            s.recvfrom(2048)  # none of the above were ever echoed back
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and server.counters.udp_dropped < before + 500:
        time.sleep(0.05)
    assert server.counters.udp_dropped >= before + 500
    # the server is still alive and still echoes legitimate pings afterwards
    sample = udp_rtt(Endpoint("127.0.0.1", server.port), count=20, interval_s=0.005)
    assert sample["received"] > 0


def test_udp_flood_does_not_starve_valid_pings(server: S1Server) -> None:
    """A concurrent flood of garbage UDP must not prevent legitimate echoes."""
    stop = threading.Event()

    def flood() -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            while not stop.is_set():
                with contextlib.suppress(OSError):
                    s.sendto(os.urandom(200), ("127.0.0.1", server.port))

    threads = [threading.Thread(target=flood) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        sample = udp_rtt(Endpoint("127.0.0.1", server.port), count=50, interval_s=0.005)
        assert sample["received"] > 0
        assert sample["loss_pct"] < 100.0
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
