"""Follow a job's logs over the controller's WebSocket from a cluster test.

Imported as ``mf_cluster_ws``. Synchronous (``websockets.sync``) so it runs in a
plain thread beside the harness. Yields ``(line, receive_wall_time)`` for every
complete stdout line, in order, until the end frame.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from typing import Any

from websockets.sync.client import connect

__all__ = ["follow_lines"]


def follow_lines(
    fabric: Any, job_id: str, *, timeout_s: float = 120.0
) -> Iterator[tuple[str, float]]:
    url = fabric.http_base.replace("https://", "wss://") + (
        f"/v1/jobs/{job_id}/logs/ws?stream=stdout"
    )
    headers = {"Authorization": f"Bearer {fabric.api_token()}"}
    pending = b""
    deadline = time.monotonic() + timeout_s
    with connect(
        url, additional_headers=headers, open_timeout=10, ssl=fabric.ssl_context
    ) as ws:
        while time.monotonic() < deadline:
            raw = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
            received = time.time()
            frame = json.loads(raw)
            if frame.get("end"):
                return
            pending += base64.b64decode(frame["data"])
            *complete, pending = pending.split(b"\n")
            for line in complete:
                yield line.decode(errors="replace"), received
    raise AssertionError(f"no end frame within {timeout_s} s")
