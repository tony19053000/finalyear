"""Adversarial component tests for spike S3 (task 0.2.7), independent of the
implementer's own suite. These target the attack surfaces the implementer
nominated in the handoff and a few more found by reading ``S3.md`` and
``ARCHITECTURE`` failure model (§13) as the spec, not the code:

* non-LAN peers on all three ports (not just the raw-TCP control port);
* a server that sends exactly ``size - 1`` bytes (an off-by-one truncation,
  not a small stub write);
* the "vice versa" gRPC asymmetry: a client without grpcio against a real
  server that *has* it (the implementer's own tests only cover a server
  without grpcio against a client that has it);
* a connect-and-hang client on the HTTP port (header/request-line timeout,
  not the TCP control port already covered elsewhere);
* hostile control messages with an astronomically large declared size and a
  request missing the ``size`` field entirely;
* ``run_client`` phase boundaries the implementer's own tests do not hit:
  the server dying between ``hello`` and the very first case, and during the
  gRPC chunk sweep (which runs *after* the per-size case loop);
* the loopback ceiling child dying mid-measurement (not just failing to
  start) - the run must stay bounded and honest, never hang or fabricate a
  number for a transport whose child is already gone.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
LAB_DIR: Final = REPO_ROOT / "lab"
SCRIPT: Final = LAB_DIR / "spikes" / "s3_transport.py"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._s3 import (  # noqa: E402
    ceiling,
    grpc_server,
    http_server,
    payload,
    tcp_server,
)
from spikes._s3.client import Endpoint, control, hello  # noqa: E402
from spikes._s3.context import ServerLimits  # noqa: E402
from spikes._s3.grpc_server import GRPC_IMPORT_ERROR  # noqa: E402
from spikes._s3.protocol import (  # noqa: E402
    KIB,
    MIB,
    FailureType,
    TransferError,
    recv_msg,
)
from spikes._s3.run import ClientConfig, run_client  # noqa: E402
from spikes._s3.server import S3Server  # noqa: E402

pytestmark = pytest.mark.timeout(180)

SIZE: Final = 256 * KIB
CHUNK: Final = 64 * KIB
needs_grpc = pytest.mark.skipif(
    GRPC_IMPORT_ERROR is not None, reason=GRPC_IMPORT_ERROR or ""
)


def _endpoint(server: S3Server, **kw: Any) -> Endpoint:
    ports = server.ports
    base: dict[str, Any] = {
        "connect_timeout_s": 3,
        "io_timeout_s": 5,
        "transfer_timeout_s": 30,
    }
    base.update(kw)
    return Endpoint("127.0.0.1", ports["tcp"], ports["grpc"], ports["http"], **base)  # fmt: skip


@pytest.fixture
def server() -> Iterator[S3Server]:
    srv = S3Server(
        "127.0.0.1",
        0,
        ServerLimits(idle_s=60, io_timeout_s=3, header_timeout_s=2, result_wait_s=3),
        name="inproc",
    )
    thread = srv.start_thread()
    deadline = time.monotonic() + 10
    while not srv.ports and time.monotonic() < deadline:
        time.sleep(0.02)
    assert srv.ports, "server did not open its ports"
    yield srv
    srv.stop()
    thread.join(timeout=15)
    assert not thread.is_alive(), "server thread leaked"


def _raw(server: S3Server, port_key: str = "tcp") -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", server.ports[port_key]), timeout=5)
    sock.settimeout(5)
    return sock


# ------------------------------------------------------- non-LAN peers
def test_non_lan_peer_is_refused_on_all_three_ports(
    server: S3Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LAB.md §6: "every transport admits LAN peers only". A real socket test
    cannot forge a non-LAN source address on loopback, so this patches the
    *address classifier* each transport calls at accept time (never mocking
    the transport logic itself) and drives a real connection through it - the
    same integration path a genuine non-LAN client would hit.
    """
    monkeypatch.setattr(tcp_server, "is_lan_address", lambda _host: False)
    monkeypatch.setattr(http_server, "is_lan_address", lambda _host: False)
    monkeypatch.setattr(grpc_server, "is_lan_address", lambda _host: False)

    before_peer = server.ctx.counters.refused_peer
    with _raw(server) as sock:
        assert sock.recv(10) == b""  # dropped before any control reply
    assert server.ctx.counters.refused_peer == before_peer + 1

    conn = http.client.HTTPConnection("127.0.0.1", server.ports["http"], timeout=5)
    try:
        conn.request("GET", "/hello")
        with pytest.raises(
            (http.client.RemoteDisconnected, ConnectionResetError, OSError)
        ):
            conn.getresponse()
    finally:
        conn.close()
    assert server.ctx.counters.refused_peer == before_peer + 2

    if GRPC_IMPORT_ERROR is None:
        import grpc

        from spikes._s3.protocol import GRPC_DOWNLOAD, grpc_target

        channel = grpc.insecure_channel(grpc_target("127.0.0.1", server.ports["grpc"]))
        try:
            download = channel.unary_stream(GRPC_DOWNLOAD, lambda b: b, lambda b: b)
            request = json.dumps(
                {"transfer_id": "t-nonlan", "size": SIZE, "chunk": CHUNK}
            ).encode()
            with pytest.raises(grpc.RpcError) as exc:
                list(download(request, timeout=10))
            assert exc.value.code() is grpc.StatusCode.PERMISSION_DENIED
        finally:
            channel.close()
        assert server.ctx.counters.refused_peer == before_peer + 3

    # the server survives the hostile probing and serves the *real* loopback
    # peer normally once the classifier is restored
    monkeypatch.undo()
    assert hello(_endpoint(server))["ok"] is True


