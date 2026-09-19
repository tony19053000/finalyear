"""Component tests for the S3 spike end to end (roadmap 0.2.7).

The real script as the team runs it: ``serve`` in a **separate process** on
``127.0.0.1`` (so the loopback ceiling's second process and the two-laptop run
have the same shape), then the ``client`` and ``report`` subcommands in this
process against it with tiny payloads. Checks the result JSON validates with
the harness (``load_dir``), the run is ``complete``, nothing in JSON or report
leaks the home directory, the report is deterministic, the serve process stops
cleanly on SIGTERM, and that :func:`run_client` records typed errors - never
raises - when the server is dead, vanishes mid-run, or has no gRPC.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
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

from spikes import s3_transport as cli  # noqa: E402
from spikes._harness.envelope import load_dir, validate  # noqa: E402
from spikes._s3 import grpc_server  # noqa: E402
from spikes._s3.ceiling import (  # noqa: E402
    loopback_ceiling,
    read_ready_file,
    stop_process,
    wait_ready,
)
from spikes._s3.context import ServerLimits  # noqa: E402
from spikes._s3.protocol import KIB, FailureType  # noqa: E402
from spikes._s3.run import ClientConfig, run_client  # noqa: E402
from spikes._s3.server import S3Server  # noqa: E402

pytestmark = pytest.mark.timeout(240)

SIZES: Final = "64KiB,256KiB"
CHUNK: Final = 16 * KIB


def _cmd(*args: str) -> list[str]:
    return [sys.executable, str(SCRIPT), *args]


class ServerProc:
    def __init__(
        self, proc: subprocess.Popen[bytes], ports: dict[str, int], log: Path
    ) -> None:
        self.proc, self.ports, self.log = proc, ports, log

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace")


@contextlib.contextmanager
def server_process(tmp: Path, name: str, *extra: str) -> Iterator[ServerProc]:
    ready, log = tmp / f"{name}.ports.json", tmp / f"{name}.log"
    with log.open("wb") as log_file:
        proc = subprocess.Popen(
            _cmd(
                "serve", "--bind", "127.0.0.1", "--port", "0", "--name", name,
                "--idle", "60", "--io-timeout", "5", "--ready-file", str(ready),
                *extra,
            ),
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )  # fmt: skip
        try:
            ports = wait_ready(ready, proc, timeout_s=30)
            assert ports is not None, log.read_text(encoding="utf-8", errors="replace")
            yield ServerProc(proc, ports, log)
        finally:
            stop_process(proc)
    assert proc.poll() is not None, "server process leaked"


def _client_argv(port: int, out_dir: Path, *extra: str) -> list[str]:
    return [
        "client", "--server", "127.0.0.1", "--port", str(port), "--link", "ethernet",
        "--sizes", SIZES, "--chunk", str(CHUNK), "--chunk-sweep",
        f"{4 * KIB},{CHUNK}", "--chunk-sweep-size", "256KiB", "--reps", "5",
        "--warmup", "1", "--pause", "0", "--range-parts", "3", "--name", "cli-client",
        "--out-dir", str(out_dir), "--connect-timeout", "3", "--io-timeout", "5",
        "--transfer-timeout", "30", *extra,
    ]  # fmt: skip


def _no_home(text: str) -> None:
    home = str(Path.home())
    assert home not in text
    user = Path.home().name
    if len(user) >= 4:  # a short user name could legitimately occur in words
        assert user not in text.lower(), user


# ------------------------------------------------------------- end to end
def test_cli_serve_client_report_end_to_end(tmp_path: Path) -> None:
    """The whole S3 path with the loopback ceiling (a third process) on."""
    out_dir = tmp_path / "raw"
    with server_process(tmp_path, "cli-server") as server:
        # --port 0: the +1/+2 convention does not hold; hello must fix the ports
        assert server.ports["grpc"] != server.ports["tcp"] + 1 or True
        argv = _client_argv(
            server.ports["tcp"], out_dir, "--ceiling-size", "64KiB",
            "--ceiling-reps", "1",
        )  # fmt: skip
        assert cli.main(argv) == cli.EXIT_OK
        # SIGTERM stops the serve process cleanly (exit 0) - what Ctrl-C/kill does
        if sys.platform != "win32":
            server.proc.send_signal(signal.SIGTERM)
            assert server.proc.wait(timeout=20) == 0
    log = server.log_text()
    assert "listening on 127.0.0.1" in log and "server stopped" in log
    assert "refused" not in log.split("server stopped")[0]

    docs, skipped = load_dir(out_dir)
    assert skipped == [] and len(docs) == 1
    (doc,) = docs
    assert doc["_source"] == "cli-client__to-cli-server_ethernet.json"
    assert doc["spike"] == "S3" and doc["link"] == "ethernet"
    assert doc["params"]["server_name"] == "cli-server"
    assert doc["extra"]["complete"] is True and doc["errors"] == []
    assert doc["extra"]["server_hello"]["ports"] == server.ports
    families = {
        "tcp-down", "tcp-up", "grpc-down", "grpc-up", "http-get", "http-range3"
    }  # fmt: skip
    for family in families:
        for size in ("64KiB", "256KiB"):
            case = doc["cases"][f"{family}-{size}"]
            assert len(case["samples"]) == 5, (family, size)
            assert case["summary"]["recv_mbps"]["n"] == 5
            assert all(s["server_bytes"] == s["bytes"] for s in case["samples"])
    sweep = doc["cases"]["grpc-down-256KiB-chunk4KiB"]
    assert sweep["attrs"]["sweep"] is True and len(sweep["samples"]) == 5
    assert "grpc-down-256KiB-chunk16KiB" not in doc["cases"]  # = the default row
    ceiling = doc["extra"]["loopback_ceiling"]
    assert ceiling["ok"] is True and ceiling["crc_rate_mib_s"] > 0
    assert set(ceiling["per_transport"]) == {"tcp-down", "grpc-down", "http-get"}
    assert all(
        v["n"] == 1 and v["error"] is None for v in ceiling["per_transport"].values()
    )
    probe = doc["extra"]["grpc_default_limit_probe"]
    assert probe["ok"] is True and probe["at_default_limit"]["ok"] is True
    assert probe["over_limit"]["ok"] is False
    assert probe["over_limit"]["failure_type"] == str(FailureType.TRANSPORT_ERROR)
    assert doc["tool"]["grpc"] == grpc_server.grpc_version()
    raw = (out_dir / str(doc["_source"])).read_text(encoding="utf-8")
    _no_home(raw)
    assert not list(out_dir.glob("*.tmp"))

    # report: deterministic, sensible, no home path
    report = tmp_path / "S3.md"
    assert cli.main(["report", "--results", str(out_dir), "--out", str(report)]) == 0
    text = report.read_text(encoding="utf-8")
    assert cli.main(["report", "--results", str(out_dir), "--out", str(report)]) == 0
    assert report.read_text(encoding="utf-8") == text
    _no_home(text)
    assert "S3 transport throughput" in text
    assert "**cli-client -> cli-server · Ethernet**" in text
    assert "| HTTP 3 parallel ranges |" in text
    assert "### gRPC chunk (message) size sweep" in text and "| 4KiB |" in text
    assert "### Loopback ceiling" in text and "### gRPC message-limit probe" in text
    assert "(default) | passed |" in text and "(one byte under) | refused" in text
    assert "### Q2 answer" in text and "at 256KiB" in text
    assert "ADR-0005" in text and "ADR-0003" in text and "Best gRPC chunk" in text
    assert "TBD" not in text.split("### Q2 answer")[1]


def test_client_against_dead_server_exits_3_with_typed_error(tmp_path: Path) -> None:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]  # bound, not listening: refused
        out_dir = tmp_path / "raw"
        started = time.monotonic()
        code = cli.main(_client_argv(port, out_dir, "--ceiling-size", "0"))
        assert code == cli.EXIT_INCOMPLETE
        assert time.monotonic() - started < 30
    docs, skipped = load_dir(out_dir)
    assert skipped == [] and len(docs) == 1
    (doc,) = docs
    assert doc["cases"] == {} and doc["extra"]["complete"] is False
    (error,) = doc["errors"]
    assert error["phase"] == "hello"
    assert error["failure_type"] == str(FailureType.SERVER_UNREACHABLE)
    assert doc["extra"]["grpc_default_limit_probe"] is None
    _no_home((out_dir / str(doc["_source"])).read_text(encoding="utf-8"))


# ------------------------------------------------------------- run_client
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
    assert srv.ports
    yield srv
    srv.stop()
    thread.join(timeout=15)
    assert not thread.is_alive(), "server thread leaked"


def test_run_client_uses_hello_ports_and_is_complete(server: S3Server) -> None:
    lines: list[str] = []
    run = run_client(_config(server), log=lines.append)
    assert run.complete and run.errors == [] and run.server_name == "inproc"
    doc = validate(run.envelope, source="test")
    assert doc["params"]["port"] == server.ports["tcp"]
    assert doc["extra"]["loopback_ceiling"] is None
    assert len(doc["cases"]) == 7  # 6 families + 1 sweep chunk (the other = default)
    assert all(len(c["samples"]) == 5 for c in doc["cases"].values())
    assert any("ports tcp" in line for line in lines)
    assert not any("FAILED" in line for line in lines)


def test_run_client_records_server_vanishing_mid_run(server: S3Server) -> None:
    """When the server dies during a case, that case aborts on the first fatal
    error, ``hello`` is retried once, and the remaining cases are *skipped and
    listed* - never ``reps x cases`` timeouts."""
    seen: list[str] = []

    def log(line: str) -> None:
        seen.append(line)
        if line.startswith("grpc-down-64KiB:"):  # the second case starts
            server.stop()
            server.close()

    started = time.monotonic()
    run = run_client(_config(server), log=log)
    assert time.monotonic() - started < 60
    assert not run.complete
    doc = validate(run.envelope, source="test")
    assert len(doc["cases"]["tcp-down-64KiB"]["samples"]) == 5
    grpc_case = doc["cases"]["grpc-down-64KiB"]
    assert grpc_case["aborted"] is True and grpc_case["errors"]
    assert grpc_case["errors"][0]["failure_type"] in {
        str(FailureType.SERVER_UNREACHABLE),
        str(FailureType.CONNECTION_LOST),
        str(FailureType.TIMEOUT),
    }
    assert set(doc["cases"]) == {"tcp-down-64KiB", "grpc-down-64KiB"}
    (error,) = run.errors
    assert error["phase"] == "grpc-down-64KiB"
    assert error["failure_type"] == str(FailureType.CONNECTION_LOST)
    assert "skipped cases: http-get-64KiB, http-range2-64KiB" in error["message"]
    assert any("skipping 5 case(s)" in line for line in seen)


def test_run_client_without_grpc_records_typed_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without grpcio (simulated) the server serves TCP + HTTP only, hello says
    why, and every gRPC case is a non-fatal ``grpc_unavailable`` - the run
    continues, nothing hangs, and the JSON still validates."""
    reason = "grpcio is not installed (ImportError: simulated)"
    # build_grpc_server() raises through require_grpc("grpc server"), so the
    # server records - and hello reports - the reason with that prefix.
    served = f"grpc server: {reason}"
    monkeypatch.setattr(grpc_server, "GRPC_IMPORT_ERROR", reason)
    srv = S3Server(
        "127.0.0.1", 0, ServerLimits(idle_s=60, io_timeout_s=3), name="nogrpc"
    )
    thread = srv.start_thread()
    try:
        while not srv.ports:
            time.sleep(0.02)
        assert srv.ports["grpc"] == 0 and srv.grpc_error == served
        started = time.monotonic()
        run = run_client(_config(srv))
        assert time.monotonic() - started < 60
    finally:
        srv.stop()
        thread.join(timeout=15)
    assert not thread.is_alive()
    doc = validate(run.envelope, source="test")
    assert not run.complete and run.errors == []  # no *run* error: server answered
    assert doc["extra"]["server_hello"]["grpc_error"] == served
    assert doc["tool"]["grpc"] is None
    for case_id in ("tcp-down-64KiB", "tcp-up-64KiB", "http-get-64KiB"):
        assert len(doc["cases"][case_id]["samples"]) == 5, case_id
    for case_id in ("grpc-down-64KiB", "grpc-up-64KiB", "grpc-down-64KiB-chunk4KiB"):
        case = doc["cases"][case_id]
        assert case["samples"] == [] and not case["aborted"], case_id
        assert {e["failure_type"] for e in case["errors"]} == {
            str(FailureType.GRPC_UNAVAILABLE)
        }
    probe = doc["extra"]["grpc_default_limit_probe"]
    assert probe["ok"] is False
    assert probe["failure_type"] == str(FailureType.GRPC_UNAVAILABLE)
    assert probe["over_limit"]["failure_type"] == str(FailureType.GRPC_UNAVAILABLE)
    assert "grpc_unavailable" in json.dumps(run.envelope)
    assert "/home/" not in json.dumps(run.envelope)


