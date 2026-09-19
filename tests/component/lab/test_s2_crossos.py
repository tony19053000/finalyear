"""Component tests for spike S2 (0.2.6): real HTTP + gRPC over 127.0.0.1.

Real sockets, a real gRPC server, real subprocesses (``python
lab/spikes/s2_crossos.py``), no mocks: HTTP GET and streamed body, gRPC unary
and server-streaming, the long-lived bidirectional session (ADR-0002) across
an idle gap with keepalive on and off, an exact diagnosis when the port is
closed or the peer vanishes mid-stream, bounded non-zero exits, and no leaked
processes, threads or ports.

The runs here are deliberately short (sub-second idle gaps); the real lab run
uses 30 s and 120 s gaps on two machines.
"""

from __future__ import annotations

import contextlib
import json
import signal
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
SCRIPT: Final = LAB_DIR / "spikes" / "s2_crossos.py"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._s2.client import (  # noqa: E402
    Endpoint,
    grpc_download,
    grpc_session,
    grpc_unary,
    http_blob,
    http_ping,
)
from spikes._s2.protocol import FailureType, ProbeError  # noqa: E402
from spikes._s2.run import ClientConfig, run_client  # noqa: E402
from spikes._s2.server import STOP_IDLE, S2Server, ServerLimits  # noqa: E402
from spikes._s2.verdict import direction_verdict  # noqa: E402

pytestmark = pytest.mark.timeout(240)

BLOB: Final = 256 * 1024


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _wait_free(ports: list[int], timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(_port_is_free(p) for p in ports):
            return True
        time.sleep(0.1)
    return False


class _Running:
    def __init__(self, server: S2Server, http_port: int, grpc_port: int) -> None:
        self.server, self.http_port, self.grpc_port = server, http_port, grpc_port

    @property
    def endpoint(self) -> Endpoint:
        return Endpoint("127.0.0.1", self.http_port, self.grpc_port, 3.0, 10.0)


@pytest.fixture
def server() -> Iterator[_Running]:
    srv = S2Server(
        "127.0.0.1",
        0,
        0,
        ServerLimits(duration_s=180.0, idle_s=120.0),
        name="loopback",
    )
    http_port, grpc_port = srv.open()
    thread = srv.start_thread()
    try:
        yield _Running(srv, http_port, grpc_port)
    finally:
        srv.stop()
        thread.join(timeout=20.0)
        assert not thread.is_alive()
        assert _wait_free([http_port, grpc_port]), "listeners leaked after stop()"


# ------------------------------------------------------------------- HTTP
def test_http_ping_returns_the_peer_identity(server: _Running) -> None:
    sample = http_ping(server.endpoint)
    identity = sample["peer_identity"]
    assert identity["hostname"]
    assert identity["system"]
    assert "is_wsl" in identity["wsl"]
    assert sample["connect_ms"] >= 0
    assert sample["ttfb_ms"] >= 0
    assert sample["total_ms"] >= sample["ttfb_ms"] - 1e-6


def test_http_blob_streams_the_whole_body(server: _Running) -> None:
    sample = http_blob(server.endpoint, BLOB)
    assert sample["bytes"] == BLOB
    assert sample["mbps"] > 0


def test_http_unknown_path_is_a_server_error(server: _Running) -> None:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server.http_port, timeout=5)
    try:
        conn.request("GET", "/nope")
        assert conn.getresponse().status == 404
    finally:
        conn.close()


def test_http_blob_rejects_an_absurd_size(server: _Running) -> None:
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", server.http_port, timeout=5)
    try:
        conn.request("GET", "/blob?bytes=999999999999")
        assert conn.getresponse().status == 400
    finally:
        conn.close()


# ------------------------------------------------------------------- gRPC
@pytest.mark.parametrize("keepalive", ["on", "off"])
def test_grpc_unary_round_trip(server: _Running, keepalive: str) -> None:
    sample = grpc_unary(server.endpoint, keepalive, seq=7)
    assert sample["peer_identity"]["op"] == "echo_reply"
    assert sample["peer_identity"]["seq"] == 7
    assert sample["rtt_ms"] > 0
    assert sample["channel_ready_ms"] >= 0


def test_grpc_server_streaming_delivers_every_byte(server: _Running) -> None:
    sample = grpc_download(server.endpoint, BLOB)
    assert sample["bytes"] == BLOB
    assert sample["mbps"] > 0
    assert sample["ttfb_ms"] >= 0


@pytest.mark.parametrize("keepalive", ["on", "off"])
def test_long_lived_bidi_session_survives_an_idle_gap(
    server: _Running, keepalive: str
) -> None:
    """ADR-0002 in miniature: the client dials out, both sides talk, the stream
    goes quiet, and it still works afterwards."""
    sample = grpc_session(
        server.endpoint,
        keepalive=keepalive,
        hold_s=0.6,
        ping_interval_s=0.15,
        idle_s=1.5,
    )
    assert sample["survived"] == 1
    assert sample["failure"] is None
    assert sample["msgs_up"] >= 3
    assert sample["msgs_down"] >= 3, "the server must write to the stream too"
    assert sample["alive_s"] >= 2.0
    assert sample["final_rtt_ms"] is not None
    assert sample["keepalive"] == keepalive


