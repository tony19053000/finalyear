"""Component tests for spike S1 (0.2.5): real server + client over 127.0.0.1.

Real sockets, real processes (``python lab/spikes/s1_network.py``), no mocks:
throughput > 0 both directions, sane loopback RTT/loss, bounded failure when the
server is killed mid-run, clean Ctrl-C / SIGTERM, and no leaked processes,
threads or ports.
"""

# ruff: noqa: RUF001 - the en dash is docs/LAB.md's pair separator

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
SCRIPT: Final = LAB_DIR / "spikes" / "s1_network.py"
if str(LAB_DIR) not in sys.path:
    sys.path.insert(0, str(LAB_DIR))

from spikes._harness.envelope import safe_name  # noqa: E402
from spikes._s1.ceiling import stop_process, wait_ready  # noqa: E402
from spikes._s1.client import (  # noqa: E402
    Endpoint,
    hello,
    request,
    tcp_throughput,
    udp_rtt,
)
from spikes._s1.protocol import FATAL, MeasureError, send_msg  # noqa: E402
from spikes._s1.server import (  # noqa: E402
    STOP_DURATION,
    STOP_IDLE,
    STOP_REQUESTED,
    S1Server,
    ServerLimits,
)

pytestmark = pytest.mark.timeout(180)

FAST_CLIENT: Final = [
    "--reps", "5", "--warmup", "1", "--seconds", "0.3", "--pings", "30",
    "--ping-interval", "0.002", "--ping-timeout", "0.5", "--pause", "0",
    "--connect-timeout", "2", "--io-timeout", "3",
]  # fmt: skip


def _cmd(*args: str) -> list[str]:
    return [sys.executable, str(SCRIPT), *args]


def _port_is_free(port: int) -> bool:
    """TCP and UDP ``127.0.0.1:port`` can be bound again (nothing leaked)."""
    for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
        with socket.socket(socket.AF_INET, kind) as sock:
            if sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                return False
    return True


class ServerProc:
    def __init__(self, proc: subprocess.Popen[bytes], port: int, log: Path) -> None:
        self.proc, self.port, self.log = proc, port, log

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


@contextlib.contextmanager
def server_process(tmp: Path, name: str, *extra: str) -> Iterator[ServerProc]:
    ready, log = tmp / f"{name}.port", tmp / f"{name}.log"
    with log.open("wb") as log_file:
        proc = subprocess.Popen(
            _cmd(
                "serve", "--bind", "127.0.0.1", "--port", "0", "--name", name,
                "--idle", "60", "--ready-file", str(ready), *extra,
            ),
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )  # fmt: skip
        try:
            port = wait_ready(ready, proc, timeout_s=30)
            assert port is not None, log.read_text(encoding="utf-8", errors="replace")
            yield ServerProc(proc, port, log)
        finally:
            stop_process(proc)
    assert proc.poll() is not None, "server process leaked"


@pytest.fixture
def inproc_server() -> Iterator[S1Server]:
    server = S1Server(
        "127.0.0.1", 0, ServerLimits(idle_s=60, io_timeout_s=5), name="inproc"
    )
    thread = server.start_thread()
    yield server
    server.stop()
    thread.join(timeout=10)
    assert not thread.is_alive(), "server thread leaked"


# ------------------------------------------------------- in-process functions
@pytest.mark.parametrize("streams", [1, 4])
@pytest.mark.parametrize("direction", ["upload", "download"])
def test_tcp_throughput_both_directions(
    inproc_server: S1Server, direction: str, streams: int
) -> None:
    sample = tcp_throughput(
        Endpoint("127.0.0.1", inproc_server.port),
        direction=direction,  # type: ignore[arg-type]
        streams=streams,
        seconds=0.3,
    )
    assert sample["receiver_mbps"] > 0 and sample["sender_mbps"] > 0
    assert sample["bytes_received"] > 0
    # every byte the sender's kernel accepted arrived (bytes counted at receiver)
    assert sample["bytes_match"] is True
    assert sample["streams"] == streams
    assert 0.2 < sample["receiver_s"] < 5


def test_udp_rtt_loopback_is_sane(inproc_server: S1Server) -> None:
    sample = udp_rtt(
        Endpoint("127.0.0.1", inproc_server.port), count=100, interval_s=0.001
    )
    assert sample["received"] == 100 and sample["loss_pct"] == 0.0
    assert 0 < sample["rtt_mean_ms"] < 50
    assert sample["rtt_min_ms"] <= sample["rtt_mean_ms"] <= sample["rtt_max_ms"]
    assert sample["jitter_ms"] >= 0
    assert inproc_server.counters.udp_echoed >= 100


def test_hello_reports_identity(inproc_server: S1Server) -> None:
    reply = hello(Endpoint("127.0.0.1", inproc_server.port))
    assert reply["name"] == "inproc" and reply["peer"] == "127.0.0.1"


