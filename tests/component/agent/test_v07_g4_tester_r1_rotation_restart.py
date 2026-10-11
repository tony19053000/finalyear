"""V0.7 G4 tester round 1: certificate rotation while the CONTROLLER restarts,
with a real agent and a real job (brief 5d item 6: 'cert near not_after ->
agent hands over ... no attempt LOST'; failure model: controller restart).

The agent's node certificate is past two thirds of its life, a 14 s job runs on
it, the agent renews (the controller issues a pending certificate) and the
controller is stopped right then and started again on the same ports. Whatever
state the rotation was in (swap done or not, hand-over done or not), the agent
must end up on one valid certificate, the controller's node row must name it,
every older serial must be refused, and the job must succeed as ONE attempt.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, wait_job
from mf_fabric import client_ssl_context, fabric_ca
from mf_jobs_harness import HARNESS_NODE_ID, token_of
from mf_session_client import free_port_pair
from pydantic import SecretStr

from meshforge.agent import enrollment
from meshforge.agent.identity import load_or_create
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common import pki
from meshforge.common.tree import build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]

MAIN = b"""\
import sys, time
time.sleep(float(sys.argv[2]))
open(sys.argv[1], "w").write("done")
"""
FAST_RENEW = {"renew_check_interval_s": 0.3, "renew_retry_base_s": 0.3}


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    from sqlalchemy import text

    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


def _short_enrolment(data_dir: Path, due_in_s: float) -> Any:
    identity = load_or_create(data_dir / "identity")
    ca = fabric_ca()
    skew = pki.CLOCK_SKEW.total_seconds()
    lifetime = dt.timedelta(seconds=1.5 * (due_in_s + skew) - skew)
    csr = pki.build_csr(
        identity.private_key, fabric_id=ca.fabric_id, node_id=identity.node_id
    )
    cert = pki.issue_node_cert(
        ca, csr, node_id=identity.node_id, fabric_id=ca.fabric_id, lifetime=lifetime
    )
    enrollment.store(identity, cert, ca.cert, force=True)
    return cert


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
        trust_env=False,
        verify=client_ssl_context(node_id=HARNESS_NODE_ID),
    )


async def _until(check: Any, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.05)


@pytest.mark.parametrize("down_s", [0.5, 4.0], ids=["quick-restart", "long-outage"])
async def test_controller_restart_during_a_renewal_loses_nothing(
    pg_settings: DatabaseSettings, tmp_path: Path, down_s: float
) -> None:
    grpc_port, http_port = free_port_pair()
    settings = make_controller_settings(
        pg_settings,
        grpc_bind=f"127.0.0.1:{grpc_port}",
        http_bind=f"127.0.0.1:{http_port}",
        jobs={"dispatch_interval_s": 0.1, "reconnect_grace_s": 60.0},
        renewal={"min_interval_s": 1.0},
        **tolerant_liveness(),
    )
    old_cert = _short_enrolment(tmp_path / "agent-a", due_in_s=5.0)
    old_serial = format(old_cert.serial_number, "x")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_bytes(MAIN)
    built = build_tree(project)

    a: ExecAgent | None = None
    try:
        async with ControllerRuntime.start(settings) as rt1:
            client = ControllerClient(
                f"https://127.0.0.1:{rt1.http_port}",
                timeout_s=30,
                token=SecretStr(token_of(rt1)),
            )
            try:
                await asyncio.to_thread(upload_tree, client, built)
            finally:
                client.close()
            a = await ExecAgent.start(
                f"127.0.0.1:{grpc_port}",
                tmp_path / "agent-a",
                agent_overrides={"cert": FAST_RENEW},
                renew=True,
            )
            async with _http(rt1) as http:
                posted = await http.post(
                    "/v1/jobs",
                    json={
                        "manifest": {
                            "name": "restart-during-renewal",
                            "entrypoint": ["python", "main.py", "out.txt", "14"],
                        },
                        "tree_id": str(built.tree.tree_id),
                        "placement": {"node_id": a.node_id},
                    },
                )
                assert posted.status_code == 201, posted.text
                job_id = str(posted.json()["job_id"])
                await wait_job(
                    http, job_id, states=frozenset({"running"}), timeout_s=30
                )
            # The moment the controller has issued the renewal, take it down.
            assert a.client.renewer is not None
            await _until(
                lambda: (
                    (a.client.renewer is not None and a.client.renewer.renewals >= 1)
                    or a.client.stream_cert_serial != old_serial
                ),
                timeout_s=30,
                what="the agent never renewed",
            )
        # --- controller is down here; the job keeps running on the agent
        await asyncio.sleep(down_s)
        async with ControllerRuntime.start(settings) as rt2:
            await _until(
                lambda: (
                    a is not None
                    and a.client.stream_cert_serial not in (None, old_serial)
                ),
                timeout_s=60,
                what="the agent never reconnected on a renewed certificate",
            )
            async with _http(rt2) as http:
                job = await wait_job(http, job_id, timeout_s=90)
            assert job["state"] == "succeeded", job
            assert [(x["node_id"], x["state"]) for x in job["attempts"]] == [
                (a.node_id, "done")
            ], job["attempts"]
            current = a.client.stream_cert_serial
            on_disk = enrollment.load(load_or_create(tmp_path / "agent-a" / "identity"))
            assert on_disk is not None
            assert format(on_disk.node_cert.serial_number, "x") == current
            rows = await _sql(
                pg_settings,
                "SELECT cert_serial, pending_cert_serial, state FROM node "
                "WHERE node_id = :n",
                n=a.node_id,
            )
            assert rows == [(current, None, "online")], rows
            assert await _sql(
                pg_settings,
                "SELECT reason FROM revoked_cert WHERE serial = :s",
                s=old_serial,
            ) == [("superseded",)]
            ends = {
                r[0]
                for r in await _sql(
                    pg_settings,
                    "SELECT end_reason FROM node_session WHERE node_id = :n "
                    "AND end_reason IS NOT NULL",
                    n=a.node_id,
                )
            }
            assert not ends & {"cert_expired", "cert_revoked"}, ends
    finally:
        if a is not None:
            await a.stop()
