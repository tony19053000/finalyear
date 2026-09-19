"""S1 server admission control: header deadline and pending-connection eviction.

Real sockets against an in-process :class:`S1Server` (DEFECT-1 of 0.2.5 round 1):
peers that never finish their header must not hold measurement slots, and a
running measurement must never be evicted to make room.
"""

from __future__ import annotations

import contextlib
import socket
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._s1.client import Endpoint, hello  # noqa: E402
from spikes._s1.protocol import PROTOCOL, send_msg  # noqa: E402
from spikes._s1.server import S1Server, ServerLimits  # noqa: E402

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
def small_server() -> Iterator[S1Server]:
    srv = S1Server(
        "127.0.0.1",
        0,
        ServerLimits(
            idle_s=60, io_timeout_s=10, header_timeout_s=1.0, max_connections=4
        ),
        name="small",
    )
    thread = srv.start_thread()
    yield srv
    srv.stop()
    thread.join(timeout=10)
    assert not thread.is_alive(), "server thread leaked"


def _connect(port: int) -> socket.socket:
    return socket.create_connection(("127.0.0.1", port), timeout=3)


def test_trickled_header_is_cut_at_the_header_deadline(small_server: S1Server) -> None:
    """One byte every 0.3 s would keep a per-recv timeout alive forever; the
    whole-header deadline must still drop the connection after ~1 s."""
    s = _connect(small_server.port)
    started = time.monotonic()
    closed_after: float | None = None
    try:
        s.settimeout(0.3)
        s.sendall((64).to_bytes(4, "big"))  # valid length; the body then trickles
        for _ in range(30):
            try:
                s.sendall(b"{")
                if s.recv(1) == b"":
                    closed_after = time.monotonic() - started
                    break
            except TimeoutError:
                continue
            except OSError:
                closed_after = time.monotonic() - started
                break
    finally:
        s.close()
    assert closed_after is not None, "server never dropped the trickling peer"
    assert closed_after < 3.0
    assert small_server.counters.tcp_header_timeouts >= 1


def test_active_measurement_survives_a_flood_of_idle_peers(
    small_server: S1Server,
) -> None:
    upload = _connect(small_server.port)
    lazy: list[socket.socket] = []
    try:
        send_msg(
            upload, {"protocol": PROTOCOL, "op": "upload", "test_id": "t1", "stream": 0}
        )
        upload.sendall(b"x" * 1024)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and small_server._pending:
            time.sleep(0.02)
        assert not small_server._pending, "upload never became active"

        for _ in range(12):
            lazy.append(_connect(small_server.port))
            time.sleep(0.02)  # let the accept loop run in order

        # the cap holds and the measurement was never the one evicted
        assert small_server.open_connections <= small_server.limits.max_connections
        assert small_server.counters.tcp_evicted_pending >= 12 - 3
        upload.sendall(b"y" * 1024)  # still connected
        ep = Endpoint("127.0.0.1", small_server.port)
        assert hello(ep)["ok"] is True
    finally:
        for s in lazy:
            with contextlib.suppress(OSError):
                s.close()
        upload.shutdown(socket.SHUT_WR)
        upload.close()


def test_all_active_means_new_peers_are_refused(small_server: S1Server) -> None:
    uploads: list[socket.socket] = []
    try:
        for i in range(small_server.limits.max_connections):
            s = _connect(small_server.port)
            send_msg(
                s,
                {"protocol": PROTOCOL, "op": "upload", "test_id": f"t{i}", "stream": 0},
            )
            uploads.append(s)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and (
            small_server._pending
            or small_server.open_connections < small_server.limits.max_connections
        ):
            time.sleep(0.02)
        before = small_server.counters.tcp_refused_busy
        extra = _connect(small_server.port)
        extra.settimeout(3)
        with contextlib.suppress(OSError):
            assert extra.recv(1) == b""  # closed at once, nothing evicted
        extra.close()
        assert small_server.counters.tcp_refused_busy == before + 1
        assert small_server.counters.tcp_evicted_pending == 0
    finally:
        for s in uploads:
            with contextlib.suppress(OSError):
                s.close()
