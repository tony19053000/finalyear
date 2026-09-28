"""V0.4 G5 cluster scenarios: outputs, live logs and the CLI through real PROCESSES.

A controller and two agents run as separate processes (the agents behind the
fault proxy); ``compute`` runs as its own process too. Agents use the subprocess
runtime (pinned, as in G4's scenarios). Timings are **loopback** numbers - "not a
benchmark claim": they bound the software on one machine, never a network.

* criterion 1 - the ``cpu-deterministic`` example (and ``pytorch-train`` on the
  CPU, when torch is installed) run with ``compute run --node`` on one agent and
  ``compute run --local`` on the other: the two attempts' ``output_digest`` are
  identical, and ``compute get`` unpacks byte-identical files;
* criterion 2 - a workload prints a line with its wall-clock time every 100 ms;
  a WebSocket follower measures print -> receive over >= 50 lines: p50/p95/max
  reported, max <= 1 s asserted (same host clock on both ends);
* ``compute run -f`` exits with the job's outcome (0, the workload's code).
"""

from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(300)]

EXAMPLES = Path(__file__).resolve().parents[2] / "workloads" / "examples"
TIMING = Timing(
    name="outputs-logs",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.2,
    lease_multiplier=4,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=2.0,
)
EXEC = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}

TICKER = """\
import sys, time
time.sleep(2.0)  # the follower connects first: live delivery is what is measured
for i in range(int(sys.argv[1])):
    print(f"tick {i} {time.time():.6f}", flush=True)
    time.sleep(0.1)
"""


def _fabric(make_fabric: Callable[..., Fabric]) -> Fabric:
    return make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )


def _compute(
    fabric: Fabric, local_data: Path, *args: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__TOKEN": fabric.fabric_token(),
            "MESHFORGE_DATA_DIR": str(local_data),
            "XDG_CONFIG_HOME": str(cwd / "cli-config"),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "meshforge.cli", *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )


def _job(fabric: Fabric, job_id: str) -> dict[str, Any]:
    resp = fabric.call("GET", f"/v1/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _run_both(
    fabric: Fabric, project: Path, tmp_path: Path, label: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The same project with --node (agent 1) and --local (agent 0's laptop)."""
    local, remote = fabric.nodes[0], fabric.nodes[1]
    work = tmp_path / label
    work.mkdir()
    results = []
    for args in (("--node", remote.node_id), ("--local",)):
        done = _compute(
            fabric, local.data_dir, "run", str(project), *args, "-f", cwd=work
        )
        assert done.returncode == 0, (args, done.stdout[-2000:], done.stderr[-2000:])
        job_id = done.stdout.splitlines()[0].strip()
        results.append(_job(fabric, job_id))
    remote_job, local_job = results
    assert remote_job["placement"] == {"mode": "offload", "node_id": remote.node_id}
    assert local_job["placement"] == {"mode": "local", "node_id": local.node_id}
    return remote_job, local_job


def _outputs_equal(
    fabric: Fabric,
    tmp_path: Path,
    remote_job: dict[str, Any],
    local_job: dict[str, Any],
) -> str:
    (ra,) = remote_job["attempts"]
    (la,) = local_job["attempts"]
    assert ra["output_digest"] and ra["output_digest"] == la["output_digest"], (ra, la)
    got = []
    for job in (remote_job, local_job):
        dest = tmp_path / "get" / job["job_id"]
        done = _compute(
            fabric,
            fabric.nodes[0].data_dir,
            "get",
            job["job_id"],
            "--dest",
            str(dest),
            cwd=tmp_path,
        )
        assert done.returncode == 0, done.stderr
        got.append(
            {
                p.relative_to(dest).as_posix(): p.read_bytes()
                for p in sorted(dest.rglob("*"))
                if p.is_file()
            }
        )
    assert got[0] == got[1] and got[0]
    return str(ra["output_digest"])


def test_criterion_1_outputs_are_identical_remote_and_local(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    remote_job, local_job = _run_both(
        fabric, EXAMPLES / "cpu-deterministic", tmp_path, "cpu"
    )
    digest = _outputs_equal(fabric, tmp_path, remote_job, local_job)
    print(f"criterion 1 cpu-deterministic: both {digest}")  # noqa: T201


def test_criterion_1_pytorch_on_cpu(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    pytest.importorskip("torch", reason="the agents' interpreter has no torch")
    project = tmp_path / "pytorch-cpu"
    shutil.copytree(EXAMPLES / "pytorch-train", project)
    (project / "compute.yaml").write_text(
        "name: pytorch-train-cpu\n"
        "entrypoint: python main.py --steps 200 --device cpu\n"
        "resources: {gpu: none}\noutputs: [results/]\nlimits: {timeout: 10m}\n"
    )
    fabric = _fabric(make_fabric)
    remote_job, local_job = _run_both(fabric, project, tmp_path, "torch")
    digest = _outputs_equal(fabric, tmp_path, remote_job, local_job)
    print(f"criterion 1 pytorch-train (CPU): both {digest}")  # noqa: T201


def test_criterion_2_live_log_latency_under_one_second(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    from mf_cluster_ws import follow_lines

    fabric = _fabric(make_fabric)
    project = tmp_path / "ticker"
    project.mkdir()
    (project / "main.py").write_text(TICKER)
    lines = 60
    (project / "compute.yaml").write_text(
        f"name: ticker\nentrypoint: python main.py {lines}\n"
    )
    node = fabric.nodes[1]
    latencies: list[float] = []
    errors: list[BaseException] = []
    job_box: dict[str, str] = {}
    ready = threading.Event()

    def follow() -> None:
        try:
            ready.wait(60)
            for text, received in follow_lines(fabric, job_box["id"]):
                parts = text.split()
                if len(parts) == 3 and parts[0] == "tick":
                    latencies.append(received - float(parts[2]))
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    follower = threading.Thread(target=follow, daemon=True)
    follower.start()
    done = _compute(
        fabric,
        fabric.nodes[0].data_dir,
        "run",
        str(project),
        "--node",
        node.node_id,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    job_box["id"] = done.stdout.strip()
    ready.set()
    follower.join(120)
    assert not errors, errors
    assert len(latencies) >= 50, len(latencies)
    ordered = sorted(latencies)
    p50 = statistics.median(ordered)
    p95 = ordered[int(0.95 * (len(ordered) - 1))]
    worst = ordered[-1]
    print(  # noqa: T201 - measured, for the handoff
        f"Laptop 1 loopback (not a benchmark claim): log latency over {len(ordered)} "
        f"lines p50 {p50 * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms, max {worst * 1000:.0f} ms"
    )
    assert worst <= 1.0, ordered[-5:]


def test_cli_run_follow_exit_code_is_the_job_outcome(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    project = tmp_path / "bad"
    project.mkdir()
    (project / "main.py").write_text("import sys\nprint('bye')\nsys.exit(7)\n")
    done = _compute(
        fabric,
        fabric.nodes[0].data_dir,
        "run",
        str(project),
        "--node",
        fabric.nodes[1].node_id,
        "-f",
        cwd=tmp_path,
    )
    assert done.returncode == 7, (done.stdout, done.stderr)
    assert "bye" in done.stdout
    job = _job(fabric, done.stdout.splitlines()[0].strip())
    assert job["state"] == "failed" and job["failure_type"] == "user_error"
