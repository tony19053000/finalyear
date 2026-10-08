"""V0.7 G3 item 0 (D153 item 7): an ``enrolling`` placeholder row - written by an
enrolment of a never-seen node, before that node's first Hello - is never a
scheduling or probing candidate.

Real PostgreSQL, a real controller with a fabric CA, a real ``Enroll`` over TLS
(which inserts the row), and a hand-driven exec node on the session port.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from cryptography.hazmat.primitives import serialization
from mf_fabric import seed_controller_fabric, session_channel
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent, poll_until

from meshforge.common import pki
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, enroll_pb2, enroll_pb2_grpc
from meshforge.controller.enrollment.invites import InviteStore
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

GIB = 1024**3
BUNDLE = os.urandom(3000)


@pytest_asyncio.fixture
async def rt(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, jobs={"dispatch_interval_s": 60.0})
    seed_controller_fabric(settings)
    async with running_controller(settings) as runtime:
        yield runtime


def _enroll(rt: ControllerRuntime, token: bytes, node: str) -> None:
    assert rt.fabric is not None
    csr = pki.build_csr(
        pki.generate_key(), fabric_id=rt.fabric.fabric_id, node_id=node
    ).public_bytes(serialization.Encoding.PEM)
    creds = grpc.ssl_channel_credentials(pki.cert_to_pem(rt.fabric.cert))
    with grpc.secure_channel(
        f"127.0.0.1:{rt.enroll_port}",
        creds,
        options=[("grpc.ssl_target_name_override", pki.SERVER_NAME)],
    ) as channel:
        enroll_pb2_grpc.EnrollmentStub(channel).Enroll(
            enroll_pb2.EnrollRequest(join_token=token, csr_pem=csr), timeout=10
        )


async def _placeholder(rt: ControllerRuntime, pg: DatabaseSettings) -> str:
    engine = create_engine(pg)
    try:
        invite = await InviteStore(engine).create(ttl_s=300, actor="t")
    finally:
        await engine.dispose()
    node = str(new_node_id())
    await asyncio.to_thread(_enroll, rt, invite.token, node)
    return node


def _job(bundle: str) -> dict[str, Any]:
    return {
        "manifest": {"name": "demo", "entrypoint": "python main.py"},
        "bundle_digest": bundle,
    }


async def _bundle(rt: ControllerRuntime) -> str:
    digest = sha256_bytes(BUNDLE)
    async with api_client(rt, token=token_of(rt)) as client:
        assert (
            await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        ).status_code == 201
    return str(digest)


async def test_only_an_enrolling_row_means_no_feasible_plan(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    node = await _placeholder(rt, pg_settings)
    bundle = await _bundle(rt)
    async with api_client(rt, token=token_of(rt)) as client:
        refused = await client.post("/v1/jobs", json=_job(bundle))
    assert refused.status_code == 409, refused.text
    rejections = {r["node_id"]: r for r in refused.json()["error"]["rejections"]}
    assert node in rejections
    assert rejections[node]["reject_code"] in {"not_exec", "not_online"}
    assert rt.manager is not None
    assert node not in {live.node_id for live in rt.manager.snapshot()}


async def test_job_goes_to_the_online_node_never_the_enrolling_one(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    placeholder = await _placeholder(rt, pg_settings)
    bundle = await _bundle(rt)
    node_id = str(new_node_id())
    async with session_channel(rt, node_id) as channel:
        agent = FakeAgent(channel, node_id=node_id)
        try:
            await agent.connect(
                hardware=agent_pb2.HardwareSummary(
                    cpu_model="t",
                    cpu_cores=4,
                    cpu_threads=8,
                    ram_bytes=16 * GIB,
                    capabilities=["exec", "exec-subprocess"],
                )
            )
            async with api_client(rt, token=token_of(rt)) as client:
                created = await client.post("/v1/jobs", json=_job(bundle))
                assert created.status_code == 201, created.text
                job_id = created.json()["job_id"]
                assert rt.dispatcher is not None
                rt.dispatcher.wake()

                async def placed() -> bool:
                    return bool(agent.messages("start_attempt"))

                await poll_until(placed, timeout_s=10)
                decisions = await client.get(f"/v1/jobs/{job_id}/decisions")
            assert decisions.status_code == 200, decisions.text
            body = decisions.json()
            candidates = {
                c["node_id"]: c
                for d in body["decisions"]
                for c in d.get("candidates", [])
            }
            if placeholder in candidates:
                assert candidates[placeholder].get("reject_code"), candidates
            assert all(
                d.get("chosen_node_id") in (None, node_id) for d in body["decisions"]
            ), body
            starts = agent.messages("start_attempt")
            assert starts and starts[0].start_attempt.job_id == job_id
        finally:
            await agent.close()