# ----------------------------------------------------- truncated by 1 byte
@contextlib.contextmanager
def _one_shot_tcp_server(handler: Any) -> Iterator[int]:
    """A minimal one-connection TCP server that runs ``handler`` once, so a
    real network fault (truncation) can be injected precisely, independent of
    ``S3Server`` and the implementer's own ``fake_tcp_server`` helper."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                conn.settimeout(10)
                handler(conn)
        except OSError:
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        listener.close()
        thread.join(timeout=10)


def test_server_sending_exactly_size_minus_one_bytes_is_short_transfer() -> None:
    """The task's own wording: "make the server send size - 1 bytes". A
    truncated-by-exactly-one-byte transfer must be ``short_transfer``, never a
    fast-looking valid row, and never misreported as ``checksum_mismatch``
    (the bytes that did arrive are byte-for-byte correct - only the last byte
    is missing).
    """
    from spikes._s3.client import tcp_download

    def handler(conn: socket.socket) -> None:
        msg = recv_msg(conn)
        assert msg["op"] == "download"
        conn.sendall(bytes(payload.chunk_view(0, SIZE - 1)))  # size - 1, then close

    with _one_shot_tcp_server(handler) as port, pytest.raises(TransferError) as exc:
        tcp_download(Endpoint("127.0.0.1", port, 0, 0, io_timeout_s=5), SIZE, CHUNK)
    assert exc.value.failure_type is FailureType.SHORT_TRANSFER
    assert f"{SIZE - 1} of {SIZE}" in str(exc.value)


# --------------------------------------------- grpc asymmetry, vice versa
def test_client_without_grpc_against_real_grpc_server(
    server: S3Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The implementer's suite covers "server has no grpcio, client does".
    This covers the opposite: the server has a real, working gRPC service,
    but *this* client's grpcio import failed. The gRPC cases must degrade to
    a typed, non-fatal ``grpc_unavailable`` without ever touching the
    network, while TCP/HTTP against the very same server keep working.
    """
    if GRPC_IMPORT_ERROR is not None:
        pytest.skip("grpcio not installed; nothing to make asymmetric")
    from spikes._s3.client import (
        grpc_download,
        grpc_upload,
        http_download,
        tcp_download,
    )

    monkeypatch.setattr(
        grpc_server, "GRPC_IMPORT_ERROR", "grpcio is not installed (simulated client)"
    )
    ep = _endpoint(server)
    with pytest.raises(TransferError) as exc:
        grpc_download(ep, SIZE, CHUNK)
    assert exc.value.failure_type is FailureType.GRPC_UNAVAILABLE
    assert not exc.value.fatal
    with pytest.raises(TransferError) as exc:
        grpc_upload(ep, SIZE, CHUNK)
    assert exc.value.failure_type is FailureType.GRPC_UNAVAILABLE
    # the server's own gRPC is untouched: TCP/HTTP still work over the same
    # server, and a *real* grpc client (not patched) can still reach it
    assert tcp_download(ep, SIZE, CHUNK)["bytes"] == SIZE
    assert http_download(ep, SIZE, CHUNK)["bytes"] == SIZE
    monkeypatch.undo()
    assert grpc_download(ep, SIZE, CHUNK)["bytes"] == SIZE


