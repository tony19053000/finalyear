"""V0.7 G4 cluster (D141): a full job run as a **normal user** through the real
``compute`` CLI against real controller + agent processes.

* ``compute login --fingerprint`` (first contact: the CA is read from the TLS
  handshake and kept only if its full fingerprint matches) stores the token and
  ``ca.crt`` privately; a wrong fingerprint is refused before the password is
  sent;
* ``compute whoami`` / ``compute run -f`` / ``compute jobs`` / ``compute get``
  as that user, with no token or CA configured anywhere but the login;
* another user's (the admin's) job is "no such job" to them; ``compute fabric
  invite`` is refused 403;
* ``compute logout`` revokes the token on the controller: the next command says
  "run compute login";
* no token or password in any CLI output.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import ADMIN_USER, NORMAL_USER, Fabric, Timing

from meshforge.common import pki
from meshforge.common.ids import new_fabric_id

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(300)]

TIMING = Timing(
    name="g4-users",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.5,
    lease_multiplier=6,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=2.0,
)
EXEC = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}
PROGRAM = """\
import os
os.makedirs("results", exist_ok=True)
open("results/r.txt", "w").write("made by alice\\n")
print("hello from alice's job", flush=True)
"""


def _compute(
    fabric: Fabric, home: Path, *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """``compute`` with NOTHING configured but the controller URL: the token
    and the CA come only from ``compute login``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__CREDENTIALS_DIR": str(home / "creds"),
            "MESHFORGE_DATA_DIR": str(home / "data"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_DATA_HOME": str(home / "xdg-data"),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "meshforge.cli", *args],
        env=env,
        cwd=home,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )


@pytest.mark.security
def test_a_normal_user_runs_a_job_through_the_cli(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    (node,) = fabric.nodes
    home = tmp_path / "alice"
    home.mkdir()
    password = fabric.password(NORMAL_USER)
    secrets_seen = (password, fabric.api_token(NORMAL_USER), fabric.api_token())
    ca = pki.read_cert(fabric.ca_dir / "ca.crt", what="ca")
    other = pki.create_ca(str(new_fabric_id()))

    def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        done = _compute(fabric, home, *args, stdin=stdin)
        for secret in secrets_seen:
            assert secret not in done.stdout and secret not in done.stderr, args
        return done

    # A wrong fingerprint: refused before anything is sent; nothing stored.
    wrong = run(
        "login", "--fingerprint", pki.cert_fingerprint(other.cert),
        "--user", NORMAL_USER, "--password-stdin", stdin=password + "\n",
    )  # fmt: skip
    assert wrong.returncode != 0
    assert "does not hold the fabric CA" in wrong.stderr
    assert not (home / "creds" / "login.json").exists()

    # First contact with the right fingerprint.
    login = run(
        "login", "--fingerprint", pki.format_fingerprint(pki.cert_fingerprint(ca)),
        "--user", NORMAL_USER, "--password-stdin", stdin=password + "\n",
    )  # fmt: skip
    assert login.returncode == 0, login.stderr
    assert f"as {NORMAL_USER} (user)" in login.stdout
    stored = home / "creds" / "login.json"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert (home / "creds" / "ca.crt").read_bytes() == pki.cert_to_pem(ca)

    who = run("whoami")
    assert who.returncode == 0 and f"{NORMAL_USER} (user)" in who.stdout

    project = home / "project"
    project.mkdir()
    (project / "main.py").write_text(PROGRAM)
    (project / "compute.yaml").write_text(
        "name: alice-job\nentrypoint: [python, main.py]\noutputs: [results/]\n"
    )
    done = run("run", str(project), "--node", node.node_id, "-f")
    assert done.returncode == 0, (done.stdout[-2000:], done.stderr[-2000:])
    assert "hello from alice's job" in done.stdout
    job_id = done.stdout.splitlines()[0].strip()
    listed = run("jobs", "--json")
    assert job_id in {j["job_id"] for j in json.loads(listed.stdout)["jobs"]}
    got = run("get", job_id, "--dest", str(home / "out"))
    assert got.returncode == 0, got.stderr
    assert (home / "out" / "results" / "r.txt").read_text() == "made by alice\n"
    owner = fabric.call("GET", f"/v1/jobs/{job_id}")  # the admin sees it too
    assert owner.status_code == 200

    # The admin's job does not exist for alice.
    admin_job = fabric.call(
        "POST",
        "/v1/jobs",
        as_user=ADMIN_USER,
        json={
            "manifest": {"name": "admins", "entrypoint": ["python", "main.py"]},
            "tree_id": owner.json()["tree_id"],
            "placement": {"node_id": node.node_id},
        },
    )
    assert admin_job.status_code == 201, admin_job.text
    hidden = run("jobs", str(admin_job.json()["job_id"]))
    assert hidden.returncode != 0 and "no such job" in hidden.stderr
    invite = run("fabric", "invite")
    assert invite.returncode != 0 and "(403)" in invite.stderr

    out = run("logout")
    assert out.returncode == 0 and "logged out" in out.stdout
    assert not stored.exists()
    again = _compute(fabric, home, "jobs")
    assert again.returncode != 0
    # The CA went with the login, so the CLI cannot even verify the controller;
    # with the CA named it gets the 401 answer.
    named = _compute(fabric, home, "--ca", str(fabric.ca_dir / "ca.crt"), "jobs")
    assert named.returncode != 0 and "compute login" in named.stderr
    # The revoked token is refused by the controller itself.
    tokens = fabric.call("GET", "/v1/auth/tokens?all=true").json()["tokens"]
    alice = [t for t in tokens if t["user"] == NORMAL_USER and t["name"] == "compute"]
    assert any(t["state"] == "revoked" for t in alice), alice
