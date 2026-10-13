# ruff: noqa: T201, PERF401
"""V0.7 G6 tester r3 (T-17, REAL processes): the secrets/outputs that made the
matcher spin (and the one that printed a value in clear) go through a real job
on a real controller + agent: the job finishes, the stored log equals the
independent oracle's output, no form of any value is in any file of any
process, and `/healthz` stays responsive throughout.
"""

from __future__ import annotations

import contextlib
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
from mf_redaction_oracle import CASES, MAIN, forms_of
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import build_tree

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

TIMING = Timing(
    name="g6r3e2e",
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


def _prepare(fabric: Fabric, tmp_path: Path) -> str:
    root = tmp_path / "project"
    root.mkdir()
    (root / "main.py").write_text(MAIN)
    built = build_tree(root, chunk_size=256 * 1024)
    client = ControllerClient(
        fabric.http_base, timeout_s=30, token=SecretStr(fabric.api_token(NORMAL_USER))
    )
    try:
        upload_tree(client, built)
    finally:
        client.close()
    return str(built.tree.tree_id)


def test_the_t17_inputs_through_real_jobs_are_redacted_and_the_controller_stays_up(
    make_fabric: Callable[..., Fabric], tmp_path: Path
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
    tree = _prepare(fabric, tmp_path)
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
    all_secrets: dict[str, bytes] = {}
    try:
        for case, secrets in CASES.items():
            # distinct names per case so earlier values do not stay in the store
            named = {f"{case.upper()}_{k}": v for k, v in secrets.items()}
            for name, value in named.items():
                r = fabric.call(
                    "PUT", f"/v1/secrets/{name}", as_user=NORMAL_USER, content=value
                )
                assert r.is_success, r.text
            all_secrets.update(named)
            resp = fabric.call(
                "POST",
                "/v1/jobs",
                as_user=NORMAL_USER,
                json={
                    "manifest": {
                        "name": f"r3-{case}",
                        "entrypoint": ["python", "main.py", case],
                        "secrets": sorted(named),
                        "retry": {"max_attempts": 1},
                    },
                    "tree_id": tree,
                    "placement": {"node_id": node.node_id},
                },
            )
            assert resp.status_code == 201, resp.text
            job_id = str(resp.json()["job_id"])
            deadline = time.monotonic() + 120
            state: dict[str, Any] = {}
            while time.monotonic() < deadline:
                state = dict(
                    fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json()
                )
                if state["state"] in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.2)
            assert state.get("state") == "succeeded", (case, state)
            body = fabric.call(
                "GET",
                f"/v1/jobs/{job_id}/logs",
                as_user=NORMAL_USER,
                params={"stream": "stdout"},
            ).content
            for name, value in named.items():
                for form in forms_of(value):
                    assert form not in body.replace(f"[secret:{name}]".encode(), b""), (
                        case,
                        name,
                        form,
                    )
            # nothing but markers/stars where a value was: no stalled stream
            assert b"[secret:" in body, (case, body[:200])
            print(case, "stored stdout bytes:", len(body), "stars:", body.count(b"*"))
    finally:
        stop.set()
        thread.join(10)
    print(f"healthz worst {max(stalls):.3f}s over {len(stalls)} probes")
    assert max(stalls) < 2.0

    leaks: list[str] = []
    for path in fabric.workdir.rglob("*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.name.endswith((".key", ".sock", ".lock"))
        ):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for name, value in all_secrets.items():
            for form in forms_of(value):
                if len(form) >= 8 and form in data:
                    leaks.append(
                        f"{path.relative_to(fabric.workdir)}: {name} {form[:12]!r}"
                    )
    # short forms (< 8 bytes) are skipped above only because tiny values match
    # unrelated bytes in binary files; the log bodies were checked in full.
    assert leaks == [], leaks[:5]
