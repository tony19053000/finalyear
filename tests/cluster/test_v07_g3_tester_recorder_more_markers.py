"""V0.7 G3 tester: more markers for the D146 plaintext recorder.

The implementer's run proves the argv marker, a project-file marker and the
fabric token never cross a link in clear. Criterion 3 says "no plaintext on any
plane"; this adds the planes and secrets that run does not touch:

* a stderr line and a **job output file** (the agent's upload body, PUT to
  ``/v1/blobs`` with an upload grant) and the job name;
* the **WebSocket log stream** (``wss``), followed live by the CLI's own
  ``follow_logs`` over the recorded REST link;
* **secret-ish file content that must never travel at all**: the PEM bodies of
  every agent's ``node.key``, the controller's ``server.key`` and the CA key
  (nothing but certificates and CSRs may cross a link);
* the same markers as **JSON-escaped / base64** forms (the log WebSocket
  base64-encodes chunk data, so a plaintext leak could hide in that form).
"""

from __future__ import annotations

import base64
import io
import secrets
import ssl
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient
from meshforge.cli.follow import follow_logs, ws_url
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(300)]

TIMING = Timing(
    name="recorder2",
    heartbeat_interval_s=0.5,
    miss_allowance_s=15.0,
    lease_multiplier=60,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=2.0,
)
EXEC = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}
TERMINAL = {"succeeded", "failed", "cancelled"}

STDERR_MARKER = "MF-ERR-" + secrets.token_hex(12)
OUTPUT_MARKER = "MF-OUT-" + secrets.token_hex(12)
STDOUT_MARKER = "MF-LOG-" + secrets.token_hex(12)
NAME_MARKER = "mf-name-" + secrets.token_hex(8)

MAIN = f"""\
import os, sys, time
time.sleep(1.5)  # the websocket follower connects first
print("{STDOUT_MARKER}", flush=True)
print("{STDERR_MARKER}", file=sys.stderr, flush=True)
os.makedirs("results", exist_ok=True)
open("results/r.txt", "w").write("{OUTPUT_MARKER}")
time.sleep(0.5)
"""


def _pem_body_lines(path: Path) -> list[bytes]:
    lines = [
        ln.strip()
        for ln in path.read_bytes().splitlines()
        if ln.strip() and not ln.startswith(b"-----")
    ]
    return [ln for ln in lines if len(ln) >= 16]


def test_more_markers_never_cross_a_link_in_clear(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        agent_extra={"peer": {"bind": "127.0.0.1", "port": 0}},
        record_links=True,
    )
    a, b = fabric.nodes
    token = fabric.api_token()
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(MAIN)
    built: BuiltTree = build_tree(root, chunk_size=256 * 1024)
    client = ControllerClient(fabric.http_base, timeout_s=30, token=SecretStr(token))
    try:
        upload_tree(client, built)
    finally:
        client.close()

    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": {
                "name": NAME_MARKER,
                "entrypoint": ["python", "main.py"],
                "outputs": ["results/"],
            },
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": b.node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = str(resp.json()["job_id"])

    # follow the live log over wss on the recorded rest link
    ctx = ssl.create_default_context(cafile=str(fabric.ca_dir / "ca.crt"))
    out, err = io.BytesIO(), io.BytesIO()
    end = follow_logs(
        ws_url(fabric.http_base, job_id, stream="both"),
        auth_header=f"Bearer {token}",
        outputs={"stdout": out, "stderr": err},
        open_timeout_s=20,
        ssl_context=ctx,
    )
    assert end.get("end") is True, end
    deadline = time.monotonic() + 60
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        job = dict(fabric.call("GET", f"/v1/jobs/{job_id}").json())
        if job["state"] in TERMINAL:
            break
        time.sleep(0.1)
    assert job["state"] == "succeeded", job
    # the markers really travelled (encrypted): the wss follower received them
    assert STDOUT_MARKER.encode() in out.getvalue()
    assert STDERR_MARKER.encode() in err.getvalue()
    # ... and the uploaded output blob is on the controller
    digest = job["attempts"][-1]["output_digest"] if job.get("attempts") else None
    assert digest, job

    markers: dict[str, bytes] = {
        "stdout": STDOUT_MARKER.encode(),
        "stderr": STDERR_MARKER.encode(),
        "output_file": OUTPUT_MARKER.encode(),
        "job_name": NAME_MARKER.encode(),
        "api_token": token.encode(),
        "api_token_b64": base64.b64encode(token.encode()),
        "stdout_b64": base64.b64encode(STDOUT_MARKER.encode())[:-4],
        "output_b64": base64.b64encode(OUTPUT_MARKER.encode())[:-4],
    }
    key_files = {
        "ca.key": fabric.ca_dir / "ca.key",
        "server.key": fabric.ca_dir.parent / "tls" / "server.key",
        "agent0.key": a.data_dir / "identity" / "node.key",
        "agent1.key": b.data_dir / "identity" / "node.key",
    }
    for name, path in key_files.items():
        assert path.exists(), (name, path)
        for i, line in enumerate(_pem_body_lines(path)[:3]):
            markers[f"{name}#{i}"] = line

    leaks: list[str] = []
    seen_bytes = 0
    for link in fabric.links():
        recording = link.recording()
        assert not recording.truncated, link.name
        seen_bytes += recording.total_bytes
        for name, marker in markers.items():
            for conn, direction in recording.find(marker):
                leaks.append(f"{name} on {link.name} conn {conn} {direction}")
    assert seen_bytes > 0
    assert leaks == [], leaks
