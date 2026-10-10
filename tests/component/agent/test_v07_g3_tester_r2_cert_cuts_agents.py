"""V0.7 G3 tester round 2 (D155 T-1, T-3) with real agents: when a busy node's
session is cut because its certificate was superseded or reached ``not_after``,
its attempt takes the node-loss path (``LOST``) at once and the job finishes on
the other node; the cut agent stops with the remedy, it does not retry-loop.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import pytest
from mf_exec_harness import ExecAgent, wait_job
from test_v07_g3_revocation_agents import (  # noqa: F401  (fixtures)
    _agent,
    _http,
    _until_job,
    project,
    settings,
)

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_fabric import fabric_ca
from mf_jobs_harness import token_of
from pydantic import SecretStr

from meshforge.agent.enrollment import store
from meshforge.agent.identity import load_or_create
from meshforge.agent.session.state import AgentState
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common import pki
from meshforge.common.tree import build_tree
from meshforge.controller.revocation import supersede
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]


async def _submit(http, project_id: str, name: str) -> str:  # type: ignore[no-untyped-def]
    posted = await http.post(
        "/v1/jobs",
        json={
            "manifest": {"name": name, "entrypoint": ["python", "main.py", "o.txt"]},
            "tree_id": project_id,
        },
    )
    assert posted.status_code == 201, posted.text
    return str(posted.json()["job_id"])


async def test_superseding_a_busy_nodes_certificate_moves_its_job(
    runtime: ControllerRuntime,
    grpc_target: str,
    project: str,  # noqa: F811
    tmp_path: Path,
    pg_settings: DatabaseSettings,
) -> None:
    a = await _agent(grpc_target, tmp_path / "agent-a")
    b: ExecAgent | None = None
    try:
        async with _http(runtime) as http:
            job_id = await _submit(http, project, "supersede-me")
            await _until_job(
                http,
                job_id,
                lambda j: any(
                    x["node_id"] == a.node_id and x["state"] == "running"
                    for x in j["attempts"]
                ),
            )
            b = await _agent(grpc_target, tmp_path / "agent-b")
            cert = pki.cert_from_pem((a.client.identity.path / "node.crt").read_bytes())
            engine = create_engine(pg_settings)
            try:
                async with create_session_factory(engine)() as db, db.begin():
                    await supersede(
                        db,
                        node_id=a.node_id,
                        serial=format(cert.serial_number, "x"),
                        not_after=cert.not_valid_after_utc,
                    )
            finally:
                await engine.dispose()
            await asyncio.wait({a.task}, timeout=15)
            assert a.task.done(), "the cut agent kept running"
            assert a.client.state is AgentState.STOPPED
            error = a.task.exception()
            assert error is not None and "cert_" in str(error), error
            assert "join" in str(error)
            job = await wait_job(http, job_id, timeout_s=90)
            assert job["state"] == "succeeded", job
            lost = [x for x in job["attempts"] if x["node_id"] == a.node_id]
            assert [x["state"] for x in lost] == ["lost"], job["attempts"]
            assert job["attempts"][-1]["node_id"] == b.node_id
    finally:
        if b is not None:
            await b.stop()
        await a.stop()


async def test_a_busy_nodes_certificate_reaching_not_after_moves_its_job(
    runtime: ControllerRuntime, grpc_target: str, tmp_path: Path
) -> None:
    """A's certificate has ~10 s left when it starts; the job sleeps 20 s, so
    it is still running at not_after and must be moved to B."""
    root = tmp_path / "long-proj"
    root.mkdir()
    (root / "main.py").write_bytes(
        b"import sys, time\ntime.sleep(20)\nopen(sys.argv[1],'w').write('x')\n"
    )
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
    project_id = str(built.tree.tree_id)
    data_dir = tmp_path / "agent-a"
    identity = load_or_create(data_dir / "identity")
    ca = fabric_ca()
    csr = pki.build_csr(
        identity.private_key, fabric_id=ca.fabric_id, node_id=identity.node_id
    )
    cert = pki.issue_node_cert(
        ca,
        csr,
        node_id=identity.node_id,
        fabric_id=ca.fabric_id,
        now=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=48),
        lifetime=dt.timedelta(minutes=1),
    )
    store(identity, cert, ca.cert, force=True)
    a = await _agent(grpc_target, data_dir)
    b: ExecAgent | None = None
    try:
        async with _http(runtime) as http:
            job_id = await _submit(http, project_id, "expire-me")
            await _until_job(
                http,
                job_id,
                lambda j: any(x["node_id"] == a.node_id for x in j["attempts"]),
            )
            b = await _agent(grpc_target, tmp_path / "agent-b")
            await asyncio.wait({a.task}, timeout=30)
            assert a.task.done(), "the expired agent kept running"
            assert a.client.state is AgentState.STOPPED
            job = await wait_job(http, job_id, timeout_s=90)
            assert job["state"] == "succeeded", job
            assert job["attempts"][-1]["node_id"] == b.node_id, job["attempts"]
            first = [x for x in job["attempts"] if x["node_id"] == a.node_id]
            assert first and first[0]["state"] in ("lost", "succeeded"), first
    finally:
        if b is not None:
            await b.stop()
        await a.stop()