def test_garbage_header_does_not_break_the_server(inproc_server: S1Server) -> None:
    for junk in (
        b"\x00\x00\x00\x05hello",
        b"\xff\xff\xff\xff",
        b"GET / HTTP/1.0\r\n\r\n",
    ):
        with socket.create_connection(
            ("127.0.0.1", inproc_server.port), timeout=5
        ) as s:
            s.sendall(junk)
            s.shutdown(socket.SHUT_WR)
            with contextlib.suppress(OSError):
                s.recv(100)
    # well-framed but invalid requests: typed refusal or a closed connection
    ep = Endpoint("127.0.0.1", inproc_server.port, io_timeout_s=3)
    with pytest.raises(MeasureError, match="unknown op"):
        request(ep, {"op": "format-disk"})
    with pytest.raises(MeasureError, match="protocol must be"):
        request(ep, {"op": "hello", "protocol": 99})
    for bad in (
        {"protocol": 1, "op": "upload", "test_id": "x", "stream": 99},
        {"protocol": 1, "op": "download", "test_id": "../x", "stream": 0, "seconds": 1},
        {"protocol": 1, "op": "download", "test_id": "x", "stream": 0, "seconds": 1e9},
    ):
        with socket.create_connection(
            ("127.0.0.1", inproc_server.port), timeout=5
        ) as s:
            send_msg(s, bad)
            s.settimeout(5)
            assert s.recv(100) == b""  # server closed without sending data
    assert hello(Endpoint("127.0.0.1", inproc_server.port))["ok"] is True


def test_udp_ignores_foreign_packets(inproc_server: S1Server) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(0.5)
        s.sendto(b"not our magic" * 3, ("127.0.0.1", inproc_server.port))
        with pytest.raises(TimeoutError):
            s.recvfrom(2048)
    assert inproc_server.counters.udp_dropped >= 1


def test_stop_joins_threads_and_frees_port() -> None:
    before = threading.active_count()
    server = S1Server("127.0.0.1", 0, ServerLimits(idle_s=60))
    thread = server.start_thread()
    port = server.port
    tcp_throughput(
        Endpoint("127.0.0.1", port), direction="download", streams=4, seconds=0.2
    )
    server.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert server.stop_reason == STOP_REQUESTED
    deadline = time.monotonic() + 5
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert threading.active_count() <= before
    assert _port_is_free(port)


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (ServerLimits(idle_s=0.3), STOP_IDLE),
        (ServerLimits(duration_s=0.3), STOP_DURATION),
    ],
)
def test_server_auto_stops(limits: ServerLimits, reason: str) -> None:
    server = S1Server("127.0.0.1", 0, limits)
    started = time.monotonic()
    assert server.serve() == reason
    assert time.monotonic() - started < 5


def test_dead_server_gives_typed_fatal_errors() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # bound but not listening: connections refused
        ep = Endpoint("127.0.0.1", port, connect_timeout_s=2, io_timeout_s=2)
        with pytest.raises(MeasureError) as tcp_err:
            tcp_throughput(ep, direction="upload", streams=1, seconds=0.2)
        assert tcp_err.value.fatal
        started = time.monotonic()
        with pytest.raises(MeasureError) as udp_err:
            udp_rtt(ep, count=10, interval_s=0.01, reply_timeout_s=0.5)
        assert udp_err.value.fatal
        assert time.monotonic() - started < 5


