"""V0.7 G7 criterion 4: a job secret's value never appears on any surface -
after a run that **fails** (the workload prints every form of the value on
both streams and dies with the value in its exception) and one that is
**cancelled** while it prints.

Real controller + agent processes. Searched, for the value and its forms
(raw, base64 std/url padded/unpadded, hex lower/upper, URL-encoded; D144):

* every file under the fabric's work folder: the controller's and the
  agent's log files (their structlog JSON output, DEBUG level), data dirs,
  stored job logs, blobs;
* the job and attempt records (``GET /v1/jobs/{id}``: states, failure
  detail, attempts, events) and ``GET /v1/jobs/{id}/decisions``
  (``plan_decision``);
* every row of every table of the controller's database as JSON (job,
  attempt, transitions, phase and transfer telemetry, plan decisions, audit,
  sessions...; the ``secret`` table holds only ciphertext);
* ``compute explain`` and ``compute jobs <id>`` output (a real CLI process);
* error bodies of the secrets API for a value sent with a bad name and with
  an oversized body.

The stored logs do carry the marker ``[secret:C4_KEY]`` - the value was
printed and replaced, not simply never printed.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import NORMAL_USER, Fabric, Timing
from mf_redaction_oracle import forms_of
from pydantic import SecretStr
from sqlalchemy import text

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import build_tree
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.cluster, pytest.mark.security, pytest.mark.timeout(300)]

TIMING = Timing(
    name="g7c4",
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
NAME = "C4_KEY"
TERMINAL = {"succeeded", "failed", "cancelled"}

MAIN = """\
import base64, os, sys, time, urllib.parse
v = open(os.path.join(os.environ["MESHFORGE_SECRETS_DIR"], "C4_KEY"), "rb").read()
forms = [v, base64.b64encode(v), base64.urlsafe_b64encode(v).rstrip(b"="),
         v.hex().encode(), v.hex().upper().encode(),
         urllib.parse.quote_from_bytes(v).encode()]
for form in forms:
    sys.stdout.buffer.write(b"value: " + form + b"\\n")
    sys.stderr.buffer.write(b"value: " + form + b"\\n")
sys.stdout.flush(); sys.stderr.flush()
if sys.argv[1] == "fail":
    raise RuntimeError("failing with " + v.decode())
