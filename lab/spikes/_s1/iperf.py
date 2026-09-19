"""Optional iperf3 cross-check (used only when ``iperf3`` is on PATH).

The operator starts ``iperf3 -s -B <server-ip>`` on the server laptop; the
client runs one upload and one reverse (download) run and records iperf3's
receiver/sender numbers next to ours. Never required: if the binary is missing
the result says so and the spike still counts.
"""

from __future__ import annotations

import json
import math
from typing import Any, Final

from _inventory import runner

DEFAULT_IPERF3_PORT: Final = 5201


def available() -> bool:
    return runner.which("iperf3") is not None


def parse_iperf3_json(text: str) -> dict[str, Any]:
    """Receiver/sender Mbit/s from ``iperf3 -J`` output. Raises ``ValueError``."""
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("iperf3 output is not a JSON object")  # noqa: TRY004
    if doc.get("error"):
        raise ValueError(f"iperf3 error: {doc['error']}")
    end = doc.get("end") or {}
    received = (end.get("sum_received") or {}).get("bits_per_second")
    sent = (end.get("sum_sent") or {}).get("bits_per_second")
    if not isinstance(received, int | float) or not isinstance(sent, int | float):
        raise ValueError(  # noqa: TRY004 (bad content, not a caller type error)
            "iperf3 output has no end.sum_received/sum_sent"
        )
    return {"receiver_mbps": received / 1e6, "sender_mbps": sent / 1e6}


def run_iperf3(
    host: str, port: int, *, seconds: float, reverse: bool, streams: int = 1
) -> dict[str, Any]:
    """One iperf3 client run. Never raises; errors are returned in the dict."""
    base: dict[str, Any] = {
        "direction": "download" if reverse else "upload",
        "streams": streams,
        "port": port,
    }
    if not available():
        return {**base, "ok": False, "error": "iperf3 not found on PATH"}
    secs = max(1, math.ceil(seconds))
    args = ["-c", host, "-p", str(port), "-t", str(secs), "-P", str(streams), "-J"]
    if reverse:
        args.append("-R")
    res = runner.run("iperf3", args, timeout=secs + 20.0, remember_hang=False)
    if res.error is not None:
        return {**base, "ok": False, "error": res.error}
    try:
        parsed = parse_iperf3_json(res.stdout)
    except ValueError as exc:
        return {**base, "ok": False, "error": str(exc)[:500]}
    return {**base, "ok": True, **parsed}