def test_session_counts_server_initiated_beats(server: _Running) -> None:
    sample = grpc_session(server.endpoint, hold_s=1.0, ping_interval_s=0.2, idle_s=0.5)
    assert sample["beats"] >= 1, "no server-initiated message arrived"


# --------------------------------------------------------- failure paths
def test_closed_port_is_diagnosed_as_refused_with_a_fix() -> None:
    dead = Endpoint("127.0.0.1", _free_port(), _free_port(), 2.0, 3.0)
    with pytest.raises(ProbeError) as exc:
        http_ping(dead)
    assert exc.value.failure_type is FailureType.CONNECTION_REFUSED
    assert "listening" in exc.value.fix
    assert exc.value.fatal is True


def test_closed_grpc_port_is_diagnosed_not_hung() -> None:
    dead = Endpoint("127.0.0.1", _free_port(), _free_port(), 2.0, 3.0)
    started = time.monotonic()
    with pytest.raises(ProbeError) as exc:
        grpc_unary(dead)
    assert time.monotonic() - started < 20.0
    assert exc.value.failure_type in {
        FailureType.CONNECTION_REFUSED,
        FailureType.CONNECT_TIMEOUT,
    }
    assert exc.value.fix


def test_peer_vanishing_mid_stream_is_recorded_not_hung(server: _Running) -> None:
    """The server is stopped while the session is idle: the run must end with a
    recorded diagnosis in bounded time, not hang until the deadline."""
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            result["sample"] = grpc_session(
                server.endpoint, hold_s=0.5, ping_interval_s=0.2, idle_s=4.0
            )
        except ProbeError as exc:
            result["error"] = exc

    worker = threading.Thread(target=run, name="s2-session-under-test")
    worker.start()
    time.sleep(2.0)
    server.server.stop()
    worker.join(timeout=60.0)
    assert not worker.is_alive(), "the session did not end after the peer vanished"
    if "error" in result:
        assert result["error"].fix
    else:
        sample = result["sample"]
        assert sample["survived"] == 0
        assert sample["failure"]["fix"]
        assert sample["failure"]["failure_type"]


def test_run_client_records_an_unreachable_peer_without_raising() -> None:
    cfg = ClientConfig(
        server="127.0.0.1",
        http_port=_free_port(),
        grpc_port=_free_port(),
        link="ethernet",
        reps=5,
        warmup=0,
        connect_timeout_s=2.0,
        io_timeout_s=3.0,
    )
    started = time.monotonic()
    run = run_client(cfg, log=lambda _m: None)
    assert time.monotonic() - started < 60.0
    assert run.complete is False
    assert run.envelope["errors"], "the diagnosis must be recorded"
    assert run.envelope["errors"][0]["fix"]
    verdict = direction_verdict(run.envelope)
    assert verdict.cases["http-ping"].value == "TBD"


# ------------------------------------------------------- full client run
def test_full_run_produces_a_complete_verdict(server: _Running) -> None:
    cfg = ClientConfig(
        server="127.0.0.1",
        http_port=server.http_port,
        grpc_port=server.grpc_port,
        link="ethernet",
        reps=5,
        warmup=0,
        blob_bytes=BLOB,
        hold_s=0.3,
        ping_interval_s=0.1,
        idle_gaps_s=(0.5,),
        keepalives=("on", "off"),
        pause_s=0.0,
        name="client-a",
        no_inbound_rule=True,
        note="no Defender prompt (loopback)",
    )
    run = run_client(cfg, log=lambda _m: None)
    assert run.complete is True, run.envelope["errors"]
    verdict = direction_verdict(run.envelope)
    assert all(v.value == "yes" for v in verdict.cases.values()), verdict.fixes()
    assert all(v.value == "yes" for v in verdict.sessions.values()), verdict.fixes()
    assert verdict.outbound_only.value == "yes"
    assert verdict.metrics["http_mbps"] and verdict.metrics["grpc_mbps"]
    extra = run.envelope["extra"]
    assert extra["operator_note"]
    assert extra["outbound_only"]["client_listeners_opened"] == 0
    assert "wsl" in extra
    assert extra["endpoint"]["remote_address"] == "127.0.0.1"
    for case in run.envelope["cases"].values():
        assert len(case["samples"]) == 5
        assert not case["errors"]


# ----------------------------------------------------------------- the CLI
def _cmd(*args: str) -> list[str]:
    return [sys.executable, str(SCRIPT), *args]


def _wait_ready(
    path: Path, proc: subprocess.Popen[bytes], timeout_s: float = 30.0
) -> dict[str, int]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"serve exited early with {proc.returncode}")
        if path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                data = json.loads(path.read_text(encoding="utf-8"))
                if "http_port" in data:
                    return {k: int(v) for k, v in data.items()}
        time.sleep(0.1)
    raise AssertionError("serve never became ready")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=15)
    if proc.poll() is None:  # pragma: no cover - only if terminate was ignored
        proc.kill()
        proc.wait(timeout=15)


