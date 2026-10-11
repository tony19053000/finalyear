"""V0.7 G4 deliverable 1 (roadmap 0.7.7b, D140) with real agents: a real
controller (PostgreSQL, mTLS gRPC, HTTPS) and real agents with TLS peer servers
and the certificate renewer running as ``serve()`` runs it.

* **Near expiry, both rotations, a job running.** Agent A starts with a node
  certificate that is already past two thirds of its life (it would be cut at
  its ``not_after``). A job runs on A. Meanwhile the controller's server
  certificate is renewed under it (G3 hot reload) and A renews its node
  certificate (``RenewCertificate``), swaps the file and hands its session over
  to the new certificate. The attempt is never lost and the job succeeds on A;
  the old session ended by the hand-over, never ``cert_expired``; the node row
  holds the new certificate, the old serial is ``superseded`` and B - another
  agent - refuses it on its peer server.
* **rotate-key end to end.** A key staged on a running agent (as
  ``meshforge-agent rotate-key`` does) is certified, committed with the
  identity (same node_id, new fingerprint in ``node.json`` and on the
  controller) and handed over; a job afterwards runs (its blob fetches present
  the new certificate - the old one is superseded).
"""

from __future__ import annotations

import asyncio
import datetime as dt
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

from meshforge.agent import enrollment
from meshforge.agent.identity import load_or_create, stage_rotation_key
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common import pki
from meshforge.common.fabric_tls import client_context, peer_server_name
from meshforge.common.tree import build_tree
from meshforge.controller.enrollment.service import public_key_fingerprint
from meshforge.controller.fabric_ca import ensure_server_cert
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

MAIN = b"""\
import sys, time
time.sleep(float(sys.argv[2]))
open(sys.argv[1], "w").write("done")
"""
FAST_RENEW = {"renew_check_interval_s": 0.3, "renew_retry_base_s": 0.3}


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        renewal={"min_interval_s": 1.0},
        **tolerant_liveness(),
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


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    from sqlalchemy import text

    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _until(check: Any, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.05)


def _short_enrolment(data_dir: Path, due_in_s: float) -> pki.x509.Certificate:
    """Enrol the identity in ``data_dir`` with a certificate due for renewal in
    ``due_in_s`` (not_before is back-dated by the CA's 5 min clock skew)."""
    identity = load_or_create(data_dir / "identity")
    ca = fabric_ca()
    skew = pki.CLOCK_SKEW.total_seconds()
    # due = now - skew + 2/3 (lifetime + skew)  =>  lifetime = 1.5 (due + skew) - skew
    lifetime = dt.timedelta(seconds=1.5 * (due_in_s + skew) - skew)
    csr = pki.build_csr(
        identity.private_key, fabric_id=ca.fabric_id, node_id=identity.node_id
    )
    cert = pki.issue_node_cert(
        ca, csr, node_id=identity.node_id, fabric_id=ca.fabric_id, lifetime=lifetime
    )
    enrollment.store(identity, cert, ca.cert, force=True)
    return cert


async def _post_job(
    http: httpx.AsyncClient, project: str, node_id: str, sleep_s: float
) -> str:
    posted = await http.post(
        "/v1/jobs",
        json={
            "manifest": {
                "name": "renew-under-me",
                "entrypoint": ["python", "main.py", "out.txt", str(sleep_s)],
            },
            "tree_id": project,
            "placement": {"node_id": node_id},
        },
    )
    assert posted.status_code == 201, posted.text
    return str(posted.json()["job_id"])


def _serial_of(cert: Any) -> str:
    return format(cert.serial_number, "x")


