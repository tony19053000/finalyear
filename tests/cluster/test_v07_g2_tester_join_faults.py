"""V0.7 G2 tester cluster: failure injection around ``meshforge-agent join``.

Real controller process (CA made by ``fabric create``), real agents, real
``meshforge-agent join -`` subprocesses with the code on stdin:

* controller frozen (SIGSTOP = a black-holed network): the join times out cleanly,
  stores nothing, and the same code works after SIGCONT;
* the join process itself killed (SIGKILL) while the controller is frozen: nothing
  half-written, the code unspent;
* controller killed (SIGKILL) then restarted on the same enrolment port: an
  outstanding invite still works (it lives in PostgreSQL), the CA pin is unchanged;
* a joined agent is not disturbed (G2 does not yet require the certificate).
"""

# ruff: noqa: E501, SIM105  (tester file: adversarial literals, blunt asserts)

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import MakeFabric

from meshforge.common import join_code

pytestmark = [pytest.mark.docker]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["PYTHONUTF8"] = "1"
    return env


def _join_cmd(config: Path, *extra: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "meshforge.agent",
        "join",
        "-",
        "--config",
        str(config),
        *extra,
    ]


def _identity_dir(config: Path) -> Path:
    shown = subprocess.run(
        [sys.executable, "-m", "meshforge.agent", "identity", "show", "--json", "--config", str(config)],
        env=_env(), capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    return Path(json.loads(shown.stdout)["path"])


def test_join_through_a_frozen_then_killed_controller(make_fabric: MakeFabric) -> None:
    port = _free_port()
    fabric = make_fabric(
        agents=2,
        fabric_ca=True,
        controller_extra={"enroll": {"bind": f"127.0.0.1:{port}"}},
    )
    assert fabric.enroll_target.endswith(f":{port}"), fabric.enroll_target
    controller = fabric.controller
    assert controller is not None
    first, second = fabric.nodes

    def invite() -> str:
        made = fabric.call("POST", "/v1/fabric/invites", json={"ttl_s": 600})
        assert made.status_code == 201, made.text
        return str(made.json()["code"])

    # ---- 1. frozen controller: clean timeout, nothing stored, same code later works
    code = invite()
    controller.suspend()
    try:
        start = time.monotonic()
        done = subprocess.run(
            _join_cmd(first.config, "--timeout", "2"), input=code + "\n", env=_env(),
            capture_output=True, text=True, timeout=90, check=False,
        )  # fmt: skip
        assert done.returncode != 0
        assert time.monotonic() - start < 60
        assert code not in done.stdout + done.stderr
        assert "Traceback" not in done.stderr
        idir = _identity_dir(first.config)
        assert not (idir / "node.crt").exists() and not (idir / "ca.crt").exists()

        # ---- 2. the join process killed mid-flight: no partial files
        proc = subprocess.Popen(
            _join_cmd(first.config, "--timeout", "60"), stdin=subprocess.PIPE, env=_env(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
        )  # fmt: skip
        assert proc.stdin is not None
        proc.stdin.write(code + "\n")
        proc.stdin.flush()
        time.sleep(1.0)
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=10)
        try:
            proc.stdin.close()
        except OSError:
            pass
        assert not (idir / "node.crt").exists() and not (idir / "ca.crt").exists()
    finally:
        controller.resume()
    invites = {
        i["id"]: i for i in fabric.call("GET", "/v1/fabric/invites").json()["invites"]
    }
    assert all(i["state"] == "pending" for i in invites.values()), (
        invites
    )  # nothing spent
    done = subprocess.run(
        _join_cmd(first.config), input=code + "\n", env=_env(),
        capture_output=True, text=True, timeout=90, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr

    # ---- 3. controller SIGKILL + restart on the same enrolment port
    code2 = invite()
    pin = join_code.decode(code2).ca_pin
    fabric.kill_controller()
    gone = subprocess.run(
        _join_cmd(second.config, "--timeout", "2"), input=code2 + "\n", env=_env(),
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert gone.returncode != 0 and code2 not in gone.stdout + gone.stderr
    fabric.restart_controller()
    assert fabric.enroll_target.endswith(f":{port}"), (
        "the enrolment port must be stable"
    )
    assert join_code.decode(code2).ca_pin == pin
    done = subprocess.run(
        _join_cmd(second.config), input=code2 + "\n", env=_env(),
        capture_output=True, text=True, timeout=90, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    nodes = fabric.nodes_by_id()
    assert nodes is not None
    for node in fabric.nodes:
        assert nodes[node.node_id]["certificate"] is not None
    # no process log contains any group of either code
    logs = fabric.workdir / "logs"
    text = "".join(
        p.read_text("utf-8", "replace") for p in logs.iterdir() if p.is_file()
    )
    for secret in (code, code2):
        for group in secret.split("-")[1:-1]:
            assert group not in text