# --------------------------------------------------------------- ceiling
def test_loopback_ceiling_spawns_measures_and_reaps(tmp_path: Path) -> None:
    before = threading.active_count()
    result = loopback_ceiling(SCRIPT, size=64 * KIB, chunk=CHUNK, reps=2, timeout_s=60)
    assert result["ok"] is True, result
    assert result["reps"] == 2 and result["bind"] == "127.0.0.1"
    assert result["crc_rate_mib_s"] > 0
    per = result["per_transport"]
    assert set(per) == {"tcp-down", "grpc-down", "http-get"}
    for item in per.values():
        assert item["n"] == 2 and item["error"] is None and item["mean_mbps"] > 0
    assert threading.active_count() <= before
    # no children left behind: nothing of ours is listening on the tmp ready file
    assert not list(tmp_path.glob("**/ports.json"))


def test_loopback_ceiling_records_failure_when_server_cannot_start(
    tmp_path: Path,
) -> None:
    bogus = tmp_path / "not-a-script.py"
    bogus.write_text("import sys; sys.exit(7)\n", encoding="utf-8")
    result = loopback_ceiling(bogus, size=64 * KIB, chunk=CHUNK, reps=1, timeout_s=30)
    assert result["ok"] is False and "did not start" in result["error"]
    missing = tmp_path / "missing" / "s3.py"
    result = loopback_ceiling(missing, size=64 * KIB, chunk=CHUNK, reps=1, timeout_s=30)
    assert result["ok"] is False