# --------------------------------------------------- connect-and-hang (HTTP)
def test_http_connect_and_hang_is_bounded_by_io_timeout_and_frees_its_slot(
    server: S3Server,
) -> None:
    """A client that connects to the HTTP port and never sends a request line
    must not hang the handler thread forever or hold its admission slot past
    ``io_timeout_s`` - unlike the TCP control port's slow-loris case (already
    covered elsewhere), ``ThreadingHTTPServer`` handles this through the
    per-connection socket timeout, not the header-timeout counter.
    """
    http_srv = server._http
    assert http_srv is not None
    with _raw(server, "http") as sock:
        started = time.monotonic()
        # never send anything
        closed = sock.recv(10) == b""
        elapsed = time.monotonic() - started
    assert closed, "server never closed the hung connection"
    assert elapsed < server.ctx.limits.io_timeout_s + 5.0
    deadline = time.monotonic() + 5
    while http_srv.active_connections and time.monotonic() < deadline:
        time.sleep(0.05)
    assert http_srv.active_connections == 0
    assert hello(_endpoint(server))["ok"] is True


# ------------------------------------------------- hostile control fields
def test_astronomically_large_declared_size_is_refused_immediately(
    server: S3Server,
) -> None:
    """A declared size far beyond ``MAX_SIZE_BYTES`` (here: larger than a
    64-bit byte count would ever need) must be refused by ``transfer_request``
    before anything is allocated - never accepted, never hung, never silently
    clamped.
    """
    ep = _endpoint(server)
    with pytest.raises(TransferError, match="bad size") as exc:
        control(
            ep,
            {
                "op": "download",
                "transfer_id": "t-huge",
                "size": 10**30,
                "chunk": CHUNK,
            },
        )
    assert exc.value.failure_type is FailureType.SERVER_ERROR
    assert hello(ep)["ok"] is True


def test_control_message_missing_size_field_is_refused(server: S3Server) -> None:
    ep = _endpoint(server)
    with pytest.raises(TransferError, match="bad size"):
        control(ep, {"op": "download", "transfer_id": "t-missing", "chunk": CHUNK})
    with pytest.raises(TransferError, match="bad transfer_id"):
        control(ep, {"op": "download", "size": SIZE, "chunk": CHUNK})
    assert hello(ep)["ok"] is True


def test_control_message_negative_and_float_size_is_refused(server: S3Server) -> None:
    ep = _endpoint(server)
    for bad_size in (-1, 1.5, "64", None, True):
        with pytest.raises(TransferError, match="bad size"):
            control(
                ep,
                {
                    "op": "download",
                    "transfer_id": "t-bad-size",
                    "size": bad_size,
                    "chunk": CHUNK,
                },
            )
    assert hello(ep)["ok"] is True


# ------------------------------------------------- run_client phase kills
def _config(server: S3Server, **kw: Any) -> ClientConfig:
    base: dict[str, Any] = {
        "server": "127.0.0.1",
        "port": server.ports["tcp"],
        "link": "wifi",
        "reps": 5,
        "warmup": 0,
        "sizes": (64 * KIB,),
        "max_size": 64 * KIB,
        "chunk": CHUNK,
        "chunk_sweep": (4 * KIB, CHUNK),
        "chunk_sweep_size": 64 * KIB,
        "range_parts": 2,
        "pause_s": 0.0,
        "connect_timeout_s": 3.0,
        "io_timeout_s": 5.0,
        "transfer_timeout_s": 30.0,
        "name": "inproc-client",
    }
    base.update(kw)
    return ClientConfig(**base)