time.sleep(120)
"""


def _wait(fabric: Fabric, job_id: str, states: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = dict(fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json())
        if job["state"] in states:
            return job
        time.sleep(0.1)
    fabric.fail(f"job {job_id} never reached {states}")


def _submit(fabric: Fabric, tree: str, node_id: str, mode: str) -> str:
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        as_user=NORMAL_USER,
        json={
            "manifest": {
                "name": f"c4-{mode}",
                "entrypoint": ["python", "main.py", mode],
                "secrets": [NAME],
                "retry": {"max_attempts": 1},
            },
            "tree_id": tree,
            "placement": {"node_id": node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"])


def _cli(fabric: Fabric, tmp_path: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__CA_FILE": str(fabric.ca_dir / "ca.crt"),
            "MESHFORGE_CLI__TOKEN": fabric.api_token(NORMAL_USER),
            "MESHFORGE_DATA_DIR": str(tmp_path / "cli-data"),
            "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
        }
    )
    done = subprocess.run(
        [sys.executable, "-m", "meshforge.cli", *args],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, (args, done.stdout[-2000:], done.stderr[-2000:])
    return done.stdout + done.stderr


async def _database_rows(pg: DatabaseSettings) -> dict[str, list[str]]:
    engine = create_engine(pg)
    try:
        async with engine.connect() as conn:
            tables = [
                str(name)
                for (name,) in await conn.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                    )
                )
            ]
            rows: dict[str, list[str]] = {}
            for table in tables:
                result = await conn.execute(
                    text(f'SELECT row_to_json(t)::text FROM "{table}" t')  # noqa: S608
                )
                rows[table] = [str(r) for (r,) in result]
            return rows
    finally:
        await engine.dispose()


def test_a_secret_is_on_no_surface_after_a_failed_and_a_cancelled_run(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: DatabaseSettings
) -> None:
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        log_level="DEBUG",
    )
    (node,) = fabric.nodes
    value = b"MF-C4-" + secrets.token_hex(16).encode()
    forms = {f for f in forms_of(value) if len(f) >= 8}
    stored = fabric.call(
        "PUT", f"/v1/secrets/{NAME}", as_user=NORMAL_USER, content=value
    )
    assert stored.status_code == 200, stored.text

    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(MAIN)
    built = build_tree(root, chunk_size=256 * 1024)
    client = ControllerClient(
        fabric.http_base,
        timeout_s=30,
        token=SecretStr(fabric.api_token(NORMAL_USER)),
        ssl_context=fabric.ssl_context,
    )
    try:
        upload_tree(client, built)
    finally:
        client.close()
    tree = str(built.tree.tree_id)

    failed_id = _submit(fabric, tree, node.node_id, "fail")
    failed = _wait(fabric, failed_id, TERMINAL)
    assert failed["state"] == "failed", failed
    cancel_id = _submit(fabric, tree, node.node_id, "sleep")
    _wait(fabric, cancel_id, {"running"})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:  # its prints reached the controller
        body = fabric.call(
            "GET",
            f"/v1/jobs/{cancel_id}/logs",
            as_user=NORMAL_USER,
            params={"stream": "stdout"},
        ).content
        if body.count(f"[secret:{NAME}]".encode()) >= 1:
            break
        time.sleep(0.1)
    cancelled = fabric.call("POST", f"/v1/jobs/{cancel_id}/cancel", as_user=NORMAL_USER)
    assert cancelled.status_code in (200, 202), cancelled.text
    assert _wait(fabric, cancel_id, TERMINAL)["state"] == "cancelled"

    surfaces: dict[str, bytes] = {}
    for job_id in (failed_id, cancel_id):
        for path in (f"/v1/jobs/{job_id}", f"/v1/jobs/{job_id}/decisions"):
            surfaces[path] = fabric.call("GET", path, as_user=NORMAL_USER).content
        for stream in ("stdout", "stderr"):
            log = fabric.call(
                "GET",
                f"/v1/jobs/{job_id}/logs",
                as_user=NORMAL_USER,
                params={"stream": stream},
            ).content
            # the value was printed and replaced - not just never printed
            assert f"[secret:{NAME}]".encode() in log, (job_id, stream, log[:300])
            surfaces[f"logs {job_id} {stream}"] = log
        for args in (("explain", job_id), ("jobs", job_id)):
            surfaces[f"compute {' '.join(args)}"] = _cli(
                fabric, tmp_path, *args
            ).encode()
    # Error bodies: the value sent where it is refused.
    bad_name = fabric.call(
        "PUT", "/v1/secrets/bad-name!", as_user=NORMAL_USER, content=value
    )
    assert 400 <= bad_name.status_code < 500
    surfaces["error bad name"] = bad_name.content
    oversized = fabric.call(
        "PUT",
        f"/v1/secrets/{NAME}_BIG",
        as_user=NORMAL_USER,
        content=value * (70 * 1024 // len(value)),
    )
    assert 400 <= oversized.status_code < 500
    surfaces["error oversized"] = oversized.content

    rows = asyncio.run(_database_rows(pg_settings))
    assert {"job", "attempt", "audit_event"} <= set(rows), sorted(rows)
    for table, texts in rows.items():
        surfaces[f"db {table}"] = "\n".join(texts).encode()

    for path in fabric.workdir.rglob("*"):
        if path.is_file() and not path.is_symlink() and not path.name.endswith(".sock"):
            try:
                surfaces[f"file {path.relative_to(fabric.workdir)}"] = path.read_bytes()
            except OSError:
                continue

    leaks = sorted(
        f"{where}: {form[:12]!r}"
        for where, data in surfaces.items()
        for form in forms
        if form in data
    )
    assert leaks == [], leaks[:10]
    assert any(name.startswith("file ") and ".log" in name for name in surfaces)
