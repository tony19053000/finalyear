"""V0.17 G1: the packaged agent joins a real fabric and runs a job as a hardened service.

A real controller (the cluster harness: PostgreSQL, fabric CA from `fabric create`,
enrolment listener) on this machine, and a clean Ubuntu 24.04 systemd container
sharing the host network (so ``127.0.0.1`` is the controller) with the
meshforge-agent ``.deb`` installed - no Python, no uv, never joined.

1. ``apt install``: "not joined yet"; since V0.17 G3 the service already runs,
   waiting for the join with only its local console up; ``config.toml`` is
   written afterwards (the documented order);
2. ``meshforge-agent join -`` typed by root -> re-executed as ``meshforge``
   (Q15), the code on stdin; the running service notices the enrolment, re-reads
   the configuration written in 1 (G3-D2) and starts its fabric part by itself;
   the node is CONNECTED with the certificate bound to the service's key;
3. the node's live owner policy has no ``max_cpu_percent:unsandboxed_linux`` in
   ``not_enforced``: the service's delegated cgroup enforces CPU shares (V0.7 G6;
   systemd 255);
4. a requirements job pinned to the node succeeds through the **venv** runtime:
   the env is built by the bundle's uv on the bundled interpreter (no uv / Python
   on PATH), offline from a wheelhouse in the service's state dir; the job itself
   checks the hardening (it cannot write ``/opt``, ``/etc`` or ``/home``) and
   prints where its interpreter lives;
5. ``kill -9`` of the agent: systemd restarts it, it reconnects with the same
   ``node_id``.

Slow (needs the .deb; see tests/_support/mf_deb.py) and Linux only.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import Fabric
from mf_deb import UNIT, Debs, SystemdBox, built_debs, systemd_image
from mf_envs_support import make_wheel
from mf_split_cluster import TIMING, submit, upload_folder, wait_job

pytestmark = [
    pytest.mark.docker,
    pytest.mark.slow,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(sys.platform != "linux", reason="Linux package"),
]

WHEELS: Final = "/var/lib/meshforge/wheels"
MAIN: Final = """\
import os, sys
import mfdebdemo

denied = []
for path in ("/opt/meshforge/current/x", "/etc/meshforge/x", "/home/x"):
    try:
        with open(path, "w") as handle:
            handle.write("x")
    except OSError:
        denied.append(path)
print("VERSION", mfdebdemo.VERSION, "PYTHON", sys.executable, "BASE", sys.base_prefix)
print("DENIED", ",".join(denied))
sys.exit(0 if len(denied) == 3 and sys.base_prefix.startswith("/opt/meshforge/") else 3)
"""
MANIFEST: Final = {
    "name": "deb-service-venv",
    "entrypoint": ["python", "main.py"],
    "env": {"requirements": "req.txt"},
}


def _agent_config(grpc_target: str) -> str:
    return f"""\
[log]
format = "json"

[agent]
controller_address = "{grpc_target}"

[agent.discovery]
enabled = false

[agent.scores]
enabled = false

[agent.execution]
enabled = true
grace_s = 1.0

[agent.policy]
allow_unsandboxed = true

