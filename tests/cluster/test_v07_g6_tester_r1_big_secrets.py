"""V0.7 G6 tester r1: a user who stores big secrets must not stall the fabric.

Any authenticated user may store 256 secrets of up to 64 KiB and name 32 of
them in one job. The redactor compiles one regex per stream out of every form
of every value; that costs ~0.8 s CPU and ~140 MB RAM per 64 KiB secret per
redactor, on the controller's event loop (`LogHub.open`) and on the agent's
(`Executor._execute`). Six 64 KiB secrets must not freeze the controller's REST
loop for seconds (heartbeats, every other user's requests).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import ADMIN_USER, NORMAL_USER, Fabric, Timing
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import build_tree

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(400)]

TIMING = Timing(
    name="g6big",
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
SECRETS = 6
MAX_ALLOWED_STALL_S = 2.0


@pytest.mark.security
def test_big_secrets_do_not_freeze_the_controller_loop(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        # Fix round 1 (D162): 6 x 64 KiB is above the new default per-job cap
        # (256 KiB); the cap is raised here so this test still drives MORE
        # than a default fabric accepts through the redactor. The default cap
        # has its own test (test_v07_g6_fix1_secret_cap.py).
        controller_jobs={
            "dispatch_interval_s": 0.1,
            "max_job_secret_bytes": SECRETS * 64 * 1024,
        },
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    (node,) = fabric.nodes
    names = [f"BIG{i}" for i in range(SECRETS)]
    for name in names:
        r = fabric.call(
            "PUT",
            f"/v1/secrets/{name}",
            as_user=NORMAL_USER,
            content=os.urandom(64 * 1024),
        )
        assert r.is_success, r.text
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text("print('hi')\n")
    built = build_tree(root, chunk_size=256 * 1024)
    client = ControllerClient(
        fabric.http_base, timeout_s=30, token=SecretStr(fabric.api_token(NORMAL_USER))
    )
    try:
        upload_tree(client, built)
    finally:
        client.close()

    stalls: list[float] = []
    stop = threading.Event()
    admin = fabric.api_token(ADMIN_USER)

    def probe() -> None:
        with httpx.Client(
            base_url=fabric.http_base,
            headers={"authorization": f"Bearer {admin}"},
            timeout=60,
            verify=fabric.ssl_context if fabric.ssl_context is not None else True,
        ) as http:
            while not stop.is_set():
                t0 = time.monotonic()
                with contextlib.suppress(httpx.HTTPError):
                    http.get("/healthz")
                stalls.append(time.monotonic() - t0)
                time.sleep(0.05)

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    try:
        resp = fabric.call(
            "POST",
            "/v1/jobs",
            as_user=NORMAL_USER,
            json={
                "manifest": {
                    "name": "big-secrets",
                    "entrypoint": ["python", "main.py"],
                    "secrets": names,
                    "retry": {"max_attempts": 1},
                },
                "tree_id": str(built.tree.tree_id),
                "placement": {"node_id": node.node_id},
            },
        )
        assert resp.status_code == 201, resp.text
        job_id = str(resp.json()["job_id"])
        deadline = time.monotonic() + 240
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = dict(
                fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json()
            )
            if state["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.2)
    finally:
        stop.set()
        thread.join(10)
    assert state.get("state") == "succeeded", state
    assert max(stalls) < MAX_ALLOWED_STALL_S, (
        f"the controller answered /healthz after {max(stalls):.1f} s while a job "
        f"with {SECRETS} x 64 KiB secrets was dispatched"
    )
