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
(the harness, the CLI upload and the agents' blob traffic all go through it),
each agent's peer server and (fix round 1, D167 TQ-5) the enrolment listener
47101: in the mtls run the join code is **redeemed** by a real
``meshforge-agent join`` through that link (its code names the link), and
every byte on it must be TLS records - no join code, token or CSR. The
insecure-dev control cannot cover it: ``Enroll`` exists only with TLS (no
listener in insecure-dev), so the mtls run asserts the record structure
instead of a control.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import secrets
import subprocess
import sys
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
from meshforge.common import join_code
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
    joined_node: str | None = None


def _join(fabric: Fabric, tmp_path: Path, code: str) -> str:
    """Redeem ``code`` with a real ``meshforge-agent join -`` (code on stdin)
    for a fresh laptop; the code names the recorded "enroll" link. Returns
    the new node id (enrolled, its invite used by it)."""
    data_dir = tmp_path / "joiner-data"
    config = tmp_path / "joiner.toml"
    config.write_text(f"data_dir = {json.dumps(str(data_dir))}\n", "utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["PYTHONUTF8"] = "1"
    done = subprocess.run(
        [sys.executable, "-m", "meshforge.agent", "join", "-", "--config", str(config)],
        input=code + "\n", env=env, capture_output=True, text=True, timeout=60,
        check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    shown = subprocess.run(
        [sys.executable, "-m", "meshforge.agent", "identity", "show", "--json",
         "--config", str(config)],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    facts = json.loads(shown.stdout)
    assert facts["enrolled"] is True, facts
    node_id = str(facts["node_id"])
    invites = fabric.call("GET", "/v1/fabric/invites").json()["invites"]
    assert node_id in {i["used_by_node"] for i in invites}, invites
    return node_id


#: TLS record content types: change_cipher_spec, alert, handshake, application_data.
_TLS_TYPES = frozenset({20, 21, 22, 23})
_TLS_MAX_RECORD = 2**14 + 256


def _tls_problems(data: bytes) -> list[str]:
    """Walk ``data`` as a sequence of whole TLS records; every way it is not
    one (wrong type, version, oversized, trailing bytes) is a problem."""
    out: list[str] = []
    i = 0
    while i < len(data):
        header = data[i : i + 5]
        if len(header) < 5:
            out.append(f"truncated record header at {i}")
            break
        kind, major, minor = header[0], header[1], header[2]
        length = int.from_bytes(header[3:5], "big")
        if kind not in _TLS_TYPES or major != 3 or minor not in (1, 3):
            out.append(f"not a TLS record at {i}: {header[:3].hex()}")
            break
        if length > _TLS_MAX_RECORD or i + 5 + length > len(data):
            out.append(f"bad record length {length} at {i}")
            break
        i += 5 + length
    return out


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
    joined_node: str | None = None
    if security_mode == "mtls":
        assert fabric.enroll_link is not None
        assert fabric.enroll_target == fabric.enroll_link.target
        joined_node = _join(fabric, tmp_path, code)

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
    if joined_node is not None:
        markers |= {
            # What a plaintext Enroll would carry: the 16 token bytes, the CSR
            # (PEM; its node id is inside base64, so the PEM armour is the
            # readable part) and the issued certificate's armour.
            "join_token": join_code.decode(code).token,
            "join_token_hex": join_code.decode(code).token.hex().encode(),
            "csr_pem": b"-----BEGIN CERTIFICATE REQUEST-----",
            "cert_pem": b"-----BEGIN CERTIFICATE-----",
            "joined_node_id": joined_node.encode(),
        }
    return fabric, Run(markers, archive.content, logs.content, joined_node)


def test_the_tls_record_walker_refuses_plaintext() -> None:
    """Self-check of :func:`_tls_problems` (it guards the enrolment link)."""
    assert _tls_problems(b"\x16\x03\x01\x00\x02ab\x17\x03\x03\x00\x01c") == []
    assert _tls_problems(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")  # h2c preface
    assert _tls_problems(b"\x0a\x10" + bytes(16))  # a protobuf field
    assert _tls_problems(b"\x17\x03\x03\x00\x09abc")  # length past the end
    assert _tls_problems(b"\x17\x03\x03\x00\x01c\x00")  # trailing byte


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
    assert {"rest", "enroll", "agent0", "agent1", "agent0-peer", "agent1-peer"} <= names
    # The join really went through the enrolment link, and all of it is TLS.
    assert run.joined_node is not None
    assert fabric.enroll_link is not None
    enroll = fabric.enroll_link.recording()
    assert not enroll.truncated
    ups = [data for (_, way), data in enroll.streams.items() if way == "up"]
    downs = [data for (_, way), data in enroll.streams.items() if way == "down"]
    assert ups and downs and sum(map(len, ups)) > 1024, enroll.streams.keys()
    problems = {
        key: found
        for key, data in enroll.streams.items()
        if (found := _tls_problems(data))
    }
    assert problems == {}, problems


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
