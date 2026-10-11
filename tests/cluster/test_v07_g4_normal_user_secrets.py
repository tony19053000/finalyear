"""V0.7 G4 deliverable 6 (D141 + D146): a full job run as a **normal user** -
once after ``compute login`` with a password, once with a bare API token - and
neither secret appears in plaintext anywhere the system writes or sends.

Real controller + agent processes; every link recorded (``record_links``).

* run P ("password"): ``compute login --ca --user alice --password-stdin``,
  then ``compute run -f`` (REST submit + the wss follow) and ``compute get``
  with nothing but the stored login;
* run T ("token"): a fresh CLI home, no login - ``compute --token-file`` with
  an API token alice got from a separate ``POST /v1/auth/login``;
* both jobs succeed and are owned by alice (a ``user``, not an admin) in the
  database.

Then the markers - alice's password, the CLI login's token, the bare API
token - are searched in: every recorded byte stream of every link (both
directions, not truncated); every file under the fabric's work dir (every
process log at DEBUG, the controller's and agents' data dirs, the CAS); every
row of every table in the database (as text); every CLI stdout/stderr. Each
place has a positive control so an empty search cannot pass: the DB holds the
SHA-256 of both tokens, the controller log holds alice's login events, the
rest link carried the login requests.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import NORMAL_USER, Fabric, Timing
from sqlalchemy import text

from meshforge.common.api_token import api_token_hash
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(300)]

TIMING = Timing(
    name="g4-user-secrets",
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
PROGRAM = """\
import os, sys
os.makedirs("results", exist_ok=True)
open("results/r.txt", "w").write("made by " + sys.argv[1] + "\\n")
print("job of alice", sys.argv[1], flush=True)
"""


def _compute(
    fabric: Fabric, home: Path, *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """``compute`` with nothing configured but the controller URL (through the
    recorded rest link) and a private home."""
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


def _project(home: Path, tag: str) -> Path:
    project = home / "project"
    project.mkdir(parents=True)
    (project / "main.py").write_text(PROGRAM)
    (project / "compute.yaml").write_text(
        f"name: alice-{tag}\nentrypoint: [python, main.py, {tag}]\n"
        "outputs: [results/]\n"
    )
    return project


def _db_rows(
    settings: DatabaseSettings,
) -> tuple[dict[str, list[str]], dict[str, tuple[str, str]]]:
    """Every row of every table, as PostgreSQL's text form of the row; and
    job id -> (owner name, owner role)."""

    async def dump() -> tuple[dict[str, list[str]], dict[str, tuple[str, str]]]:
        engine = create_engine(settings)
        try:
            async with engine.connect() as db:
                tables = (
                    await db.execute(
                        text(
                            "SELECT tablename FROM pg_tables "
                            "WHERE schemaname = 'public' ORDER BY tablename"
                        )
                    )
                ).scalars()
                rows: dict[str, list[str]] = {}
                for table in list(tables):
                    found = await db.execute(
                        text(f'SELECT t::text FROM "{table}" AS t')  # noqa: S608 - catalog names
                    )
                    rows[table] = [str(r) for r in found.scalars()]
                owned = await db.execute(
                    text(
                        "SELECT j.id, u.name, u.role::text FROM job j "
                        "JOIN app_user u ON u.id = j.owner_user_id"
                    )
                )
                return rows, {str(j): (str(n), str(r)) for j, n, r in owned.all()}
        finally:
            await engine.dispose()

    return asyncio.run(dump())


def _files(root: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_normal_user_runs_with_password_and_token_and_neither_leaks(
    make_fabric: Callable[..., Fabric],
    pg_settings: DatabaseSettings,
    tmp_path: Path,
) -> None:
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        record_links=True,
        log_level="DEBUG",
    )
    (node,) = fabric.nodes
    assert fabric.rest_link is not None
    password = fabric.password(NORMAL_USER)
    bare_token = fabric.api_token(NORMAL_USER)  # its own POST /v1/auth/login
    ca = str(fabric.ca_dir / "ca.crt")
    outputs: list[str] = []

    def compute(home: Path, *args: str, stdin: str | None = None) -> str:
        done = _compute(fabric, home, *args, stdin=stdin)
        outputs.extend((done.stdout, done.stderr))
        assert done.returncode == 0, (args, done.stdout[-2000:], done.stderr[-2000:])
        return done.stdout

    # ---- run P: password login, then the stored login only.
    home_p = tmp_path / "alice-password"
    home_p.mkdir()
    login = compute(
        home_p, "login", "--ca", ca, "--user", NORMAL_USER, "--password-stdin",
        stdin=password + "\n",
    )  # fmt: skip
    assert f"as {NORMAL_USER} (user)" in login
    login_token = str(
        json.loads((home_p / "creds" / "login.json").read_text())["token"]
    )
    assert login_token != bare_token
    ran_p = compute(
        home_p, "run", str(_project(home_p, "pw")), "--node", node.node_id, "-f"
    )
    assert "job of alice pw" in ran_p
    job_p = ran_p.splitlines()[0].strip()
    compute(home_p, "get", job_p, "--dest", str(home_p / "out"))
    assert (home_p / "out" / "results" / "r.txt").read_text() == "made by pw\n"

    # ---- run T: a bare API token, no login on this CLI home.
    home_t = tmp_path / "alice-token"
    home_t.mkdir()
    token_file = home_t / "token"
    fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(bare_token + "\n")
    with_token = ("--token-file", str(token_file), "--ca", ca)
    who = compute(home_t, *with_token, "whoami")
    assert f"{NORMAL_USER} (user)" in who
    who_json = compute(home_t, *with_token, "whoami", "--json")
    ran_t = compute(
        home_t, *with_token, "run", str(_project(home_t, "tok")), "--node",
        node.node_id, "-f",
    )  # fmt: skip
    assert "job of alice tok" in ran_t
    job_t = ran_t.splitlines()[0].strip()
    compute(home_t, *with_token, "get", job_t, "--dest", str(home_t / "out"))
    assert (home_t / "out" / "results" / "r.txt").read_text() == "made by tok\n"

    # ---- both jobs ran as alice, a normal user.
    rows, owners = _db_rows(pg_settings)
    assert owners[job_p] == owners[job_t] == (NORMAL_USER, "user"), owners
    alice_id = str(json.loads(who_json)["user"]["id"])

    markers = {
        "password": password.encode(),
        "login_token": login_token.encode(),
        "api_token": bare_token.encode(),
        # The secret half alone (the scrubber's shape is mfat_ + 43).
        "login_token_body": login_token[5:].encode(),
        "api_token_body": bare_token[5:].encode(),
    }
    leaks: list[str] = []

    # 1. The wire: every link, both directions.
    seen: dict[str, int] = {}
    for link in fabric.links():
        recording = link.recording()
        assert not recording.truncated, link.name
        seen[link.name] = recording.total_bytes
        for name, marker in markers.items():
            leaks.extend(
                f"{name} on link {link.name} conn {conn} {direction}"
                for conn, direction in recording.find(marker)
            )
    assert {"rest", "agent0"} <= set(seen) and seen["rest"] > 0, seen

    # 2. Every file the fabric wrote: logs (DEBUG), data dirs, CAS.
    fabric_files = _files(fabric.workdir)
    controller_logs = b"".join(
        data
        for path, data in fabric_files.items()
        if path.name.startswith("controller.")
    )
    assert controller_logs.count(b"controller.auth.login") >= 2, "no login events"
    assert alice_id.encode() in controller_logs
    # The CLI homes, but for the two places the user keeps a token on purpose.
    kept = {home_p / "creds" / "login.json", token_file}
    cli_files = {
        p: d for p, d in (_files(home_p) | _files(home_t)).items() if p not in kept
    }
    for path, data in (fabric_files | cli_files).items():
        for name, marker in markers.items():
            if marker in data:
                leaks.append(f"{name} in file {path}")

    # 3. Every database row (the tokens only as their SHA-256).
    dumped = "\n".join(line for table in rows.values() for line in table)
    assert api_token_hash(login_token) in dumped
    assert api_token_hash(bare_token) in dumped
    for table, lines in rows.items():
        for name, marker in markers.items():
            if any(marker.decode() in line for line in lines):
                leaks.append(f"{name} in table {table}")

    # 4. Every CLI output.
    for index, out in enumerate(outputs):
        for name, marker in markers.items():
            if marker.decode() in out:
                leaks.append(f"{name} in CLI output #{index}")

    assert leaks == [], leaks
