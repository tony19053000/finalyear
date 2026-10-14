"""V0.7 G3 deliverable 7 (D146, criterion 3): no plaintext on any link of a
full job run - proved without root by recording every byte on every link.

A real controller and two real agents as processes; **every** network path
goes through a recording :class:`~mf_faultproxy.FaultLink` (``record_links``):
each agent's gRPC session, the REST + blob port (the controller advertises the
"rest" link, the test's own REST calls use it too) and each agent's peer blob
server (``[agent.peer] advertise_port`` = its link).

The run: the test uploads a project whose ``secret.txt`` holds FILE_MARKER,
submits a job whose argv holds ARGV_MARKER (V0.7 G4: as the harness's normal
user - their API token in every REST header, their password in the harness
login that went over the same recorded link), the job prints both (so its log
chunks carry them over gRPC and its stdout comes back over REST), runs on A,
then on B with the controller link throttled so B fetches the tree mostly
**from A's peer server**. Afterwards:

* the markers really travelled: B's stdout (read back over the rest link)
  contains both, A's peer link carried most of the tree;
* not one recorded byte stream - any link, any connection, either direction -
  contains ARGV_MARKER, FILE_MARKER, either user's password or API token, or
  the fabric token; nothing was dropped from the recording (not truncated).

V0.7 G6 (D146 + D144): the user also stores a **job secret** (SECRET_MARKER,
``PUT /v1/secrets`` over the recorded rest link) and both jobs name it; the
job proves it read it by writing the value's SHA-256 next to the test (never
the value) - and neither the value nor its base64/hex is on any link (it
travelled inside ``StartAttempt`` over mTLS).
The 🧑 lab checklist adds a real tcpdump/Wireshark capture on the LAN.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import ADMIN_USER, NORMAL_USER, Fabric, Node, Timing
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.fabric_tls import peer_server_name
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(300)]

TIMING = Timing(
    name="recorder",
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
AGENT_EXTRA = {
    "peer": {"bind": "127.0.0.1", "port": 0},
    "cache": {"flush_s": 0.2},
    "fetch": {"per_source": 2, "request_timeout_s": 10.0, "endgame_after_s": 600.0},
}
CHUNK = 256 * 1024
TERMINAL = {"succeeded", "failed", "cancelled"}

ARGV_MARKER = "MF-ARGV-" + secrets.token_hex(12)
FILE_MARKER = b"MF-FILE-" + secrets.token_hex(12).encode()
SECRET_MARKER = b"MF-SECRET-" + secrets.token_hex(12).encode()
SECRET_NAME = "RECORDER_KEY"

MAIN = """\
import hashlib, os, sys
data = open("data.bin", "rb").read()
secret = open("secret.txt").read()
print(sys.argv[1], secret, len(data), flush=True)
open(sys.argv[2], "w").write(secret)
job_secret = open(os.path.join(os.environ["MESHFORGE_SECRETS_DIR"], "RECORDER_KEY"), "rb").read()
open(sys.argv[2] + ".sha", "w").write(hashlib.sha256(job_secret).hexdigest())
"""


def _tree(tmp_path: Path) -> BuiltTree:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(MAIN)
    (root / "secret.txt").write_bytes(FILE_MARKER)
    (root / "data.bin").write_bytes(os.urandom(24 * CHUNK))
    return build_tree(root, chunk_size=CHUNK)


def _run(fabric: Fabric, built: BuiltTree, node: Node, out: Path) -> dict[str, Any]:
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        as_user=NORMAL_USER,
        json={
            "manifest": {
                "name": "recorder",
                "entrypoint": ["python", "main.py", ARGV_MARKER, str(out)],
                "secrets": [SECRET_NAME],
            },
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": node.node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = str(resp.json()["job_id"])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = dict(fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json())
        if job["state"] in TERMINAL:
            return job
        time.sleep(0.05)
    fabric.fail(f"job {job_id} did not finish")


def _holds(fabric: Fabric, node: Node) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        detail = fabric.call("GET", f"/v1/nodes/{node.node_id}").json()
        tree = detail.get("cache", {}).get("tree", {"count": 0})
        if detail.get("peer_endpoint") and tree["count"] >= 1:
            return
        time.sleep(0.1)
    fabric.fail(f"{node.name} never reported holding the tree")


@pytest.mark.security
def test_no_plaintext_marker_crosses_any_link_of_a_full_job_run(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        agent_extra=AGENT_EXTRA,
        record_links=True,
    )
    a, b = fabric.nodes
    assert fabric.rest_link is not None
    assert a.peer_link is not None and b.peer_link is not None
    token = fabric.api_token(NORMAL_USER)

    stored = fabric.call(
        "PUT",
        f"/v1/secrets/{SECRET_NAME}",
        as_user=NORMAL_USER,
        content=SECRET_MARKER,
    )
    assert stored.status_code == 200, stored.text

    built = _tree(tmp_path)
    client = ControllerClient(fabric.http_base, timeout_s=30, token=SecretStr(token))
    try:
        upload_tree(client, built)
    finally:
        client.close()
    first = _run(fabric, built, a, tmp_path / "a.out")
    assert first["state"] == "succeeded", first
    _holds(fabric, a)
    fabric.rest_link.bandwidth(256 * 1024)  # B: most bytes from A's peer server
    try:
        second = _run(fabric, built, b, tmp_path / "b.out")
    finally:
        fabric.rest_link.bandwidth(None)
    assert second["state"] == "succeeded", second
    assert (tmp_path / "b.out").read_bytes() == FILE_MARKER
    # The job really read the secret (both runs), proved without the value.
    for out in ("a.out.sha", "b.out.sha"):
        assert (tmp_path / out).read_text() == hashlib.sha256(SECRET_MARKER).hexdigest()

    # The markers really travelled (encrypted): B's stdout, read back over
    # the rest link, holds both; A's peer link carried most of the tree.
    logs = fabric.call(
        "GET",
        f"/v1/jobs/{second['job_id']}/logs",
        params={"stream": "stdout"},
        as_user=NORMAL_USER,
    )
    assert logs.status_code == 200, logs.text
    assert ARGV_MARKER.encode() in logs.content and FILE_MARKER in logs.content
    assert a.peer_link.counters().bytes_down > 12 * CHUNK

    markers = {
        "argv": ARGV_MARKER.encode(),
        "file": FILE_MARKER,
        "fabric_token": fabric.fabric_token().encode(),
        # V0.7 G4: both users' passwords (sent by the harness login over the
        # recorded rest link) and API tokens (every REST header).
        "user_password": fabric.password(NORMAL_USER).encode(),
        "user_api_token": token.encode(),
        "admin_password": fabric.password(ADMIN_USER).encode(),
        "admin_api_token": fabric.api_token(ADMIN_USER).encode(),
        # V0.7 G6: the job secret's value and its common encodings.
        "secret_value": SECRET_MARKER,
        "secret_b64": base64.b64encode(SECRET_MARKER).rstrip(b"="),
        "secret_hex": SECRET_MARKER.hex().encode(),
    }
    seen: dict[str, int] = {}
    leaks: list[str] = []
    for link in fabric.links():
        recording = link.recording()
        assert not recording.truncated, link.name
        seen[link.name] = recording.total_bytes
        for name, marker in markers.items():
            for conn, direction in recording.find(marker):
                leaks.append(f"{name} on {link.name} conn {conn} {direction}")
    assert leaks == [], leaks
    # Positive control - the recording is the real wire: the one plaintext a
    # TLS 1.3 client sends on purpose, the server name (SNI) B asked A's peer
    # server for, is found on A's peer link. (Node ids are not secrets; the
    # THREAT_MODEL lists this as accepted.)
    sni = peer_server_name(a.node_id).encode()
    assert (a.peer_link.recording().find(sni) or [(0, "")])[0][1] == "up"
    # Every kind of link was recorded and carried traffic.
    expected = {"rest", "agent0", "agent1", "agent0-peer", "agent1-peer"}
    assert expected <= set(seen), seen
    for name in ("rest", "agent0", "agent1", "agent0-peer"):
        assert seen[name] > 0, seen
