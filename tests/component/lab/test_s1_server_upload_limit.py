"""Component test (0.2.5, reviewer round 3 N3): an ``upload`` that never
half-closes is cut off ``max_upload_s`` after its first byte, recorded with
``error = "upload exceeded limit"``, and the server keeps answering.

Real sockets against an in-process :class:`S1Server`; no mocks.
"""

from __future__ import annotations

import contextlib
import os
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

from spikes._s1.client import Endpoint, hello, request  # noqa: E402
from spikes._s1.protocol import MAX_SECONDS, PROTOCOL, send_msg  # noqa: E402
from spikes._s1.server import UPLOAD_LIMIT_ERROR, S1Server, ServerLimits  # noqa: E402

pytestmark = pytest.mark.timeout(60)

LIMIT_S: Final = 0.5


@pytest.fixture
def server() -> Iterator[S1Server]:
    srv = S1Server(
        "127.0.0.1",
        0,
        ServerLimits(idle_s=60, io_timeout_s=2, max_upload_s=LIMIT_S),
        name="limited",
    )
    thread = srv.start_thread()
    yield srv
    srv.stop()
    thread.join(timeout=10)
    assert not thread.is_alive(), "server thread leaked"


def test_default_upload_limit_is_max_seconds_plus_io_timeout() -> None:
    assert ServerLimits(io_timeout_s=7).upload_limit_s() == MAX_SECONDS + 7
    assert ServerLimits(max_upload_s=0.25).upload_limit_s() == 0.25


def test_endless_upload_is_cut_off_and_recorded(server: S1Server) -> None:
    payload = os.urandom(64 * 1024)
    ep = Endpoint("127.0.0.1", server.port, connect_timeout_s=3, io_timeout_s=3)
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=3)
    sock.settimeout(0.2)
    sent = 0
    started = time.monotonic()
    closed_at: float | None = None
    try:
        send_msg(
            sock, {"protocol": PROTOCOL, "op": "upload", "test_id": "t1", "stream": 0}
        )
        # never half-close: keep pushing until the server drops us (or 5x limit)
        while time.monotonic() - started < LIMIT_S * 10:
            try:
                sent += sock.send(payload)
            except TimeoutError:
                continue  # send buffer full; the server is still draining
            except OSError:
                closed_at = time.monotonic()
                break
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    assert sent > 0
    assert closed_at is not None, "server never closed the endless upload"
    # generous bound: limit + io_timeout + scheduling slack, far below MAX_SECONDS
    assert closed_at - started < LIMIT_S + 2 + 2

    reply = request(ep, {"op": "result", "test_id": "t1", "streams": 1})
    (stats,) = reply["streams"]
    assert stats["error"] == UPLOAD_LIMIT_ERROR
    assert stats["bytes"] > 0
    assert stats["t_last"] - stats["t_first"] >= LIMIT_S
    assert stats["t_last"] - stats["t_first"] < LIMIT_S + 2
    assert server.open_connections == 0
    assert hello(ep)["name"] == "limited"


def test_normal_upload_below_limit_has_no_error(server: S1Server) -> None:
    ep = Endpoint("127.0.0.1", server.port, connect_timeout_s=3, io_timeout_s=3)
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=3)
    try:
        send_msg(
            sock, {"protocol": PROTOCOL, "op": "upload", "test_id": "t2", "stream": 0}
        )
        sock.sendall(b"x" * 100_000)
        sock.shutdown(socket.SHUT_WR)
        while sock.recv(4096):
            pass
    finally:
        sock.close()
    reply = request(ep, {"op": "result", "test_id": "t2", "streams": 1})
    assert reply["streams"][0]["error"] is None
    assert reply["streams"][0]["bytes"] == 100_000
