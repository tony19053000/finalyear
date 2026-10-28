"""V0.17 G4: the installed Windows service joins a real fabric and runs a venv job.

Runs only in the ``package-windows`` CI job (``MESHFORGE_TEST_WINDOWS_SERVICE=1``,
an elevated prompt on ``windows-latest``), after the installer put
``MeshForgeAgent`` on the runner; the controller is the cluster harness
(``Fabric``) on the runner's own PostgreSQL (``MESHFORGE_TEST_PG=external``).

1. the service's ``config.toml`` names this controller and lets it run
   unsandboxed jobs with a CPU cap of 50 %; the service is restarted;
2. ``meshforge-agent join -`` typed by an administrator (Q32: elevated, acts on
   the service's state in place, the code on stdin and never echoed); the
   running service notices the enrolment and connects as that node;
3. the CPU cap is reported honestly (V0.7 G6 on Windows: job objects with a hard
   CPU-rate cap, so no ``max_cpu_percent`` in ``not_enforced``) - and the job
   below proves it from inside by its effect: one busy process per logical CPU
   gets about one core in total (the venv launcher's own nested job hides the
   agent's job from a direct query);
4. a requirements job pinned to the node succeeds through the **venv** runtime,
   the env built by the bundle's uv on the bundled interpreter, offline from a
   wheelhouse in the service's cache; the job runs as the service account (it
   cannot write the program folder or the config folder).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import Fabric
from mf_envs_support import make_wheel
from mf_split_cluster import TIMING, submit, upload_folder, wait_job

pytestmark = [
    pytest.mark.slow,
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        sys.platform != "win32"
        or os.environ.get("MESHFORGE_TEST_WINDOWS_SERVICE") != "1",
        reason="the installed Windows service (package-windows CI job only)",
    ),
]

SERVICE: Final = "MeshForgeAgent"
DATA: Final = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "MeshForge"
PROGRAM: Final = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "MeshForge"
SHIM: Final = PROGRAM / "bin" / "meshforge-agent.cmd"
WHEELS: Final = DATA / "cache" / "wheels"
MAIN: Final = r"""
import ctypes, os, subprocess, sys, time
from ctypes import wintypes
import mfwindemo

# A venv's python.exe is a launcher that runs the real interpreter inside a job
# object of its own, nested in the agent's: the immediate job (read below with a
# NULL handle) is the launcher's and has no CPU rate. That line is diagnostic
# only; the agent's hard cap (cpus = 1 here) binds from the parent job and is
# proven by its effect: one busy process per logical CPU for BURN_S seconds
# gets about one core in total, not one each.
class Rate(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

rate, size = Rate(), wintypes.DWORD()
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
ok = kernel32.QueryInformationJobObject(
    None, 15, ctypes.byref(rate), ctypes.sizeof(rate), ctypes.byref(size)
)
in_job = wintypes.BOOL()
kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job))
program = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "MeshForge")
config = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "MeshForge", "config")
denied = []
for folder in (program, config):
    try:
        with open(os.path.join(folder, "job-was-here"), "w") as handle:
            handle.write("x")
    except OSError:
        denied.append(folder)
BURN_S = 3.0
burn = (
    "import time\nend = time.monotonic() + %r\n"
    "while time.monotonic() < end: pass\nprint(time.process_time())" % BURN_S
)
threads = os.cpu_count() or 1
burners = [
    subprocess.Popen([sys.executable, "-c", burn], stdout=subprocess.PIPE, text=True)
    for _ in range(max(2, threads))
]
cpu_s = sum(float(p.communicate()[0].strip() or "nan") for p in burners)
cores = cpu_s / BURN_S
print("VERSION", mfwindemo.VERSION, "BASE", sys.base_prefix)
print("CPURATE", int(ok), rate.ControlFlags, rate.CpuRate, "IN_JOB", int(in_job.value))
print("CORES", round(cores, 2), "THREADS", threads, "BURNERS", len(burners))
print("DENIED", len(denied))
# The cap is 1 core of `threads`; uncapped the burners would use ~`threads`
# cores. A busy runner only lowers `cores`, so load can never fail this check.
hard_cap = bool(in_job.value) and threads >= 2 and cores <= 1.5
in_bundle = sys.base_prefix.lower().startswith(program.lower())
sys.exit(0 if hard_cap and in_bundle and len(denied) == 2 else 3)
"""
MANIFEST: Final = {
    "name": "windows-service-venv",
    "entrypoint": ["python", "main.py"],
    "env": {"requirements": "req.txt"},
    "resources": {"cpus": 1},
}


def _config(grpc_target: str) -> str:
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
grace_s = 1.0

[agent.policy]
accept_jobs = true
allow_unsandboxed = true
max_cpu_percent = 50

[agent.envs]
find_links = '{WHEELS}'
no_index = true
free_disk_floor_mb = 0
"""


