"""V0.7 G7 criterion 3 (D146): one full fabric run - upload, staging through a
peer, a job with a secret, live logs, an output download, a join code and the
cancel of a second job - with every byte of every link recorded, and **no**
plaintext marker on any of them; plus the **control run** in ``insecure-dev``
(plaintext on loopback), where the same recorder must find the markers -
proof that the recorder really sees the wire (a recorder that saw nothing
would pass the first test too).

Extends ``test_v07_g3_plaintext_recorder.py`` (which stays as it is) with what
that run did not do: declared outputs uploaded by the agent and downloaded by
the user, a cancelled second job, a join code over REST, the structure of every
bearer credential (``Bearer `` on HTTP/1.1 links: API tokens, fetch tokens,
upload grants, peer tokens) and the fixed prefix of every peer token (the
base64 of its canonical JSON), and the insecure-dev control.

Links (``record_links``): each agent's gRPC session, the REST + blob port
(the harness, the CLI upload and the agents' blob traffic all go through it)
and each agent's peer server.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import secrets
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import zstandard
from mf_cluster import ADMIN_USER, NORMAL_USER, Fabric, Node, Timing
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.cluster, pytest.mark.security, pytest.mark.timeout(300)]

TIMING = Timing(
    name="recorder-g7",
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
SECRET_NAME = "RECORDER_KEY"
#: Every peer token starts with the base64url of this (canonical JSON, D98).
PEER_TOKEN_PREFIX = base64.urlsafe_b64encode(b'{"att":"att_')

MAIN = """\
import hashlib, os, sys, time
data = open("data.bin", "rb").read()
print(sys.argv[1], open("secret.txt").read(), len(data), flush=True)
value = open(os.path.join(os.environ["MESHFORGE_SECRETS_DIR"], "RECORDER_KEY"), "rb").read()
open("secret.sha", "w").write(hashlib.sha256(value).hexdigest())
open("result.bin", "wb").write(data[: 64 * 1024])
if sys.argv[2:] == ["sleep"]:
    time.sleep(120)
