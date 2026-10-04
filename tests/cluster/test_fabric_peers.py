"""V0.5 G4 cluster scenarios: peer blob fetching between agent PROCESSES.

A controller and three agents (A, B, C) as real processes, each agent with its
peer blob server on loopback (``[agent.peer] bind = 127.0.0.1``, any port). A and
B run a tree job first, so they hold the tree and report it; then C runs it with
the controller, A and B as sources. Observed through REST and the processes'
JSON logs only.

* A **blackholed** (process suspended: its socket accepts, nothing answers):
  C's requests to A time out, A is dropped, B and the controller finish - the
  job succeeds with the right data.
* A **killed** while C's requests to it are in flight: they fail at once, A is
  dropped, the job succeeds.

The lease is long on purpose (OFFLINE after 30 s) so A is still listed as an
ONLINE holder when C's job is dispatched: the scenario is the transfer, not
liveness.
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Node, Timing
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(300)]

PEER_TIMING = Timing(
    name="peers",
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
JOBS = {"dispatch_interval_s": 0.1}
AGENT_EXTRA = {
    "peer": {"bind": "127.0.0.1", "port": 0},
    "cache": {"flush_s": 0.2},
    "fetch": {"per_source": 2, "request_timeout_s": 3.0, "endgame_after_s": 600.0},
}
CHUNK = 256 * 1024
TERMINAL = {"succeeded", "failed", "cancelled"}

MAIN = """\
import hashlib, sys
data = open("data.bin", "rb").read()
open(sys.argv[1], "w").write(hashlib.sha256(data).hexdigest())
"""


def _fabric(make_fabric: Callable[..., Fabric]) -> Fabric:
    return make_fabric(
        agents=3,
        timing=PEER_TIMING,
        agent_execution=EXEC,
        controller_jobs=JOBS,
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        agent_extra=AGENT_EXTRA,
    )


def _tree(tmp_path: Path, name: str) -> tuple[BuiltTree, bytes]:
    root = tmp_path / name
    root.mkdir()
    (root / "main.py").write_text(MAIN)
    data = os.urandom(32 * CHUNK)
    (root / "data.bin").write_bytes(data)
    return build_tree(root, chunk_size=CHUNK), data


def _upload(fabric: Fabric, built: BuiltTree) -> None:
    client = ControllerClient(
        fabric.http_base, timeout_s=30, token=SecretStr(fabric.fabric_token())
    )
    try:
        upload_tree(client, built)
    finally:
        client.close()


def _run(fabric: Fabric, built: BuiltTree, node: Node, out: Path) -> str:
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": {
                "name": "peers",
                "entrypoint": ["python", "main.py", str(out)],
            },
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": node.node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"])


def _wait(
    fabric: Fabric, job_id: str, states: set[str], within: float
) -> dict[str, Any]:
    deadline = time.monotonic() + within
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = fabric.call("GET", f"/v1/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        job = dict(resp.json())
        if job["state"] in states:
            return job
        time.sleep(0.05)
    fabric.fail(f"job {job_id} not in {sorted(states)} within {within} s: {job}")


def _holds(fabric: Fabric, node: Node, trees: int) -> None:
    def ready() -> bool:
        detail = fabric.call("GET", f"/v1/nodes/{node.node_id}").json()
        tree = detail.get("cache", {}).get("tree", {"count": 0})
        return bool(detail.get("peer_endpoint")) and tree["count"] >= trees

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.1)
    fabric.fail(f"{node.name} never reported holding {trees} tree(s)")


def _seed(
    fabric: Fabric, tmp_path: Path, name: str, holders: list[Node]
) -> tuple[BuiltTree, bytes]:
    built, data = _tree(tmp_path, name)
    _upload(fabric, built)
    for node in holders:
        job = _wait(
            fabric,
            _run(fabric, built, node, tmp_path / f"{name}-{node.name}.out"),
            TERMINAL,
            60,
        )
        assert job["state"] == "succeeded", job
    return built, data


def _staged(node: Node) -> dict[str, Any]:
    records = node.process.log.events("agent.cas.staged")
    assert records, f"{node.name} logged no agent.cas.staged"
    return dict(records[-1]["sources"])


def test_blackholed_peer_is_dropped_and_the_others_finish(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    a, b, c = fabric.nodes
    built, data = _seed(fabric, tmp_path, "one", [a, b])
    _holds(fabric, a, 1)
    _holds(fabric, b, 1)
    a.pause()  # blackhole: A's socket accepts, nothing answers
    try:
        out = tmp_path / "c.out"
        job = _wait(fabric, _run(fabric, built, c, out), TERMINAL, 90)
    finally:
        a.resume()
    assert job["state"] == "succeeded", job
    assert out.read_text() == hashlib.sha256(data).hexdigest()
    sources = _staged(c)
    assert sources[a.node_id]["dropped"] in {"timeout", "transfer_error"}, sources
    assert sources[a.node_id]["units"] == 0
    assert sources[b.node_id]["units"] >= 1 and sources[b.node_id]["dropped"] is None


def test_peer_killed_mid_transfer_is_dropped_and_the_others_finish(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    a, b, c = fabric.nodes
    built, data = _seed(fabric, tmp_path, "two", [a, b])
    _holds(fabric, a, 1)
    _holds(fabric, b, 1)
    a.pause()  # C's requests to A hang in flight ...
    out = tmp_path / "c.out"
    job_id = _run(fabric, built, c, out)
    _wait(fabric, job_id, {"preparing", "running", *TERMINAL}, 30)
    time.sleep(1.0)  # ... until A dies under them (before C's 3 s timeout)
    a.kill()
    job = _wait(fabric, job_id, TERMINAL, 90)
    assert job["state"] == "succeeded", job
    assert out.read_text() == hashlib.sha256(data).hexdigest()
    sources = _staged(c)
    assert sources[a.node_id]["dropped"] in {"transfer_error", "timeout"}, sources
    assert sources[b.node_id]["units"] >= 1