async def test_a_busy_node_renews_and_hands_over_before_expiry_while_the_server_cert_renews(
    runtime: ControllerRuntime,
    settings: ControllerSettings,
    pg_settings: DatabaseSettings,
    grpc_target: str,
    project: str,
    tmp_path: Path,
) -> None:
    old_cert = _short_enrolment(tmp_path / "agent-a", due_in_s=6.0)
    old_serial = _serial_of(old_cert)
    old_pem = tmp_path / "old-node.crt"
    old_pem.write_bytes(pki.cert_to_pem(old_cert))
    b = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent-b",
        agent_overrides={"peer": {"bind": "127.0.0.1", "port": 0}},
    )
    a = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent-a",
        agent_overrides={"peer": {"bind": "127.0.0.1", "port": 0}, "cert": FAST_RENEW},
        renew=True,
    )
    try:
        assert a.client.stream_cert_serial == old_serial
        async with _http(runtime) as http:
            job_id = await _post_job(http, project, a.node_id, sleep_s=14)
            await wait_job(http, job_id, states=frozenset({"running"}), timeout_s=30)

            # The controller's server certificate renews under the running job.
            assert runtime.server_tls is not None and runtime.fabric is not None
            before = runtime.server_tls.current.cert.serial_number
            for name in ("server.crt", "server.key"):
                (settings.tls_dir / name).unlink()
            await asyncio.to_thread(
                ensure_server_cert,
                settings.tls_dir,
                runtime.fabric,
                hosts=settings.server_cert_hosts,
                lifetime=dt.timedelta(minutes=2),  # already past 2/3 of its life
            )
            assert await runtime.server_tls.check() is True
            assert runtime.server_tls.current.cert.serial_number != before

            # A renews and hands over (well before its old not_after).
            await _until(
                lambda: a.client.stream_cert_serial not in (None, old_serial),
                timeout_s=30,
                what="A never handed over to a renewed certificate",
            )
            new_serial = a.client.stream_cert_serial
            assert a.client.renewer is not None and a.client.renewer.renewals == 1
            assert a.client.handovers >= 1
            assert dt.datetime.now(dt.UTC) < old_cert.not_valid_after_utc

            job = await wait_job(http, job_id, timeout_s=60)
            assert job["state"] == "succeeded", job
            assert [(x["node_id"], x["state"]) for x in job["attempts"]] == [
                (a.node_id, "done")
            ], job["attempts"]

        rows = await _sql(
            pg_settings,
            "SELECT cert_serial, pending_cert_serial, state FROM node "
            "WHERE node_id = :n",
            n=a.node_id,
        )
        assert rows == [(new_serial, None, "online")]
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
        assert "cert_expired" not in ends and "cert_superseded" not in ends, ends

        # The peer plane: B learnt the old serial and refuses it.
        await _until(
            lambda: old_serial in b.client.revoked(),
            timeout_s=5,
            what="B never learnt the superseded serial",
        )
        assert b.peer is not None
        refused_before = b.peer.stats.refused.get("peer_cert_revoked", 0)
        old_ctx = client_context(
            fabric_ca().cert,
            cert_file=old_pem,
            key_file=a.client.identity.path / "node.key",
        )
        async with httpx.AsyncClient(verify=old_ctx, timeout=5) as client:
            with pytest.raises(httpx.HTTPError):
                await client.get(
                    f"https://127.0.0.1:{b.peer.port}/v1/blobs/{'0' * 64}",
                    extensions={"sni_hostname": peer_server_name(b.node_id)},
                )
        assert b.peer.stats.refused.get("peer_cert_revoked", 0) > refused_before
        # ...while A's reloaded HTTPS client (the new certificate) is served.
        assert a.http_tls is not None
        async with httpx.AsyncClient(verify=a.http_tls.context, timeout=5) as client:
            response = await client.get(
                f"https://127.0.0.1:{b.peer.port}/v1/blobs/{'0' * 64}",
                extensions={"sni_hostname": peer_server_name(b.node_id)},
            )
        assert response.status_code in (401, 403)  # served (no token), not cut
    finally:
        await a.stop()
        await b.stop()


async def test_rotate_key_on_a_running_agent_end_to_end(
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    grpc_target: str,
    project: str,
    tmp_path: Path,
) -> None:
    a = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent-a",
        agent_overrides={"cert": FAST_RENEW},
        renew=True,
    )
    try:
        identity = a.client.identity
        old_fp, old_serial = identity.fingerprint, a.client.stream_cert_serial
        node_id = a.node_id
        created, staged_fp = await asyncio.to_thread(stage_rotation_key, identity.path)
        assert created
        await _until(
            lambda: (
                a.client.identity.fingerprint == staged_fp
                and a.client.stream_cert_serial not in (None, old_serial)
            ),
            timeout_s=30,
            what="the staged key never went into use",
        )
        assert a.client.node_id == node_id
        assert a.client.renewer is not None and a.client.renewer.rotations == 1
        on_disk = load_or_create(identity.path)
        assert on_disk.node_id == node_id and on_disk.fingerprint == staged_fp
        assert not (identity.path / "node.key.next").exists()
        stored = enrollment.load(on_disk)
        assert stored is not None
        assert pki.public_keys_equal(
            on_disk.private_key.public_key(), stored.node_cert.public_key()
        )
        rows = await _sql(
            pg_settings,
            "SELECT public_key_fingerprint, cert_serial FROM node WHERE node_id = :n",
            n=node_id,
        )
        assert rows == [(staged_fp, a.client.stream_cert_serial)]
        assert staged_fp == public_key_fingerprint(on_disk.private_key.public_key())
        assert staged_fp != old_fp
        assert await _sql(
            pg_settings,
            "SELECT reason FROM revoked_cert WHERE serial = :s",
            s=old_serial,
        ) == [("superseded",)]
        # A job afterwards: staging fetches blobs over HTTPS with the node
        # certificate - the old one is superseded, so this needs the reload.
        async with _http(runtime) as http:
            job_id = await _post_job(http, project, node_id, sleep_s=0.1)
            job = await wait_job(http, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
    finally:
        await a.stop()
