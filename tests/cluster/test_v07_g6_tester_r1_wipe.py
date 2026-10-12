"""V0.7 G6 tester r1, real processes: secret files are wiped on EVERY exit path.

A real controller and real agent processes (``mf_cluster``). The job's secret
lives in ``/dev/shm/meshforge-secrets-<uid>-<tag>/<attempt>`` while the attempt
runs and must be gone afterwards:

* normal end (success), failure, timeout, cancel;
* the agent SIGKILLed mid-run (the files cannot be wiped by a dead process) ->
  the restarted agent's start-up sweep removes them;
* the controller SIGKILLed mid-run (controller-lost): the agent's attempt ends
  by itself and wipes without the controller;
and the value is in no log file of any process (controller, agents) nor under
any data dir, in any common encoding.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import NORMAL_USER, Fabric, Node, Timing
from pydantic import SecretStr

from meshforge.agent.execution.secret_files import agent_tag
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import build_tree

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(420)]

TIMING = Timing(
    name="g6wipe",
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
SECRET = b"cluster-r1-SECRET-7c1e0b5a3d9f"
TERMINAL = {"succeeded", "failed", "cancelled"}

MAIN = """\
import os, sys, time
d = os.environ["MESHFORGE_SECRETS_DIR"]
v = open(os.path.join(d, "WIPE_KEY"), "rb").read()
print("got", len(v), flush=True)
print("leak:" + v.decode(), flush=True)
print("leak:" + v.decode(), file=sys.stderr, flush=True)
open(sys.argv[1], "w").write("started")
mode = sys.argv[2]
if mode == "ok":
    pass
elif mode == "fail":
    sys.exit(7)
else:
    time.sleep(float(sys.argv[3]))
"""


def _base(node: Node) -> Path:
    return (
        Path("/dev/shm") / f"meshforge-secrets-{os.getuid()}-{agent_tag(node.data_dir)}"
    )


def _entries(node: Node) -> list[str]:
    base = _base(node)
    return sorted(p.name for p in base.iterdir()) if base.exists() else []


def _wait(
    cond: Callable[[], bool], what: str, fabric: Fabric, timeout: float = 60
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        time.sleep(0.05)
    fabric.fail(f"timed out waiting for {what}")


def _job(
    fabric: Fabric,
    tree_id: str,
    node: Node,
    marker: Path,
    mode: str,
    sleep_s: float = 0,
    timeout_s: int | None = None,
) -> str:
    manifest: dict[str, Any] = {
        "name": f"wipe-{mode}",
        "entrypoint": ["python", "main.py", str(marker), mode, str(sleep_s)],
        "secrets": ["WIPE_KEY"],
        "retry": {"max_attempts": 1},
    }
    if timeout_s is not None:
        manifest["limits"] = {"timeout": timeout_s}
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        as_user=NORMAL_USER,
        json={
            "manifest": manifest,
            "tree_id": tree_id,
            "placement": {"node_id": node.node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"])


def _state(fabric: Fabric, job_id: str) -> dict[str, Any]:
    return dict(fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json())


def _final(fabric: Fabric, job_id: str, timeout: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = _state(fabric, job_id)
        if job["state"] in TERMINAL:
            return job
        time.sleep(0.1)
    fabric.fail(f"job {job_id} did not finish")
    raise AssertionError


def _forms(value: bytes) -> list[bytes]:
    return [
        value,
        base64.b64encode(value),
        base64.b64encode(value).rstrip(b"="),
        value.hex().encode(),
        value.hex().upper().encode(),
    ]


def test_secret_files_are_wiped_on_every_exit_path_and_values_are_in_no_log(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        log_level="DEBUG",
    )
    (node,) = fabric.nodes
    token = fabric.api_token(NORMAL_USER)
    assert fabric.call(
        "PUT", "/v1/secrets/WIPE_KEY", as_user=NORMAL_USER, content=SECRET
    ).is_success

    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(MAIN)
    built = build_tree(root, chunk_size=256 * 1024)
    client = ControllerClient(fabric.http_base, timeout_s=30, token=SecretStr(token))
    try:
        upload_tree(client, built)
    finally:
        client.close()
    tree = str(built.tree.tree_id)

    # 1-2. success and failure
    for mode, state in (("ok", "succeeded"), ("fail", "failed")):
        job = _final(fabric, _job(fabric, tree, node, tmp_path / f"{mode}.m", mode))
        assert job["state"] == state, job
        _wait(lambda: _entries(node) == [], f"wipe after {mode}", fabric, 10)

    # 3. timeout
    marker = tmp_path / "to.m"
    job_id = _job(fabric, tree, node, marker, "sleep", 60, timeout_s=3)
    job = _final(fabric, job_id)
    assert job["state"] == "failed" and job["failure_type"] == "deadline_exceeded", job
    _wait(lambda: _entries(node) == [], "wipe after timeout", fabric, 10)

    # 4. cancel while running
    marker = tmp_path / "cancel.m"
    job_id = _job(fabric, tree, node, marker, "sleep", 120)
    _wait(marker.exists, "workload start", fabric)
    assert len(_entries(node)) == 1
    assert fabric.call(
        "POST", f"/v1/jobs/{job_id}/cancel", as_user=NORMAL_USER
    ).is_success
    assert _final(fabric, job_id)["state"] == "cancelled"
    _wait(lambda: _entries(node) == [], "wipe after cancel", fabric, 10)

    # 5. controller SIGKILLed mid-run: the agent finishes alone and wipes alone
    marker = tmp_path / "ctl.m"
    job_id = _job(fabric, tree, node, marker, "sleep", 6)
    _wait(marker.exists, "workload start", fabric)
    assert len(_entries(node)) == 1
    fabric.kill_controller()
    _wait(lambda: _entries(node) == [], "wipe with the controller dead", fabric, 40)
    fabric.restart_controller()
    final = _final(fabric, job_id, 120)
    assert final["state"] in TERMINAL, final

    # 6. agent SIGKILLed mid-run: files left behind, swept by the restart
    marker = tmp_path / "kill.m"
    job_id = _job(fabric, tree, node, marker, "sleep", 120)
    _wait(marker.exists, "workload start", fabric)
    folders = _entries(node)
    assert len(folders) == 1
    node.kill()
    leftover = _base(node) / folders[0]
    assert leftover.exists(), "SIGKILL cannot wipe: the files stay until the restart"
    node.restart(graceful=False)
    _wait(lambda: _entries(node) == [], "start-up sweep after SIGKILL", fabric, 60)
    # the (now orphaned) workload of the killed agent is not our concern here
    fabric.call("POST", f"/v1/jobs/{job_id}/cancel", as_user=NORMAL_USER)

    # the value is in no log or data file of any process, in any common encoding
    leaks: list[str] = []
    redacted_files = 0
    for path in fabric.workdir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.endswith((".key", ".sock", ".lock")):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        redacted_files += b"leak:[secret:WIPE_KEY]" in data
        leaks.extend(
            f"{path.relative_to(fabric.workdir)}: {form[:12]!r}"
            for form in _forms(SECRET)
            if form in data
        )
    assert leaks == [], leaks
    # positive control: the scan really looked at the files the leak went to
    assert redacted_files >= 1, "no agent log file holds the redacted marker"
