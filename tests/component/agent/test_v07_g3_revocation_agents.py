"""V0.7 G3 deliverable 6 (0.7.7a, D138) with real agents: a real controller
(PostgreSQL, mTLS gRPC, HTTPS) and two real agents A and B with TLS peer
servers.

A job runs on A; ``POST /v1/nodes/{A}/revoke``:

* A's session is cut ``PERMISSION_DENIED`` - the agent stops for good and
  says how to come back (a new identity: ``identity reset`` + ``join``);
* the attempt on A is ``LOST`` with detail ``node_revoked`` and the job is
  retried on B by the V0.6 node-loss path, where it succeeds;
* B learns A's serial (``RevocationUpdate``) and its peer server refuses A's
  certificate from then on (a TLS client with A's key gets nothing).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, wait_job
from mf_fabric import client_ssl_context, fabric_ca
from mf_jobs_harness import HARNESS_NODE_ID, token_of
from pydantic import SecretStr

from meshforge.agent.session.errors import NodeRevokedError
from meshforge.agent.session.state import AgentState
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common import pki
from meshforge.common.fabric_tls import client_context, peer_server_name
from meshforge.common.tree import build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(180)]

MAIN = b"""\
import sys, time
time.sleep(4)
open(sys.argv[1], "w").write("done")
"""


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


async def _agent(grpc_target: str, data_dir: Path) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={"peer": {"bind": "127.0.0.1", "port": 0}},
    )


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
        trust_env=False,
        verify=client_ssl_context(node_id=HARNESS_NODE_ID),
    )


@pytest_asyncio.fixture
async def project(runtime: ControllerRuntime, tmp_path: Path) -> AsyncIterator[str]:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_bytes(MAIN)
    built = build_tree(root)
    client = ControllerClient(
        f"https://127.0.0.1:{runtime.http_port}",
        timeout_s=30,
        token=SecretStr(token_of(runtime)),
    )
    try:
        await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()
    yield str(built.tree.tree_id)


async def _until_job(
    http: httpx.AsyncClient, job_id: str, check: Any, timeout_s: float = 30
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        job = (await http.get(f"/v1/jobs/{job_id}")).json()
        if check(job):
            return job
        assert time.monotonic() < deadline, job
        await asyncio.sleep(0.05)


@pytest.mark.security
async def test_revoking_a_busy_node_moves_its_job_and_closes_the_peer_plane(
    runtime: ControllerRuntime, grpc_target: str, project: str, tmp_path: Path
) -> None:
    a = await _agent(grpc_target, tmp_path / "agent-a")
    b: ExecAgent | None = None
    try:
        async with _http(runtime) as http:
            posted = await http.post(
                "/v1/jobs",
                json={
                    "manifest": {
                        "name": "revoke-me",
                        "entrypoint": ["python", "main.py", "out.txt"],
                    },
                    "tree_id": project,
                },
            )
            assert posted.status_code == 201, posted.text
            job_id = str(posted.json()["job_id"])
            await _until_job(
                http,
                job_id,
                lambda j: any(
                    x["node_id"] == a.node_id and x["state"] == "running"
                    for x in j["attempts"]
                ),
            )
            b = await _agent(grpc_target, tmp_path / "agent-b")
            a_cert = a.client.identity.path / "node.crt"
            a_serial = format(pki.cert_from_pem(a_cert.read_bytes()).serial_number, "x")

            revoked = await http.post(f"/v1/nodes/{a.node_id}/revoke")
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["session_killed"] is True

            # A stops for good, saying how to come back.
            await asyncio.wait({a.task}, timeout=10)
            assert a.task.done()
            assert a.client.state is AgentState.STOPPED
            error = a.task.exception()
            assert error is not None and "node_revoked" in str(error)
            assert "identity reset" in str(error)
            # V0.8 G5 (D195): a typed, expected stop (run_agent logs it
            # `agent.revoked` with no traceback).
            assert isinstance(error, NodeRevokedError), type(error)

            # The node-loss path: LOST on A, retried and done on B.
            job = await wait_job(http, job_id, timeout_s=60)
            assert job["state"] == "succeeded", job
            lost = [x for x in job["attempts"] if x["node_id"] == a.node_id]
            assert [(x["state"], x["detail"]) for x in lost] == [
                ("lost", "node_revoked")
            ]
            assert job["attempts"][-1]["node_id"] == b.node_id

        # The peer plane: B knows the serial; its peer server refuses A.
        assert a_serial in b.client.revoked()
        assert b.peer is not None
        refused_before = b.peer.stats.refused.get("peer_cert_revoked", 0)
        a_ctx = client_context(
            fabric_ca().cert,
            cert_file=a_cert,
            key_file=a.client.identity.path / "node.key",
        )
        async with httpx.AsyncClient(verify=a_ctx, timeout=5) as client:
            with pytest.raises(httpx.HTTPError):
                await client.get(
                    f"https://127.0.0.1:{b.peer.port}/v1/blobs/{'0' * 64}",
                    extensions={"sni_hostname": peer_server_name(b.node_id)},
                )
        assert b.peer.stats.refused.get("peer_cert_revoked", 0) > refused_before
    finally:
        if b is not None:
            await b.stop()
        await a.stop()