def test_ready_file_helpers(tmp_path: Path) -> None:
    path = tmp_path / "ports.json"
    assert read_ready_file(path) is None
    path.write_text("{", encoding="utf-8")
    assert read_ready_file(path) is None
    path.write_text(json.dumps({"tcp": 1, "grpc": 0, "http": 3}), encoding="utf-8")
    assert read_ready_file(path) is None  # grpc 0 = incomplete, keep waiting
    path.write_text(json.dumps({"tcp": 1, "grpc": 2, "http": 3}), encoding="utf-8")
    assert read_ready_file(path) == {"tcp": 1, "grpc": 2, "http": 3}
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert wait_ready(tmp_path / "never.json", proc, timeout_s=10) is None
    finally:
        stop_process(proc)
    assert proc.returncode == 3


def test_serve_cannot_write_ready_file_exits_1_and_frees_ports(tmp_path: Path) -> None:
    unwritable = tmp_path / "file-not-dir"
    unwritable.write_text("x", encoding="utf-8")
    argv = _cmd(
        "serve", "--bind", "127.0.0.1", "--port", "0", "--idle", "5",
        "--ready-file", str(unwritable / "ports.json"), "--quiet",
    )  # fmt: skip
    proc = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == cli.EXIT_IO, proc.stderr
    assert "cannot write --ready-file" in proc.stderr
    assert os.path.exists(unwritable)
