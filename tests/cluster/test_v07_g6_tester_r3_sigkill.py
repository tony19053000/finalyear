# ruff: noqa: E501, S311, T201
"""V0.7 G6 tester r3 (T-14): a real agent process SIGKILLed at random moments
right after a secret-carrying job was submitted (including during the secret
write); nothing is left after the restart's sweep, and nothing under the base
is listed that the restart did not remove.
"""

from __future__ import annotations

import os
import random
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

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(900)]

TIMING = Timing(
    name="g6r3kill",
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
SECRET = b"sigkill-r3-SECRET-0123456789abc"
MAIN = "import os, time\nprint(open(os.path.join(os.environ['MESHFORGE_SECRETS_DIR'], 'KILL_KEY')).read(), flush=True)\ntime.sleep(120)\n"


def _base(node: Node) -> Path:
    return (
        Path("/dev/shm") / f"meshforge-secrets-{os.getuid()}-{agent_tag(node.data_dir)}"
    )


def _listing(node: Node) -> list[str]:
    base = _base(node)
    return (
        sorted(str(p.relative_to(base)) for p in base.rglob("*"))
        if base.exists()
        else []
    )


def test_sigkill_at_random_moments_then_restart_sweeps(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.05},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    (node,) = fabric.nodes
    assert fabric.call(
        "PUT", "/v1/secrets/KILL_KEY", as_user=NORMAL_USER, content=SECRET
    ).is_success
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(MAIN)
    built = build_tree(root, chunk_size=256 * 1024)
    client = ControllerClient(
        fabric.http_base, timeout_s=30, token=SecretStr(fabric.api_token(NORMAL_USER))
    )
    try:
        upload_tree(client, built)
    finally:
        client.close()
    tree = str(built.tree.tree_id)
    rnd = random.Random(14102026)
    left_after_kill = 0
    for rnd_i in range(8):
        resp = None
        for _ in range(300):
            resp = fabric.call(
                "POST",
                "/v1/jobs",
                as_user=NORMAL_USER,
                json={
                    "manifest": {
                        "name": f"kill{rnd_i}",
                        "entrypoint": ["python", "main.py"],
                        "secrets": ["KILL_KEY"],
                        "retry": {"max_attempts": 1},
                    },
                    "tree_id": tree,
                    "placement": {"node_id": node.node_id},
                },
            )
            if resp.status_code != 409:
                break
            time.sleep(0.2)  # the restarted node is "suspect" until its session is back
        assert resp is not None and resp.status_code == 201, resp.text
        job_id = str(resp.json()["job_id"])
        time.sleep(rnd.choice([0.0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.6]))
        node.kill()
        if _listing(node):
            left_after_kill += 1
        node.restart(graceful=False)
        deadline = time.monotonic() + 60
        while _listing(node) and time.monotonic() < deadline:
            time.sleep(0.05)
        # the restarted agent may legitimately be re-running the job (retry /
        # re-dispatch): cancel first, then require a clean base
        fabric.call("POST", f"/v1/jobs/{job_id}/cancel", as_user=NORMAL_USER)
        deadline = time.monotonic() + 90
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = dict(
                fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json()
            )
            if state["state"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(0.2)
        deadline = time.monotonic() + 30
        while _listing(node) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _listing(node) == [], (rnd_i, state.get("state"), _listing(node))
    print(f"killed 8 times; folders existed right after the kill in {left_after_kill}")