def test_run_client_records_server_dying_between_hello_and_first_case(
    server: S3Server,
) -> None:
    """The server answers ``hello`` and then vanishes before the very first
    case measures anything - the earliest possible phase boundary. Every case
    must be skipped and listed, never attempted with ``reps x cases``
    timeouts, and the run must stay bounded.
    """
    seen: list[str] = []
    stopped = False

    def log(line: str) -> None:
        nonlocal stopped
        seen.append(line)
        if not stopped and line.startswith("tcp-down-64KiB:"):
            stopped = True
            server.stop()
            server.close()

    started = time.monotonic()
    run = run_client(_config(server), log=log)
    assert time.monotonic() - started < 60
    assert not run.complete
    doc = run.envelope
    # the very first case aborted with zero samples
    first = doc["cases"]["tcp-down-64KiB"]
    assert first["samples"] == [] and first["aborted"] is True
    (error,) = run.errors
    assert error["phase"] == "tcp-down-64KiB"
    assert error["failure_type"] == str(FailureType.CONNECTION_LOST)
    # nothing after the first case was even attempted
    assert set(doc["cases"]) == {"tcp-down-64KiB"}
    assert any("skipping" in line for line in seen)


def test_run_client_records_server_dying_during_chunk_sweep(server: S3Server) -> None:
    """The chunk sweep runs *after* the whole per-size case loop; a server
    dying during the sweep must not be conflated with a per-size failure and
    must still produce a bounded, honest, skipped-case list.
    """
    seen: list[str] = []
    stopped = False
    sweep_case_id = "grpc-down-64KiB-chunk4KiB"

    def log(line: str) -> None:
        nonlocal stopped
        seen.append(line)
        if not stopped and line.startswith(f"{sweep_case_id}:"):
            stopped = True
            server.stop()
            server.close()

    if GRPC_IMPORT_ERROR is not None:
        pytest.skip("grpcio not installed; the sweep is all-grpc")

    started = time.monotonic()
    run = run_client(_config(server), log=log)
    assert time.monotonic() - started < 60
    assert not run.complete
    doc = run.envelope
    # every non-sweep case (all 6 families at the one size) completed fully
    non_sweep = {c for c in doc["cases"] if not doc["cases"][c]["attrs"].get("sweep")}
    assert len(non_sweep) == 6
    for case_id in non_sweep:
        assert len(doc["cases"][case_id]["samples"]) == 5, case_id
    swept = doc["cases"][sweep_case_id]
    assert swept["aborted"] is True and swept["samples"] == []
    (error,) = run.errors
    assert error["phase"] == sweep_case_id
    assert "skipped cases" in error["message"]


# ------------------------------------------------------------ ceiling kill
def test_loopback_ceiling_bounded_when_child_dies_mid_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loopback ceiling's own child process must not let a mid-measurement
    death hang the run or fabricate a number for a transport whose peer is
    already gone. A short ``--duration``/``--idle`` races against measurement
    speed and is unreliable on a fast machine (the whole ceiling can finish in
    a few milliseconds on loopback), so this instead wraps
    ``subprocess.Popen`` to kill the *real* child with a background timer
    shortly after it starts - independent of how fast the measurement runs.
    """
    real_popen = subprocess.Popen

    def killing_popen(*args: Any, **kwargs: Any) -> Any:
        proc = real_popen(*args, **kwargs)
        timer = threading.Timer(0.15, lambda: proc.kill())
        timer.daemon = True
        timer.start()
        return proc

    # patched by qualified name so ``ceiling.py``'s own ``subprocess.Popen``
    # call (same module object) sees the wrapper too
    monkeypatch.setattr("subprocess.Popen", killing_popen)
    started = time.monotonic()
    result = ceiling.loopback_ceiling(
        SCRIPT, size=256 * MIB, chunk=CHUNK, reps=5, timeout_s=60
    )
    elapsed = time.monotonic() - started
    assert elapsed < 60, "a dead ceiling child must not hang the run"
    # the top-level call never raises and never claims total failure just
    # because the child died mid-run: it is honest per transport instead.
    assert result["ok"] is True
    per = result["per_transport"]
    assert set(per) == {"tcp-down", "grpc-down", "http-get"}
    # at least one transport measured after the child died must record a
    # typed error rather than a fabricated throughput number
    broken = [item for item in per.values() if item["error"] is not None]
    assert broken, "expected at least one transport to see the dead child"
    for item in broken:
        assert item["mean_mbps"] is None
        assert item["n"] == 0
