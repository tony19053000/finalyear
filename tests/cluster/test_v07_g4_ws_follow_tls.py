"""V0.7 G4 item 0 (D156 e): the ``compute run -f`` / ``logs -f`` flake.

**Cause (reproduced before the fix, about one open in three):**
``websockets.sync`` reads the socket in a background thread while the
caller's thread writes the handshake request. Over TLS that is two threads
inside one OpenSSL connection at once, which OpenSSL does not support; with
TLS 1.3 the server's post-handshake session tickets are processed by the
reader exactly while the request is being written, and the request or the
response was lost - ``TimeoutError: timed out while waiting for handshake
response`` (the G3 cluster flake in ``test_fabric_outputs_logs``). A raw TLS
socket used from one thread never hung (30/30), the asyncio client never hung
(150/150), and with session tickets off on the server the sync client did not
hang either (60/60).

**Fix:** the CLI follows with the asyncio client (one thread drives the TLS
object), and the controller's (and the peer servers') TLS 1.3 contexts send
no session tickets (nothing resumes sessions).

This test opens the log stream many times on a real fabric, both with the
threaded ``websockets.sync`` client (the shape that hung - guarded by the
server-side fix) and through ``compute logs -f`` (the CLI's own client), each
with a 10 s open timeout. Before the fix it failed within the first few opens.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric
from test_fabric_outputs_logs import EXAMPLES, _compute, _fabric

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(300)]

SYNC_OPENS = 40
CLI_FOLLOWS = 10


def test_log_stream_opens_never_hang_over_tls(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    from websockets.sync.client import connect

    fabric = _fabric(make_fabric)
    done = _compute(
        fabric,
        fabric.nodes[0].data_dir,
        "run",
        str(EXAMPLES / "cpu-deterministic"),
        "--node",
        fabric.nodes[1].node_id,
        "-f",
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    job_id = done.stdout.splitlines()[0].strip()
    url = fabric.http_base.replace("https://", "wss://") + (
        f"/v1/jobs/{job_id}/logs/ws?stream=both"
    )
    headers = {"Authorization": f"Bearer {fabric.fabric_token()}"}
    slowest = 0.0
    for i in range(SYNC_OPENS):
        started = time.monotonic()
        with connect(
            url, additional_headers=headers, open_timeout=10, ssl=fabric.ssl_context
        ) as ws:
            slowest = max(slowest, time.monotonic() - started)
            ended = False
            for raw in ws:
                if '"end"' in (raw if isinstance(raw, str) else raw.decode()):
                    ended = True
                    break
            assert ended, f"open {i}: no end frame"
    for i in range(CLI_FOLLOWS):
        follow = _compute(
            fabric, fabric.nodes[0].data_dir, "logs", "-f", job_id, cwd=tmp_path
        )
        assert follow.returncode == 0, (i, follow.stderr[-2000:])
    summary = (
        f"{SYNC_OPENS} sync opens (slowest {slowest:.3f} s), {CLI_FOLLOWS} follows"
    )
    print(summary)  # noqa: T201