[agent.envs]
find_links = "{WHEELS}"
no_index = true
free_disk_floor_mb = 0
"""


@pytest.fixture(scope="module")
def debs(tmp_path_factory: pytest.TempPathFactory) -> Debs:
    return built_debs(tmp_path_factory.mktemp("debs"))


@pytest.fixture
def box() -> Iterator[SystemdBox]:
    made = SystemdBox.start(
        systemd_image("24.04"), host_network=True, name_hint="deb-fabric"
    )
    try:
        yield made
    finally:
        made.remove()


def _connected(fabric: Fabric, node_id: str, within: float) -> dict[str, object]:
    deadline = time.monotonic() + within
    view: dict[str, object] = {}
    while time.monotonic() < deadline:
        nodes = fabric.nodes_by_id() or {}
        view = nodes.get(node_id, {})
        if view.get("connected") and view.get("not_enforced") is not None:
            return view
        time.sleep(0.5)
    raise AssertionError(f"node {node_id} not connected within {within} s: {view}")


def test_packaged_agent_joins_and_runs_a_venv_job(
    make_fabric: Callable[..., Fabric],
    debs: Debs,
    box: SystemdBox,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fabric = make_fabric(
        agents=0,
        fabric_ca=True,
        enroll_agents=False,
        start_agents=False,
        timing=TIMING,
        controller_jobs={"dispatch_interval_s": 0.1},
    )
    assert fabric.enroll_target, "no enrolment listener"
    # `fabric create` made this controller's own CA: the upload client trusts it
    monkeypatch.setenv("SSL_CERT_FILE", str(fabric.ca_dir / "ca.crt"))

    # 1. install, never joined
    done = box.install(debs.first)
    assert done.returncode == 0, done.stdout[-4000:]
    assert "has not joined a fabric yet" in done.stdout, done.stdout[-3000:]
    box.ok(
        "cat > /etc/meshforge/config.toml && chown root:meshforge "
        "/etc/meshforge/config.toml && chmod 0640 /etc/meshforge/config.toml",
        stdin=_agent_config(fabric.grpc_target),
    )
    wheels = tmp_path / "wheels"
    make_wheel(wheels, "mfdebdemo", "1.0")
    box.copy_in(wheels, WHEELS)
    box.ok(f"chown -R meshforge:meshforge {WHEELS}")

    # 2. join as root -> as meshforge; the waiting service starts its fabric part
    #    with the config written above (V0.17 G3: no path unit any more)
    made = fabric.call("POST", "/v1/fabric/invites", json={"ttl_s": 300})
    assert made.status_code == 201, made.text
    code = str(made.json()["code"])
    joined = box.run("meshforge-agent join -", stdin=code + "\n", timeout=120)
    assert joined.returncode == 0, joined.stdout + joined.stderr
    assert "via sudo" in joined.stderr
    assert code not in joined.stdout + joined.stderr
    facts = box.wait_unit(UNIT, ["active"], within=60)
    identity = box.identity()
    assert identity["enrolled"] is True
    node_id = str(identity["node_id"])
    view = _connected(fabric, node_id, within=60)
    cert = view["certificate"]
    assert isinstance(cert, dict)
    assert cert["public_key_fingerprint_sha256"] == identity["key_fingerprint_sha256"]

    # 3. CPU shares enforced by the service's delegated cgroup
    not_enforced = view.get("not_enforced")
    assert isinstance(not_enforced, list), view
    assert "max_cpu_percent:unsandboxed_linux" not in not_enforced, view

    # 4. a requirements job through the venv runtime, from the service
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text(MAIN)
    (project / "req.txt").write_text("mfdebdemo==1.0\n")
    job_id = submit(fabric, MANIFEST, upload_folder(fabric, project), node_id=node_id)
    job = wait_job(fabric, job_id, within=240)
    assert job["state"] == "succeeded", (job, box.journal())
    events = box.journal_events()
    ready = [e for e in events if e.get("event") == "agent.execution.env_ready"]
    assert ready and ready[0]["env_cache"] == "built", ready
    venvs = box.ok("ls /var/lib/meshforge/envs/venv").split()
    assert venvs, "no venv under the service's state dir"
    cfg = box.ok(f"cat /var/lib/meshforge/envs/venv/{venvs[0]}/pyvenv.cfg")
    assert "/opt/meshforge/current/python" in cfg, cfg  # SP3 F1/F2

    # 5. kill -9 -> restarted by systemd, reconnects as the same node
    pid = int(facts["MainPID"])
    box.ok(f"kill -9 {pid}")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        now = box.show(UNIT, "MainPID", "NRestarts", "ActiveState")
        if now["ActiveState"] == "active" and int(now["MainPID"]) not in (0, pid):
            break
        time.sleep(0.5)
    assert int(now["NRestarts"]) >= 1 and int(now["MainPID"]) != pid, now
    assert box.identity()["node_id"] == node_id
    _connected(fabric, node_id, within=90)
