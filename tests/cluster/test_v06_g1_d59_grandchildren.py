"""V0.6 G1 (D59): a real agent process is SIGKILLed while its job's workload
(subprocess runtime) has a forked sleeper - nothing the job started survives.

Before V0.6 the workload process died with the agent (``PR_SET_PDEATHSIG``) but
its children were orphaned; the chaos suite (D118) kills agents at random and
needs a clean slate after every kill. Linux only: Windows ends the tree with a
kill-on-close job object (not verifiable on this machine).
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

psutil = pytest.importorskip(
    "psutil", reason="the cluster harness needs the agent extra"
)
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing  # noqa: E402

from meshforge.common.bundle import pack_to_file  # noqa: E402

pytestmark = [
    pytest.mark.cluster,
    pytest.mark.timeout(240),
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="the group watchdog is Linux"
    ),
]

TIMING = Timing(
    name="d59",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.2,
    lease_multiplier=4,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=2.0,
)

WORKLOAD = """\
import os, subprocess, sys, time
g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
with open(sys.argv[1] + ".tmp", "w") as fh:
    fh.write(f"{os.getpid()} {g.pid}")
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(600)
"""


def _alive(pid: int) -> bool:
    try:
        return bool(psutil.Process(pid).status() != psutil.STATUS_ZOMBIE)
    except psutil.NoSuchProcess:
        return False


def test_sigkill_of_a_real_agent_leaves_no_workload_descendant(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution={"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0},
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    victim = fabric.nodes[0]
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WORKLOAD)
    bundle = tmp_path / "proj.bundle"
    packed = pack_to_file(project, bundle)
    put = fabric.call(
        "PUT", f"/v1/blobs/{packed.digest.hex}", content=bundle.read_bytes()
    )
    assert put.status_code in (200, 201), put.text
    pidfile = tmp_path / "pids.txt"
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": {
                "name": "d59",
                "entrypoint": ["python", "main.py", str(pidfile)],
                "limits": {"timeout": 600},
            },
            "bundle_digest": str(packed.digest),
            "placement": {"node_id": victim.node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    deadline = time.monotonic() + 60
    while not (pidfile.exists() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline, "the workload never started"
        time.sleep(0.05)
    workload, grandchild = map(int, pidfile.read_text().split())
    assert _alive(workload) and _alive(grandchild)
    victim.kill()
    deadline = time.monotonic() + 5.0
    while (_alive(workload) or _alive(grandchild)) and time.monotonic() < deadline:
        time.sleep(0.02)
    survivors = [p for p in (workload, grandchild) if _alive(p)]
    assert survivors == [], "a workload process outlived the SIGKILLed agent"