@pytest.mark.timeout(300)
def test_cli_serve_and_client_end_to_end(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    log = tmp_path / "serve.log"
    with log.open("wb") as handle:
        serve = subprocess.Popen(
            _cmd(
                "serve",
                "--bind",
                "127.0.0.1",
                "--http-port",
                "0",
                "--grpc-port",
                "0",
                "--idle",
                "180",
                "--duration",
                "240",
                "--ready-file",
                str(ready),
                "--name",
                "server-b",
            ),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        try:
            ports = _wait_ready(ready, serve)
            out_dir = tmp_path / "raw"
            client = subprocess.run(
                _cmd(
                    "client",
                    "--server",
                    "127.0.0.1",
                    "--http-port",
                    str(ports["http_port"]),
                    "--grpc-port",
                    str(ports["grpc_port"]),
                    "--link",
                    "ethernet",
                    "--reps",
                    "5",
                    "--warmup",
                    "0",
                    "--blob-bytes",
                    str(BLOB),
                    "--hold",
                    "0.3",
                    "--ping-interval",
                    "0.1",
                    "--idle-gaps",
                    "0.5",
                    "--keepalive",
                    "both",
                    "--pause",
                    "0",
                    "--name",
                    "client-a",
                    "--no-inbound-rule",
                    "--note",
                    "Windows Defender prompt: n/a (loopback)",
                    "--out-dir",
                    str(out_dir),
                ),
                capture_output=True,
                timeout=280,
            )
        finally:
            _stop(serve)
    stderr = client.stderr.decode("utf-8", "replace")
    assert client.returncode == 0, stderr
    assert "S2 verdict" in stderr
    assert "outbound-only" in stderr
    results = list(out_dir.glob("*.json"))
    assert len(results) == 1
    doc = json.loads(results[0].read_text(encoding="utf-8"))
    assert doc["spike"] == "S2"
    assert doc["params"]["server_name"] == "server-b"
    assert set(doc["cases"]) >= {
        "http-ping",
        "http-blob",
        "grpc-unary",
        "grpc-stream",
        "grpc-bidi-idle0.5s-kaon",
        "grpc-bidi-idle0.5s-kaoff",
    }
    assert doc["params"]["keepalive_settings"]["on"]["client_options"]
    assert doc["extra"]["complete"] is True

    report = tmp_path / "report.md"
    built = subprocess.run(
        _cmd(
            "report",
            "--results",
            str(out_dir),
            "--out",
            str(report),
            "--hosts",
            "A=client-a,B=server-b",
        ),
        capture_output=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stderr.decode("utf-8", "replace")
    text = report.read_text(encoding="utf-8")
    assert "client-a -> server-b" in text
    assert "outbound-only" in text
    assert _wait_free(list(ports.values())), "the CLI leaked a listening port"


@pytest.mark.timeout(180)
def test_cli_client_against_a_dead_peer_exits_three(tmp_path: Path) -> None:
    out_dir = tmp_path / "raw"
    started = time.monotonic()
    result = subprocess.run(
        _cmd(
            "client",
            "--server",
            "127.0.0.1",
            "--http-port",
            str(_free_port()),
            "--grpc-port",
            str(_free_port()),
            "--link",
            "wifi",
            "--reps",
            "5",
            "--warmup",
            "0",
            "--connect-timeout",
            "2",
            "--io-timeout",
            "3",
            "--out-dir",
            str(out_dir),
        ),
        capture_output=True,
        timeout=150,
    )
    assert result.returncode == 3, result.stderr.decode("utf-8", "replace")
    assert time.monotonic() - started < 120.0
    doc = json.loads(next(iter(out_dir.glob("*.json"))).read_text(encoding="utf-8"))
    assert doc["errors"][0]["failure_type"] in {"connection_refused", "connect_timeout"}
    assert doc["errors"][0]["fix"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
@pytest.mark.timeout(120)
def test_cli_serve_stops_cleanly_on_sigterm(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    serve = subprocess.Popen(
        _cmd(
            "serve",
            "--bind",
            "127.0.0.1",
            "--http-port",
            "0",
            "--grpc-port",
            "0",
            "--ready-file",
            str(ready),
            "--quiet",
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ports = _wait_ready(ready, serve)
        serve.send_signal(signal.SIGTERM)
        assert serve.wait(timeout=30) == 0
    finally:
        _stop(serve)
    assert _wait_free(list(ports.values()))


@pytest.mark.timeout(120)
def test_server_stops_itself_when_idle() -> None:
    srv = S2Server(
        "127.0.0.1", 0, 0, ServerLimits(duration_s=60.0, idle_s=1.0), name="idle"
    )
    http_port, grpc_port = srv.open()
    started = time.monotonic()
    assert srv.serve() == STOP_IDLE
    assert time.monotonic() - started < 30.0
    assert _wait_free([http_port, grpc_port])