# ------------------------------------------------------------------ real CLI
def _load(path: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return doc


def test_cli_client_end_to_end_and_matrix(tmp_path: Path) -> None:
    results = tmp_path / "2026-09-20-S1-network"
    with server_process(tmp_path, "srvB") as srv:
        run = subprocess.run(
            _cmd(
                "client", "--server", "127.0.0.1", "--port", str(srv.port),
                "--link", "ethernet", "--name", "cliA", "--ceiling-seconds", "0.5",
                "--out-dir", str(results), *FAST_CLIENT,
            ),
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=150, check=False,
        )  # fmt: skip
    assert run.returncode == 0, run.stderr
    assert "loopback ceiling:" in run.stderr
    assert "buffers:" in run.stderr
    # file names are case-folded (NTFS is case-insensitive), so a mixed-case
    # host name gets its lower-case form plus the hash of the original.
    out = results / f"{safe_name('cliA')}__{safe_name('to-srvB_ethernet')}.json"
    doc = _load(out)
    assert doc["spike"] == "S1" and doc["link"] == "ethernet"
    assert doc["errors"] == [] and doc["extra"]["complete"] is True
    assert doc["extra"]["loopback_ceiling"]["ok"] is True
    assert doc["host"]["label"] == "cliA" and doc["params"]["server_name"] == "srvB"
    assert sorted(doc["cases"]) == [
        "tcp-download-x1", "tcp-download-x4", "tcp-upload-x1", "tcp-upload-x4", "udp-rtt",
    ]  # fmt: skip
    for case_id, case in doc["cases"].items():
        assert len(case["samples"]) == 5 and len(case["warmup"]) == 1, case_id
        if case_id.startswith("tcp"):
            assert case["summary"]["receiver_mbps"]["mean"] > 0
            assert case["summary"]["receiver_mbps"]["n"] == 5
    udp = doc["cases"]["udp-rtt"]["summary"]
    assert 0 < udp["rtt_mean_ms"]["mean"] < 50
    assert udp["loss_pct"]["mean"] <= 5.0
    report = tmp_path / "report.md"
    matrix = subprocess.run(
        _cmd("matrix", "--results", str(results), "--out", str(report),
             "--labels", "L1=cliA,L2=srvB"),
        cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
        timeout=60, check=False,
    )  # fmt: skip
    assert matrix.returncode == 0, matrix.stderr
    text = report.read_text(encoding="utf-8")
    ethernet = text[text.index("#### Ethernet") :]
    l1_row = next(ln for ln in ethernet.splitlines() if ln.startswith("| L1 |"))
    l2_row = next(ln for ln in ethernet.splitlines() if ln.startswith("| L2 |"))
    assert "(n=5)" in l1_row and "(n=5)" in l2_row  # both directions filled
    assert "| L1–L2 |" in ethernet and "TBD" not in l1_row


def test_server_killed_mid_run_bounded_exit(tmp_path: Path) -> None:
    results = tmp_path / "raw"
    with server_process(tmp_path, "victim") as srv:
        started = time.monotonic()
        client = subprocess.Popen(
            _cmd(
                "client", "--server", "127.0.0.1", "--port", str(srv.port),
                "--link", "wifi", "--name", "survivor", "--ceiling-seconds", "0",
                "--out-dir", str(results), "--reps", "5", "--warmup", "0",
                "--seconds", "2", "--pings", "20", "--ping-interval", "0.005",
                "--pause", "0", "--connect-timeout", "2", "--io-timeout", "3",
            ),
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )  # fmt: skip
        try:
            time.sleep(2.0)  # UDP case done, first TCP repetition running
            srv.proc.kill()
            srv.proc.wait(timeout=10)
            _, err = client.communicate(timeout=60)
        finally:
            stop_process(client)
        elapsed = time.monotonic() - started
    stderr = err.decode("utf-8", errors="replace")
    assert client.returncode == 3, stderr
    assert elapsed < 45, f"client took {elapsed:.1f} s after the server died"
    doc = _load(results / "survivor__to-victim_wifi.json")
    assert doc["extra"]["complete"] is False
    run_errors = doc["errors"]
    assert run_errors and run_errors[-1]["failure_type"] == "connection_lost"
    assert "server lost" in run_errors[-1]["message"]
    aborted = [c for c in doc["cases"].values() if c["aborted"]]
    assert len(aborted) == 1
    assert aborted[0]["errors"][-1]["failure_type"] in {str(f) for f in FATAL}
    assert aborted[0]["errors"][-1]["fatal"] is True
    assert len(doc["cases"]) < 5  # later cases skipped, not attempted
    assert _port_is_free(srv.port)


def test_client_against_nothing_exits_3_quickly(tmp_path: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        started = time.monotonic()
        run = subprocess.run(
            _cmd(
                "client", "--server", "127.0.0.1", "--port", str(port), "--link", "wifi",
                "--name", "lonely", "--ceiling-seconds", "0", "--out-dir", str(tmp_path),
                "--connect-timeout", "2",
            ),
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=60, check=False,
        )  # fmt: skip
    assert run.returncode == 3, run.stderr
    assert time.monotonic() - started < 20
    (out,) = tmp_path.glob("lonely__to-*.json")
    doc = _load(out)
    assert doc["cases"] == {}
    assert doc["errors"][0]["failure_type"] == "server_unreachable"


def test_serve_port_in_use_exits_1(tmp_path: Path) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        run = subprocess.run(
            _cmd("serve", "--bind", "127.0.0.1", "--port", str(port)),
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
            timeout=30, check=False,
        )  # fmt: skip
    assert run.returncode == 1
    assert "cannot listen" in run.stderr


def test_serve_idle_limit_exits_0(tmp_path: Path) -> None:
    with server_process(tmp_path, "sleepy", "--idle", "1") as srv:
        assert srv.proc.wait(timeout=20) == 0
        assert "idle limit reached" in srv.log_text()
        port = srv.port
    assert _port_is_free(port)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize(
    ("sig", "message"),
    [(signal.SIGINT, "interrupted"), (signal.SIGTERM, "stop requested")],
)
def test_serve_stops_cleanly_on_signal(tmp_path: Path, sig: int, message: str) -> None:
    with server_process(tmp_path, "sig") as srv:
        # a connected, idle client must not keep the server alive
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5):
            time.sleep(0.3)
            srv.proc.send_signal(sig)
            assert srv.proc.wait(timeout=15) == 0
        assert message in srv.log_text()
        port = srv.port
    assert _port_is_free(port)
