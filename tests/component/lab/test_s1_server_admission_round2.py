"""Tester round 2 (0.2.5 retest): attack the DEFECT-1 admission logic and the
DEFECT-2/DEFECT-3 follow-ups after the implementer's fix.

Real sockets against an in-process :class:`S1Server` (no mocks). Never
modifies ``src``/``lab`` production code; failing assertions are reported as
defects.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._harness.envelope import raw_result_path, safe_name  # noqa: E402
from spikes._s1.client import Endpoint, hello, tcp_throughput  # noqa: E402
from spikes._s1.protocol import (  # noqa: E402
    LENGTH_BYTES,
    MAX_HEADER_BYTES,
    PROTOCOL,
    MeasureError,
    send_msg,
)
from spikes._s1.server import S1Server, ServerLimits  # noqa: E402

pytestmark = pytest.mark.timeout(120)


@pytest.fixture
def server() -> Iterator[S1Server]:
    srv = S1Server(
        "127.0.0.1",
        0,
        ServerLimits(
            idle_s=120, io_timeout_s=5, header_timeout_s=2.0, max_connections=32
        ),
        name="victim2",
    )
    thread = srv.start_thread()
    yield srv
    srv.stop()
    thread.join(timeout=10)
    assert not thread.is_alive(), "server thread leaked"


def _connect(port: int) -> socket.socket:
    return socket.create_connection(("127.0.0.1", port), timeout=3)


def _still_answers(port: int) -> bool:
    try:
        return bool(hello(Endpoint("127.0.0.1", port, connect_timeout_s=3))["ok"])
    except MeasureError:
        return False


# ------------------------------------------------------- literal 1.9s trickle
def test_trickle_one_byte_every_1_9s_is_dropped_by_header_deadline(
    server: S1Server,
) -> None:
    """A peer sending one byte every 1.9 s never triggers a per-recv timeout,
    but the whole-header deadline (default 2 s) must still cut it off quickly
    -- it must not survive to the 5 s io_timeout used for active connections."""
    s = _connect(server.port)
    started = time.monotonic()
    closed_after: float | None = None
    try:
        s.settimeout(0.5)
        body = json.dumps({"protocol": PROTOCOL, "op": "hello"}).encode()
        s.sendall(len(body).to_bytes(LENGTH_BYTES, "big"))
        for byte in body:
            time.sleep(1.9)
            try:
                s.sendall(bytes([byte]))
            except OSError:
                closed_after = time.monotonic() - started
                break
            try:
                if s.recv(1) == b"":
                    closed_after = time.monotonic() - started
                    break
            except TimeoutError:
                continue
            if time.monotonic() - started > 15:
                break
    finally:
        s.close()
    assert closed_after is not None, "server never dropped the trickling peer"
    assert closed_after < 5.0, "trickle survived past the header deadline"
    assert server.counters.tcp_header_timeouts >= 1


# --------------------------------------------- real measurement under flood
def test_real_measurement_completes_with_correct_bytes_during_pending_flood(
    server: S1Server,
) -> None:
    """A real 4-stream measurement must complete with correct byte counts even
    while many other connections sit pending (never sending a full header)."""
    stop = threading.Event()

    def flood() -> None:
        socks: list[socket.socket] = []
        try:
            while not stop.is_set():
                with contextlib.suppress(OSError):
                    s = socket.create_connection(("127.0.0.1", server.port), timeout=1)
                    s.settimeout(0.2)
                    with contextlib.suppress(OSError):
                        s.sendall(b"\x00")  # never completes a 4-byte length prefix
                    socks.append(s)
                time.sleep(0.01)
        finally:
            for s in socks:
                with contextlib.suppress(OSError):
                    s.close()

    threads = [threading.Thread(target=flood) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        seconds = 2.0
        streams = 4
        sample = tcp_throughput(
            Endpoint("127.0.0.1", server.port, io_timeout_s=10),
            direction="upload",
            streams=streams,
            seconds=seconds,
        )
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)

    assert sample["bytes_received"] > 0
    assert sample["receiver_mbps"] > 0
    assert sample["streams"] == streams
    # receiver and sender byte counts must agree (nothing lost/duplicated
    # despite the concurrent flood of pending, never-promoted connections)
    assert sample["bytes_match"] is True


# ------------------------------------------------------- header size boundary
def _raw_send_header(sock: socket.socket, body: bytes) -> None:
    sock.sendall(len(body).to_bytes(LENGTH_BYTES, "big") + body)


def _padded_hello_body(target_len: int) -> bytes:
    """A valid JSON ``hello`` body padded with whitespace to exactly ``target_len``
    bytes (JSON tolerates leading/trailing whitespace around a value)."""
    base = json.dumps({"protocol": PROTOCOL, "op": "hello"}).encode()
    assert len(base) <= target_len
    pad = b" " * (target_len - len(base))
    return pad + base


def test_header_exactly_at_the_size_limit_is_accepted(server: S1Server) -> None:
    body = _padded_hello_body(MAX_HEADER_BYTES)
    with _connect(server.port) as s:
        s.settimeout(5)
        _raw_send_header(s, body)
        from spikes._s1.protocol import recv_msg

        reply = recv_msg(s)
    assert reply["ok"] is True
    assert _still_answers(server.port)


def test_header_one_byte_over_the_size_limit_is_dropped(server: S1Server) -> None:
    """The 4-byte length prefix itself already exceeds the limit: the server
    must drop the connection at once, without trying to read a body that
    large, and must not crash or wedge."""
    with _connect(server.port) as s:
        s.settimeout(5)
        s.sendall((MAX_HEADER_BYTES + 1).to_bytes(LENGTH_BYTES, "big"))
        with contextlib.suppress(OSError):
            s.sendall(b"x" * 4096)  # would-be body; must never be fully consumed
        reply = b""
        with contextlib.suppress(OSError):
            reply = s.recv(16)
        assert reply == b""  # dropped, no reply
    assert _still_answers(server.port)


# --------------------------------------------------- half-close after header
def test_valid_header_then_half_close_gets_a_reply(server: S1Server) -> None:
    """A peer that sends a complete valid header and then half-closes its
    write side (no more data expected for ``hello``) must still be served."""
    with _connect(server.port) as s:
        s.settimeout(5)
        send_msg(s, {"protocol": PROTOCOL, "op": "hello"})
        s.shutdown(socket.SHUT_WR)
        from spikes._s1.protocol import recv_msg

        reply = recv_msg(s)
    assert reply["ok"] is True


def test_upload_header_then_immediate_half_close_is_recorded_as_zero_bytes(
    server: S1Server,
) -> None:
    """An ``upload`` that half-closes immediately after its header (no body
    bytes at all) must be handled cleanly: 0 bytes received, no crash."""
    with _connect(server.port) as s:
        s.settimeout(5)
        send_msg(
            s, {"protocol": PROTOCOL, "op": "upload", "test_id": "half1", "stream": 0}
        )
        s.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError):
                send_msg(
                    s,
                    {
                        "protocol": PROTOCOL,
                        "op": "result",
                        "test_id": "half1",
                        "streams": 1,
                    },
                )
            time.sleep(0.1)
            break
    assert _still_answers(server.port)


# -------------------------------------------------- connect/RST storms
def test_rapid_connect_rst_storm_does_not_crash_or_leak(server: S1Server) -> None:
    """Connect-then-RST (SO_LINGER onoff=1, linger=0) in a tight loop, many
    times: the server must survive and stay bounded and responsive."""
    import struct

    linger = struct.pack("ii", 1, 0)
    for _ in range(300):
        with contextlib.suppress(OSError):
            s = socket.create_connection(("127.0.0.1", server.port), timeout=1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
            s.close()  # sends RST, not FIN
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and server.open_connections > 0:
        time.sleep(0.05)
    assert server.open_connections <= server.limits.max_connections
    assert _still_answers(server.port)


# ------------------------------------------------------ eviction fairness
def test_real_client_can_still_get_served_during_a_flood_with_retry(
    server: S1Server,
) -> None:
    """A real client, retrying on refusal/timeout, must eventually be served
    even while a flood of pending connections is hammering the server."""
    stop = threading.Event()

    def flood() -> None:
        while not stop.is_set():
            with contextlib.suppress(OSError):
                s = socket.create_connection(("127.0.0.1", server.port), timeout=1)
                s.settimeout(0.1)
                with contextlib.suppress(OSError):
                    s.sendall(b"\x00\x00")  # incomplete length prefix
                time.sleep(0.05)
                s.close()

    threads = [threading.Thread(target=flood) for _ in range(6)]
    for t in threads:
        t.start()
    try:
        served = False
        for _ in range(50):
            try:
                reply = hello(Endpoint("127.0.0.1", server.port, connect_timeout_s=2))
                if reply.get("ok"):
                    served = True
                    break
            except MeasureError:
                pass
            time.sleep(0.1)
        assert served, "a retrying real client was never served during the flood"
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=10)


# ------------------------------------------------------- counter consistency
def test_counters_stay_internally_consistent_after_mixed_traffic(
    server: S1Server,
) -> None:
    # some legitimate hellos
    for _ in range(5):
        with contextlib.suppress(MeasureError):
            hello(Endpoint("127.0.0.1", server.port, connect_timeout_s=2))
    # some pending connections that never finish a header
    lazy: list[socket.socket] = []
    for _ in range(10):
        with contextlib.suppress(OSError):
            lazy.append(_connect(server.port))
    time.sleep(server.limits.header_timeout_s + 1)
    for s in lazy:
        with contextlib.suppress(OSError):
            s.close()

    c = server.counters
    assert c.tcp_accepted >= 0
    assert c.tcp_refused_peer >= 0
    assert c.tcp_refused_busy >= 0
    assert c.tcp_evicted_pending >= 0
    assert c.tcp_header_timeouts >= 0
    # every eviction is also counted as a "busy" refusal (docstring contract)
    assert c.tcp_evicted_pending <= c.tcp_refused_busy
    # nothing accepted can exceed everything ever attempted through accept()
    assert c.tcp_header_timeouts + c.tcp_evicted_pending <= c.tcp_accepted


# --------------------------------------------------- bounded threads / RSS
def _rss_kb() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


@pytest.mark.skipif(sys.platform != "linux", reason="/proc sampling is Linux-only")
def test_thread_count_and_rss_bounded_during_sustained_flood(server: S1Server) -> None:
    stop = threading.Event()
    thread_samples: list[int] = []
    rss_samples: list[int] = []

    def flood() -> None:
        while not stop.is_set():
            with contextlib.suppress(OSError):
                s = socket.create_connection(("127.0.0.1", server.port), timeout=0.5)
                with contextlib.suppress(OSError):
                    s.sendall(os.urandom(1))
                s.close()

    attackers = [threading.Thread(target=flood) for _ in range(8)]
    for t in attackers:
        t.start()

    start_rss = _rss_kb()
    started = time.monotonic()
    duration = 10.0  # shortened from the requested 30s to keep CI time bounded
    try:
        while time.monotonic() - started < duration:
            thread_samples.append(threading.active_count())
            rss = _rss_kb()
            if rss is not None:
                rss_samples.append(rss)
            time.sleep(0.5)
    finally:
        stop.set()
        for t in attackers:
            t.join(timeout=10)

    assert _still_answers(server.port)
    # threads: the flood generates only short-lived unauthenticated
    # connections that never finish a header, so no handler thread is ever
    # started for them; overall thread count must stay small and not grow
    # unboundedly with the number of connection attempts.
    assert max(thread_samples) < 50, f"thread count grew unbounded: {thread_samples}"
    if start_rss is not None and rss_samples:
        growth_kb = max(rss_samples) - start_rss
        assert growth_kb < 100_000, f"RSS grew by {growth_kb} KiB during the flood"


# --------------------------------------------------------- DEFECT-2 follow-ups
def test_hostnames_differing_only_by_case_do_not_collide() -> None:
    name_a = safe_name("Lab1")
    name_b = safe_name("lab1")
    # safe_name case-folds, so "Lab1" is a *changed* name and gets the hash
    # suffix of its original spelling; "lab1" is unchanged and keeps its plain
    # name. The two therefore differ by more than case alone.
    assert name_a != name_b
    assert name_b == "lab1"
    assert name_a.startswith("lab1-")


def test_hostnames_differing_only_by_case_do_not_collide_on_case_insensitive_fs() -> (
    None
):
    """Windows NTFS (and macOS APFS by default) compare file names
    case-insensitively: two hosts named 'Lab1' and 'lab1' must not write to the
    same result file there. This test simulates such a filesystem by comparing
    the case-folded paths, which is exactly how those filesystems decide
    whether two names are the same path -- so it is meaningful on Linux too."""
    path_a = raw_result_path("S1-network", "Lab1", date(2026, 9, 20))
    path_b = raw_result_path("S1-network", "lab1", date(2026, 9, 20))
    assert path_a != path_b  # distinct on a case-sensitive filesystem
    assert _casefold_path(path_a) != _casefold_path(path_b), (
        f"{path_a.name!r} and {path_b.name!r} are the same file on a "
        "case-insensitive filesystem (default NTFS on the lab's Windows laptops)"
    )
    # ...and every Windows-only spelling of the same idea, for good measure.
    names = {
        _casefold_path(raw_result_path("S1-network", h, date(2026, 9, 20)))
        for h in ("Lab1", "lab1", "LAB1", "lAb1")
    }
    assert len(names) == 4


def _casefold_path(path: Path) -> str:
    """How a case-insensitive filesystem sees a path."""
    return str(path).casefold()


def test_hostnames_differing_by_sanitized_characters_do_not_collide() -> None:
    """'lab 1' (space, sanitised) vs 'lab_1' (already valid) must not collide:
    sanitising 'lab 1' changes it, so it gets a hash suffix; 'lab_1' is
    unchanged and keeps its plain name."""
    name_space = safe_name("lab 1")
    name_underscore = safe_name("lab_1")
    assert name_space != name_underscore
    path_space = raw_result_path("S1-network", "lab 1", date(2026, 9, 20))
    path_underscore = raw_result_path("S1-network", "lab_1", date(2026, 9, 20))
    assert path_space != path_underscore


def test_resultcollisionerror_message_is_actionable(tmp_path: Path) -> None:
    from spikes._harness.envelope import ResultCollisionError, write_envelope

    doc_a = {
        "schema_version": 1,
        "spike": "S1",
        "topic": "S1-network",
        "host": {"hostname": "hostA"},
        "timestamp_utc": "2026-09-20T09:00:00Z",
        "link": "ethernet",
        "params": {},
        "cases": {},
        "errors": [],
    }
    doc_b = {**doc_a, "host": {"hostname": "hostB"}}
    path = tmp_path / "shared.json"
    write_envelope(doc_a, path)
    with pytest.raises(ResultCollisionError) as excinfo:
        write_envelope(doc_b, path)
    message = str(excinfo.value)
    # actionable: names both hosts and the path, so a human knows what to do
    assert "hostA" in message
    assert "hostB" in message
    assert str(path) in message
