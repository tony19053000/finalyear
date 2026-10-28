"""V1.0 G5 (roadmap 1.0.7, D420): every demo scenario runs in its dry run
against a real fabric (controller + 3 agents as processes) and ends PASS, and
its reset leaves nothing behind.

The demos (``scripts/demo/0N-*.sh`` -> ``demo.py``) drive the real ``compute``
CLI, logged in as the harness admin, from the laptop of agent 0 (its node is
the Local candidate). Nothing is faked: the jobs run, the planner decides, the
outputs are fetched and checked by the demo itself against values it computes.

* ``01-local`` / ``02-mode1-offload``: through the ``.sh`` wrapper (bash);
* ``03-mode3-split``: ``--kill-cmd`` / ``--revive-cmd`` go through
  :mod:`mf_demo_ctl`, so the harness kills (SIGKILL) and restarts the lane's
  agent - the job must still succeed with the verified output;
* ``04-planner-explains``: Local (min_network), Mode 1 (Local's one CPU slot
  busy: ``slots_full``), Mode 3 - the planner's own choices, asserted;
* ``05-install-join``: a fabric whose agents are not enrolled; the demo's
  ``--join-cmd`` runs the real ``meshforge-agent join -`` with the code it
  made, then the agent starts and connects with its new certificate;
* ``--reset`` cancels a leftover demo job and revokes an unused demo invite.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing
from mf_demo_ctl import DemoControl, command

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(900)]

DEMO_DIR: Final = Path(__file__).resolve().parents[2] / "scripts" / "demo"
SCENARIOS: Final = (
    "01-local",
    "02-mode1-offload",
    "03-mode3-split",
    "04-planner-explains",
    "05-install-join",
)
TIMING: Final = Timing(
    name="demos",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.2,
    lease_multiplier=4,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=2.0,
)
EXEC: Final = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}
AGENT_ENV: Final = {"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"}
SPLIT: Final = {"split": {"visibility_timeout_s": 10.0, "wait_retry_s": 0.3}}


def _env(fabric: Fabric, home: Path, local_data: Path | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__CA_FILE": str(fabric.ca_dir / "ca.crt"),
            "MESHFORGE_CLI__TOKEN": fabric.api_token(),
            "XDG_CONFIG_HOME": str(home / "cli-config"),
            "MESHFORGE_DEMO_PYTHON": sys.executable,
            "PYTHONUTF8": "1",
            "NO_COLOR": "1",
        }
    )
    if local_data is not None:
        env["MESHFORGE_DATA_DIR"] = str(local_data)
    return env


def demo_argv(
    scenario: str, work: Path, *extra: str, wrapper: bool = False
) -> list[str]:
    if wrapper:
        argv = ["bash", str(DEMO_DIR / f"{scenario}.sh")]
    else:
        argv = [sys.executable, str(DEMO_DIR / "demo.py"), scenario]
    return [*argv, "--dry-run", "--work", str(work), *extra]


def run_demo(
    scenario: str,
    env: dict[str, str],
    work: Path,
    *extra: str,
    wrapper: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        demo_argv(scenario, work, *extra, wrapper=wrapper),
        env=env, capture_output=True, text=True, timeout=600, check=False,
    )  # fmt: skip


def assert_pass(done: subprocess.CompletedProcess[str], scenario: str) -> None:
    assert done.returncode == 0, (done.stdout[-6000:], done.stderr[-3000:])
    assert f"DEMO {scenario}: PASS" in done.stdout, done.stdout[-4000:]
    assert "UNEXPECTED" not in done.stdout, done.stdout[-4000:]


@pytest.fixture
def fabric(make_fabric: Callable[..., Fabric]) -> Fabric:
    return make_fabric(
        agents=3,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        controller_extra=SPLIT,
        agent_env=AGENT_ENV,
    )


def test_every_scenario_has_an_executable_wrapper() -> None:
    for scenario in SCENARIOS:
        script = DEMO_DIR / f"{scenario}.sh"
        text = script.read_text(encoding="utf-8")
        assert os.access(script, os.X_OK), script
        assert text.startswith("#!/usr/bin/env bash\n")
        assert f'"$here/demo.py" {scenario} "$@"' in text
    assert sorted(p.stem for p in DEMO_DIR.glob("0*.sh")) == list(SCENARIOS)


def test_01_and_02_local_then_offload_byte_identical(
    fabric: Fabric, tmp_path: Path
) -> None:
    env = _env(fabric, tmp_path, fabric.nodes[0].data_dir)
    done = run_demo("01-local", env, tmp_path / "w01", wrapper=True)
    assert_pass(done, "01-local")
    assert "EXPECTED   it ran on this laptop" in done.stdout
    done = run_demo("02-mode1-offload", env, tmp_path / "w02", wrapper=True)
    assert_pass(done, "02-mode1-offload")
    assert "byte-identical to Local" in done.stdout
    state = json.loads((tmp_path / "w02" / "state.json").read_text(encoding="utf-8"))
    assert len(state["jobs"]) == 2  # the local baseline + the offloaded job


def test_03_split_survives_a_killed_laptop(fabric: Fabric, tmp_path: Path) -> None:
    env = _env(fabric, tmp_path, fabric.nodes[0].data_dir)
    usage = run_demo("03-mode3-split", env, tmp_path / "w03u")
    assert usage.returncode == 2 and "needs --kill-cmd" in usage.stdout, usage.stdout
    ctl = DemoControl(fabric, tmp_path / "ctl")
    argv = demo_argv(
        "03-mode3-split", tmp_path / "w03",
        "--kill-cmd", command(tmp_path / "ctl", "kill"),
        "--revive-cmd", command(tmp_path / "ctl", "revive"),
    )  # fmt: skip
    done = ctl.run(argv, env, timeout_s=600)
    assert_pass(done, "03-mode3-split")
    (kill, victim), (revive, again) = ctl.done
    assert (kill, revive) == ("kill", "revive") and victim == again
    assert victim != fabric.nodes[0].node_id  # never the presenter's laptop
    assert "the planner split the job" in done.stdout
    assert f"({victim}): lost," in done.stdout  # its lane was lost, the job still done
    fabric.wait_all_connected(within=60)


def test_04_planner_chooses_local_then_mode1_then_mode3(
    fabric: Fabric, tmp_path: Path
) -> None:
    env = _env(fabric, tmp_path, fabric.nodes[0].data_dir)
    done = run_demo("04-planner-explains", env, tmp_path / "w04")
    assert_pass(done, "04-planner-explains")
    tail = done.stdout.split("Three jobs, three answers", 1)[1]
    assert "job A: local" in tail and "job B: mode1" in tail and "job C: mode3" in tail


def test_reset_cancels_leftover_jobs_and_revokes_unused_invites(
    fabric: Fabric, tmp_path: Path
) -> None:
    env = _env(fabric, tmp_path, fabric.nodes[0].data_dir)
    work = tmp_path / "w05"
    done = run_demo("05-install-join", env, work)  # no --join-cmd: invite only
    assert_pass(done, "05-install-join")
    state = json.loads((work / "state.json").read_text(encoding="utf-8"))
    (invite_id,) = state["invites"]
    cli = [sys.executable, "-m", "meshforge.cli"]

    def submit(name: str) -> str:
        project = tmp_path / name
        project.mkdir()
        (project / "main.py").write_text(
            "import time\ntime.sleep(120)\n", encoding="utf-8"
        )
        (project / "compute.yaml").write_text(
            f"name: {name}\nentrypoint: [python, main.py]\n", encoding="utf-8"
        )
        sub = subprocess.run(
            [*cli, "run", str(project), "--node", fabric.nodes[1].node_id],
            env=env, capture_output=True, text=True, timeout=120, check=True,
        )  # fmt: skip
        return sub.stdout.split()[0]

    # a leftover demo job, as an interrupted show leaves it: recorded in state
    job_id = submit("meshforge-demo-05-install-join-leftover")
    state["jobs"].append(job_id)
    (work / "state.json").write_text(json.dumps(state), encoding="utf-8")
    # someone else's job that merely looks like a demo job: never touched
    user_job = submit("demo-render")
    reset = run_demo("05-install-join", env, work, "--reset")
    assert reset.returncode == 0 and "DEMO 05-install-join: RESET" in reset.stdout, (
        reset.stdout
    )
    assert f"$ compute cancel {job_id}" in reset.stdout
    assert user_job not in reset.stdout, reset.stdout
    assert f"$ compute fabric invites --revoke {invite_id}" in reset.stdout
    invites = fabric.call("GET", "/v1/fabric/invites").json()["invites"]
    row = next(i for i in invites if i["id"] == invite_id)
    assert row["state"] == "revoked", row
    again = run_demo("05-install-join", env, work, "--reset")  # idempotent
    assert again.returncode == 0 and "compute cancel" not in again.stdout, again.stdout
    user = fabric.call("GET", f"/v1/jobs/{user_job}").json()
    assert user.get("job", user)["state"] not in ("cancelled", "failed"), user
    fabric.call("POST", f"/v1/jobs/{user_job}/cancel")


def test_05_join_a_new_laptop_with_the_demo_code(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=2, fabric_ca=True, enroll_agents=False, start_agents=False
    )
    agent_env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    # agent 0 joins the harness way (the fabric's first laptop); agent 1 is
    # the "new laptop" the demo joins with the code it made
    made = fabric.call("POST", "/v1/fabric/invites", json={"ttl_s": 300}).json()
    first = subprocess.run(
        [sys.executable, "-m", "meshforge.agent", "join", "-", "--config",
         str(fabric.nodes[0].config)],
        input=made["code"] + "\n", env=agent_env, capture_output=True, text=True,
        timeout=60, check=False,
    )  # fmt: skip
    assert first.returncode == 0, first.stderr
    join_cmd = shlex.join(
        [sys.executable, "-m", "meshforge.agent", "join", "-", "--config",
         str(fabric.nodes[1].config)]
    )  # fmt: skip
    env = _env(fabric, tmp_path, None)
    done = run_demo("05-install-join", env, tmp_path / "w05", "--join-cmd", join_cmd)
    assert_pass(done, "05-install-join")
    assert "a new node joined with the code" in done.stdout
    for node in fabric.nodes:
        shown = subprocess.run(
            [sys.executable, "-m", "meshforge.agent", "identity", "show", "--json",
             "--config", str(node.config)],
            env=agent_env, capture_output=True, text=True, timeout=60, check=True,
        )  # fmt: skip
        node.node_id = json.loads(shown.stdout)["node_id"]
    assert f"joined as {fabric.nodes[1].node_id}" in done.stdout
    fabric.start_agents()  # the joined laptop connects with its new certificate
    state = json.loads((tmp_path / "w05" / "state.json").read_text(encoding="utf-8"))
    assert state["invites"] == []  # used, nothing left to revoke