"""


@dataclass
class Run:
    """What one scenario sent, and what the recorder must (not) see."""

    markers: dict[str, bytes]
    output_archive: bytes
    stdout_b: bytes


def _tree(tmp_path: Path, file_marker: bytes) -> BuiltTree:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(MAIN)
    (root / "secret.txt").write_bytes(file_marker)
    (root / "data.bin").write_bytes(os.urandom(24 * CHUNK))
    return build_tree(root, chunk_size=CHUNK)


def _submit(fabric: Fabric, built: BuiltTree, node: Node, argv: list[str]) -> str:
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        as_user=NORMAL_USER,
        json={
            "manifest": {
                "name": "recorder",
                "entrypoint": ["python", "main.py", *argv],
                "secrets": [SECRET_NAME],
                "outputs": ["result.bin", "secret.sha"],
            },
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": node.node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"])


def _job(fabric: Fabric, job_id: str) -> dict[str, Any]:
    return dict(fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json())


def _until(fabric: Fabric, job_id: str, states: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = _job(fabric, job_id)
        if job["state"] in states:
            return job
        time.sleep(0.05)
    fabric.fail(f"job {job_id} never reached {states}")


def _holds(fabric: Fabric, node: Node) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        detail = fabric.call("GET", f"/v1/nodes/{node.node_id}").json()
        tree = detail.get("cache", {}).get("tree", {"count": 0})
        if detail.get("peer_endpoint") and tree["count"] >= 1:
            return
        time.sleep(0.1)
    fabric.fail(f"{node.name} never reported holding the tree")


def _unpack(archive: bytes) -> dict[str, bytes]:
    raw = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(archive)).read()
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
        for member in tar.getmembers():
            handle = tar.extractfile(member)
            if handle is not None:
                out[member.name] = handle.read()
    return out


def _scenario(
    make_fabric: Callable[..., Fabric], tmp_path: Path, security_mode: str
) -> tuple[Fabric, Run]:
    fabric = make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        agent_extra=AGENT_EXTRA,
        record_links=True,
        security_mode=security_mode,
    )
    a, b = fabric.nodes
    assert fabric.rest_link is not None
    assert a.peer_link is not None and b.peer_link is not None
    argv_done = "MF-ARGV-" + secrets.token_hex(12)
    argv_cancelled = "MF-ARGV-CANCEL-" + secrets.token_hex(12)
    file_marker = b"MF-FILE-" + secrets.token_hex(12).encode()
    secret = b"MF-SECRET-" + secrets.token_hex(12).encode()
    token = fabric.api_token(NORMAL_USER)

    stored = fabric.call(
        "PUT", f"/v1/secrets/{SECRET_NAME}", as_user=NORMAL_USER, content=secret
    )
    assert stored.status_code == 200, stored.text
    invite = fabric.call("POST", "/v1/fabric/invites", json={})
    assert invite.status_code == 201, invite.text
    code = str(invite.json()["code"])

    built = _tree(tmp_path, file_marker)
    client = ControllerClient(
        fabric.http_base,
        timeout_s=30,
        token=SecretStr(token),
        ssl_context=fabric.ssl_context if fabric.scheme == "https" else None,
    )
    try:
        upload_tree(client, built)
    finally:
        client.close()

    first = _until(fabric, _submit(fabric, built, a, [argv_done]), TERMINAL)
    assert first["state"] == "succeeded", first
    _holds(fabric, a)
    fabric.rest_link.bandwidth(256 * 1024)  # B: most bytes from A's peer server
    try:
        second = _until(fabric, _submit(fabric, built, b, [argv_done]), TERMINAL)
    finally:
        fabric.rest_link.bandwidth(None)
    assert second["state"] == "succeeded", second
    assert a.peer_link.counters().bytes_down > 12 * CHUNK

    # The output archive B uploaded, downloaded by its owner over REST.
    digest = str(second["attempts"][-1]["output_digest"])
    archive = fabric.call(
        "GET", f"/v1/blobs/{digest.removeprefix('sha256:')}", as_user=NORMAL_USER
    )
    assert archive.status_code == 200, archive.text
    files = _unpack(archive.content)
    assert files["secret.sha"] == hashlib.sha256(secret).hexdigest().encode()
    assert len(files["result.bin"]) == 64 * 1024
    logs = fabric.call(
        "GET",
        f"/v1/jobs/{second['job_id']}/logs",
        params={"stream": "stdout"},
        as_user=NORMAL_USER,
    )
    assert logs.status_code == 200, logs.text

    # A second job, cancelled while it runs.
    cancel_id = _submit(fabric, built, a, [argv_cancelled, "sleep"])
    _until(fabric, cancel_id, {"running"})
    time.sleep(0.5)  # it printed its argv: live log chunks crossed gRPC
    cancelled = fabric.call("POST", f"/v1/jobs/{cancel_id}/cancel", as_user=NORMAL_USER)
    assert cancelled.status_code in (200, 202), cancelled.text
    assert _until(fabric, cancel_id, TERMINAL)["state"] == "cancelled"

    middle = len(archive.content) // 2
    markers = {
        "argv": argv_done.encode(),
        "argv_cancelled_job": argv_cancelled.encode(),
        "file": file_marker,
        "secret_value": secret,
        "secret_b64": base64.b64encode(secret).rstrip(b"="),
        "secret_hex": secret.hex().encode(),
        "user_password": fabric.password(NORMAL_USER).encode(),
        "user_api_token": token.encode(),
        "admin_password": fabric.password(ADMIN_USER).encode(),
        "admin_api_token": fabric.api_token(ADMIN_USER).encode(),
        "join_code": code.encode(),
        "output_archive": archive.content[middle : middle + 48],
        "bearer_header": b"Bearer ",
        "peer_token": PEER_TOKEN_PREFIX,
    }
    return fabric, Run(markers, archive.content, logs.content)


def _hits(fabric: Fabric, markers: dict[str, bytes]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {name: [] for name in markers}
    for link in fabric.links():
        recording = link.recording()
        assert not recording.truncated, link.name
        for name, marker in markers.items():
            for conn, direction in recording.find(marker):
                hits[name].append(f"{link.name} conn {conn} {direction}")
    return hits


def test_no_marker_of_a_full_run_crosses_any_link_in_mtls(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric, run = _scenario(make_fabric, tmp_path, "mtls")
    # The markers really travelled (inside TLS): B's stdout holds the argv and
    # the file marker, the output archive came back whole.
    assert run.markers["argv"] in run.stdout_b and run.markers["file"] in run.stdout_b
    hits = _hits(fabric, run.markers)
    leaks = {name: where for name, where in hits.items() if where}
    assert leaks == {}, leaks
    names = {link.name for link in fabric.links()}
    assert {"rest", "agent0", "agent1", "agent0-peer", "agent1-peer"} <= names


#: Markers the insecure-dev control run must find. The secret's base64/hex
#: forms are not sent by anyone (only the value travels), so they are absent in
#: both runs and are not a control.
VISIBLE_IN_PLAINTEXT = {
    "argv": {"agent0", "agent1"},  # StartAttempt + live log chunks (gRPC)
    "argv_cancelled_job": {"agent0"},
    "file": {"rest"},  # the tree upload (+ logs read back)
    "secret_value": {"rest", "agent0", "agent1"},  # PUT body; StartAttempt
    "user_password": {"rest"},  # the harness login body
    "user_api_token": {"rest"},
    "admin_password": {"rest"},
    "admin_api_token": {"rest"},
    "join_code": {"rest"},  # the 201 body of POST /v1/fabric/invites
    "output_archive": {"rest"},  # B's upload and the user's download
    "bearer_header": {"rest", "agent0-peer"},
    "peer_token": {"agent0-peer"},  # B's fetches from A's peer server
}


def test_control_run_in_insecure_dev_the_recorder_sees_every_marker(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric, run = _scenario(make_fabric, tmp_path, "insecure-dev")
    hits = _hits(fabric, run.markers)
    missing = {
        name: sorted(links - {h.split(" ")[0] for h in hits[name]})
        for name, links in VISIBLE_IN_PLAINTEXT.items()
        if not links <= {h.split(" ")[0] for h in hits[name]}
    }
    assert missing == {}, (missing, hits)
    # In plaintext the agents' own bearer credentials are readable too: fetch
    # tokens / upload grants on the rest link, peer tokens on the peer link -
    # other values than the two users' API tokens.
    users = {fabric.api_token(NORMAL_USER), fabric.api_token(ADMIN_USER)}
    seen: set[str] = set()
    for link in fabric.links():
        for data in link.recording().streams.values():
            seen.update(
                m.decode() for m in re.findall(rb"Bearer ([A-Za-z0-9_.\-]+)", data)
            )
    assert seen - users, "no agent credential was visible in plaintext"
