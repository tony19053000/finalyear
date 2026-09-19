"""Loopback ceiling: the fastest each transport goes on *this* laptop, no network.

Python moves these bytes in user space and gRPC/HTTP add framing on top, so on a
1 Gbit/s link the tool - not the link - may be the limit. Before measuring a
peer, the client starts a second ``serve`` **process** on ``127.0.0.1`` (a real
process, so sender and receiver do not share one GIL, exactly like the two-laptop
run), measures every transport at one size, and stops it. A LAN number close to
its transport's ceiling is tool-limited; the report says so instead of pretending
the link is slow.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from spikes._harness.stats import summarize
from spikes._s3 import payload
from spikes._s3.client import (
    Endpoint,
    grpc_download,
    http_download,
    tcp_download,
)
from spikes._s3.protocol import TransferError

_READY_WAIT_S: Final = 30.0
_STOP_WAIT_S: Final = 5.0
_POLL_S: Final = 0.05

#: The transports measured on loopback (download direction only: it is a ceiling).
CEILING_CASES: Final[
    tuple[tuple[str, Callable[[Endpoint, int, int], dict[str, Any]]], ...]
] = (
    ("tcp-down", tcp_download),
    ("grpc-down", grpc_download),
    ("http-get", http_download),
)


def read_ready_file(path: Path) -> dict[str, int] | None:
    """The ports a ``serve`` process wrote, or ``None`` if not (yet) complete."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    ports = {
        key: doc.get(key)
        for key in ("tcp", "grpc", "http")
        if isinstance(doc.get(key), int) and 0 < int(doc[key]) < 65536
    }
    return (
        {k: int(v) for k, v in ports.items() if v is not None}
        if len(ports) == 3
        else None
    )


def wait_ready(
    path: Path, proc: subprocess.Popen[bytes], timeout_s: float = _READY_WAIT_S
) -> dict[str, int] | None:
    """Poll until the server published its ports, exited, or time ran out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ports = read_ready_file(path)
        if ports is not None:
            return ports
        if proc.poll() is not None:
            return None
        time.sleep(_POLL_S)
    return None


def stop_process(proc: subprocess.Popen[bytes], wait_s: float = _STOP_WAIT_S) -> None:
    """Terminate, then kill after ``wait_s``; always reaps the child."""
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.terminate()
        try:
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=wait_s)


def serve_argv(script: Path, ready: Path, *, duration_s: float) -> list[str]:
    return [
        sys.executable,
        str(script),
        "serve",
        "--bind",
        "127.0.0.1",
        "--port",
        "0",
        "--idle",
        str(max(60.0, duration_s)),
        "--duration",
        str(max(120.0, duration_s * 2)),
        "--ready-file",
        str(ready),
        "--quiet",
    ]


def loopback_ceiling(
    script: Path, *, size: int, chunk: int, reps: int, timeout_s: float = 120.0
) -> dict[str, Any]:
    """Measure every transport on 127.0.0.1. Never raises; failures are recorded."""
    base: dict[str, Any] = {
        "size_bytes": size,
        "chunk_bytes": chunk,
        "reps": reps,
        "bind": "127.0.0.1",
        "crc_rate_mib_s": payload.crc_rate_mib_s(),
    }
    with tempfile.TemporaryDirectory(prefix="s3-ceiling-") as tmp:
        ready = Path(tmp) / "ports.json"
        argv = serve_argv(script, ready, duration_s=timeout_s * 2)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return {**base, "ok": False, "error": f"cannot start server: {exc}"}
        try:
            ports = wait_ready(ready, proc)
            if ports is None:
                return {**base, "ok": False, "error": "loopback server did not start"}
            endpoint = Endpoint(
                "127.0.0.1",
                ports["tcp"],
                ports["grpc"],
                ports["http"],
                transfer_timeout_s=timeout_s,
            )
            results = _measure(endpoint, size, chunk, reps)
        finally:
            stop_process(proc)
    return {**base, "ok": True, "per_transport": results}


def _measure(
    endpoint: Endpoint, size: int, chunk: int, reps: int
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for case_id, measure in CEILING_CASES:
        samples: list[float] = []
        error: str | None = None
        for _ in range(reps + 1):  # one warmup
            try:
                sample = measure(endpoint, size, chunk)
            except TransferError as exc:
                error = f"{exc.failure_type}: {exc}"
                break
            samples.append(float(sample["recv_mbps"]))
        measured = samples[1:] if len(samples) > 1 else samples
        summary = summarize(measured)
        results[case_id] = {
            "n": summary.n,
            "mean_mbps": summary.mean,
            "stdev_mbps": summary.stdev,
            "max_mbps": summary.max,
            "error": error,
        }
    return results