def _ps(script: str) -> str:
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


def _agent_events(since: int) -> list[dict[str, Any]]:
    raw = (DATA / "logs" / "agent.log").read_bytes()[since:]
    events: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", "replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _log_tail() -> str:
    path = DATA / "logs" / "agent.log"
    return path.read_text("utf-8", "replace")[-6000:] if path.exists() else "(no log)"


def _connected(fabric: Fabric, node_id: str, within: float) -> dict[str, Any]:
    deadline = time.monotonic() + within
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        view = (fabric.nodes_by_id() or {}).get(node_id, {})
        if view.get("connected") and view.get("not_enforced") is not None:
            return view
        time.sleep(0.5)
    raise AssertionError(
        f"node {node_id} not connected in {within} s: {view}\n{_log_tail()}"
    )


@pytest.fixture
def service_config() -> Iterator[Path]:
    config = DATA / "config" / "config.toml"
    backup = config.read_bytes() if config.exists() else None
    yield config
    if backup is None:
        config.unlink(missing_ok=True)
    else:
        config.write_bytes(backup)


def test_installed_service_joins_and_runs_a_venv_job(
    make_fabric: Callable[..., Fabric],
    service_config: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert SHIM.is_file(), "run the installer first (package-windows CI job)"
    fabric = make_fabric(
        agents=0,
        fabric_ca=True,
        enroll_agents=False,
        start_agents=False,
        timing=TIMING,
        controller_jobs={"dispatch_interval_s": 0.1},
    )
    assert fabric.enroll_target, "no enrolment listener"
    monkeypatch.setenv("SSL_CERT_FILE", str(fabric.ca_dir / "ca.crt"))

    # 1. the service's configuration (written by an administrator), restart
    service_config.write_text(_config(fabric.grpc_target), encoding="utf-8")
    make_wheel(WHEELS, "mfwindemo", "1.0")
    offset = (DATA / "logs" / "agent.log").stat().st_size
    _ps(
        f"Restart-Service {SERVICE} -Force; "
        f"(Get-Service {SERVICE}).WaitForStatus('Running', '00:01:30')"
    )

    # 2. an administrator joins: in place, on the service's state (Q32)
    made = fabric.call("POST", "/v1/fabric/invites", json={"ttl_s": 300})
    assert made.status_code == 201, made.text
    code = str(made.json()["code"])
    joined = subprocess.run(
        [str(SHIM), "join", "-"],
        input=code + "\n",
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    out = joined.stdout + joined.stderr
    assert joined.returncode == 0, out
    assert "acting on the MeshForge service's state" in joined.stderr, out
    assert code not in out
    node = json.loads((DATA / "identity" / "node.json").read_text(encoding="utf-8"))
    node_id = str(node["node_id"])
    view = _connected(fabric, node_id, within=90)

    # 3. honest CPU cap on Windows: job objects enforce it
    not_enforced = view["not_enforced"]
    assert isinstance(not_enforced, list), view
    assert not [c for c in not_enforced if c.startswith("max_cpu_percent:")], view

    # 4. a requirements job through the venv runtime, run by the service
    project = tmp_path / "project"
    project.mkdir()
    (project / "main.py").write_text(MAIN, encoding="utf-8")
    (project / "req.txt").write_text("mfwindemo==1.0\n", encoding="utf-8")
    job_id = submit(fabric, MANIFEST, upload_folder(fabric, project), node_id=node_id)
    job = wait_job(fabric, job_id, within=300)
    if job["state"] != "succeeded":
        # The job's own lines (VERSION / CPURATE / CORES / DENIED) say which
        # check failed; the agent log tail last (the message is long). The exit
        # code is the attempt's (the job view has none of its own).
        output = fabric.call("GET", f"/v1/jobs/{job_id}/logs?stream=both").text
        attempts = job.get("attempts") or [{}]
        raise AssertionError(
            f"job {job['state']} ({job.get('failure_type')}, exit "
            f"{attempts[-1].get('exit_code')})\njob output:\n{output[-4000:]}\n"
            f"agent log:\n{_log_tail()}"
        )
    events = _agent_events(offset)
    ready = [e for e in events if e.get("event") == "agent.execution.env_ready"]
    assert ready and ready[0].get("env_cache") == "built", (ready, _log_tail())
    venvs = sorted((DATA / "envs").rglob("pyvenv.cfg"))
    assert venvs, "no venv under the service's state folder"
    cfg = venvs[0].read_text(encoding="utf-8").lower()
    assert str(PROGRAM).lower() in cfg, cfg  # built on the bundled interpreter
    print("job venv:", venvs[0], "node:", node_id, "not_enforced:", not_enforced)  # noqa: T201 - CI evidence
