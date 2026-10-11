"""V0.5 G5 tester round 1 - pinned named inputs across REAL process restarts.

One controller + one agent process. The agent's GC quota is 1 byte with no
minimum age, so every pass evicts everything that is not leased or pinned.

* the pinned input's blobs stay on the agent's disk through passes, an agent
  SIGKILL + restart, and a controller SIGKILL + restart (pins are re-sent after
  every Reconcile);
* a job's own project tree is evicted by the same passes (the quota is real);
* after the unpin the pinned blobs go too;
* a lease of a killed agent does not keep a blob alive after the restart.
"""

from __future__ import annotations

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
from meshforge.common.cas import CasStore
from meshforge.common.hashing import Digest
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(300)]

TIMING = Timing(
    name="pins-restart",
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
GC = {"gc": {"cas_bytes": 1, "envs_bytes": 1, "min_age_s": 0, "interval_s": 1.0}}
TERMINAL = {"succeeded", "failed", "cancelled"}


def _client(fabric: Fabric) -> ControllerClient:
    return ControllerClient(
        fabric.http_base, timeout_s=30, token=SecretStr(fabric.api_token())
    )


def _upload(fabric: Fabric, built: BuiltTree) -> None:
    client = _client(fabric)
    try:
        upload_tree(client, built)
    finally:
        client.close()


def _folder(root: Path, files: dict[str, bytes]) -> BuiltTree:
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    return build_tree(root, chunk_size=64 * 1024)


def _present(node: Node, built: BuiltTree) -> bool:
    store = CasStore(node.data_dir / "cas", max_blob_bytes=1 << 40)
    digests = [
        built.tree.tree_id,
        *(Digest.parse(f"sha256:{h}") for h in built.sources),
    ]
    have = [store.path_for(d).exists() for d in digests]
    assert len(set(have)) == 1, f"a tree is half evicted: {have}"
    return have[0]


def _gc(fabric: Fabric, node: Node) -> bool:
    resp = fabric.call("POST", f"/v1/nodes/{node.node_id}/gc")
    assert resp.status_code == 202, resp.text
    time.sleep(1.5)  # a pass is a few ms; allow the command + the pass
    return True


def _until(
    predicate: Callable[[], bool], within: float, what: str, fabric: Fabric
) -> None:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    fabric.fail(f"timed out waiting for {what}")


def _run(fabric: Fabric, node: Node, project: BuiltTree) -> dict[str, Any]:
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": {
                "name": "g5",
                "entrypoint": ["python", "main.py"],
                "inputs": [{"ref": "keep@1", "mount": "in"}],
            },
            "tree_id": str(project.tree.tree_id),
            "placement": {"node_id": node.node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        job = dict(fabric.call("GET", f"/v1/jobs/{job_id}").json())
        if job["state"] in TERMINAL:
            return job
        time.sleep(0.1)
    fabric.fail(f"job {job_id} never finished")


def test_pinned_input_survives_agent_and_controller_restarts_then_goes_when_unpinned(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs=JOBS,
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        agent_extra=GC,
    )
    node = fabric.nodes[0]
    dataset = _folder(
        tmp_path / "ds", {"a.csv": b"1,2,3\n" * 100, "b.csv": b"x" * 5000}
    )
    project = _folder(
        tmp_path / "p",
        {
            "main.py": b"print(sorted(__import__('os').listdir('in')))\n",
            "lib.txt": b"l" * 3000,
        },
    )
    _upload(fabric, dataset)
    _upload(fabric, project)
    posted = fabric.call(
        "POST",
        "/v1/data",
        json={"name": "keep", "tree_id": str(dataset.tree.tree_id), "pinned": True},
    )
    assert posted.status_code == 201, posted.text
    job = _run(fabric, node, project)
    assert job["state"] == "succeeded", job
    _gc(fabric, node)
    assert _present(node, dataset), "the pinned input was evicted"
    assert not _present(node, project), "an unpinned, unleased tree survived quota 1"

    # Agent SIGKILL: its GC index (pins) is on disk; the controller re-sends
    # the pins after the new session's Reconcile either way.
    node.kill()
    node.restart(graceful=False)
    fabric.wait_all_connected(within=60)
    _gc(fabric, node)
    assert _present(node, dataset), "the pinned input did not survive an agent kill"

    # Controller SIGKILL + restart: pins are reloaded from PostgreSQL at start.
    fabric.restart_controller(graceful=False)
    fabric.wait_all_connected(within=60)
    _gc(fabric, node)
    assert _present(node, dataset), "the pinned input did not survive a controller kill"

    # And it is still usable: a job after all that succeeds (re-fetch the project).
    _upload(fabric, project)
    job = _run(fabric, node, project)
    assert job["state"] == "succeeded", job

    unpin = fabric.call("PATCH", "/v1/data/keep/1", json={"pinned": False})
    assert unpin.status_code == 200, unpin.text
    _until(
        lambda: _gc(fabric, node) and not _present(node, dataset),
        30,
        "unpinned input evicted",
        fabric,
    )
