"""Loopback ceiling: what this tool can move on *this* host with no network.

Python counts bytes in user space, so on a fast link (>1 Gbit/s) the tool itself
may be the bottleneck. Before measuring a peer, the client starts a separate
server *process* on ``127.0.0.1`` (a separate process, so sender and receiver do
not share one GIL, like in the real two-laptop run), measures one TCP stream,
and stops the server. A LAN result close to this ceiling is tool-limited, not
link-limited (``matrix`` flags such cells).
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Final

from spikes._s1.client import Endpoint, tcp_throughput
from spikes._s1.protocol import MeasureError

_READY_WAIT_S: Final = 20.0
_STOP_WAIT_S: Final = 5.0
_POLL_S: Final = 0.05


def read_ready_file(path: Path) -> int | None:
    """The port a server wrote to its ready file, or None if not (yet) valid."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if text.isdigit() and 0 < int(text) < 65536 else None


def wait_ready(
    path: Path, proc: subprocess.Popen[bytes], timeout_s: float = _READY_WAIT_S
) -> int | None:
    """Poll ``path`` until the server wrote its port, it exited, or time ran out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        port = read_ready_file(path)
        if port is not None:
            return port
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


def loopback_ceiling(script: Path, seconds: float) -> dict[str, Any]:
    """Measure one loopback TCP upload stream for ``seconds``. Never raises."""
    base: dict[str, Any] = {"seconds": seconds, "streams": 1, "bind": "127.0.0.1"}
    with tempfile.TemporaryDirectory(prefix="s1-ceiling-") as tmp:
        ready = Path(tmp) / "port"
        argv = [
            sys.executable,
            str(script),
            "serve",
            "--bind",
            "127.0.0.1",
            "--port",
            "0",
            "--idle",
            "30",
            "--duration",
            str(max(60.0, seconds * 4 + 30)),
            "--ready-file",
            str(ready),
            "--quiet",
        ]
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
            port = wait_ready(ready, proc)
            if port is None:
                return {**base, "ok": False, "error": "loopback server did not start"}
            sample = tcp_throughput(
                Endpoint("127.0.0.1", port),
                direction="upload",
                streams=1,
                seconds=seconds,
            )
        except MeasureError as exc:
            return {**base, "ok": False, "error": f"{exc.failure_type}: {exc}"}
        finally:
            stop_process(proc)
    return {
        **base,
        "ok": True,
        "receiver_mbps": sample["receiver_mbps"],
        "sender_mbps": sample["sender_mbps"],
    }
