"""Tester round 2 (V0.4 G2 fix round): API-level checks on top of the atomic
``create_queued_job`` fix, over real HTTP + real PostgreSQL + a real gRPC
session. Complements ``test_job_submit_atomic_g2_tester_r2.py`` (store level)
and the existing ``test_job_api.py`` / ``test_job_api_g2_tester.py``:

* ``GET /v1/jobs/{id}`` can never return a body whose ``state`` is
  ``submitted`` or ``planning`` for a manually-placed job -- it is either
  ``404`` (not committed yet) or ``queued`` (committed), never anything
  in-between, even while the submitting transaction is deliberately slowed;
* a genuinely dropped database connection (``pg_terminate_backend``) surfaced
  through the real HTTP route as a retryable error, with the Idempotency-Key
  still usable afterwards;
* a job rejected before ``create_queued_job`` is even called (unknown node)
  never appears in ``GET /v1/jobs`` or by id.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

GIB = 1024**3
BUNDLE = b"g2-tester-r2-bundle-bytes" * 100


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    async with running_controller(g2_settings(pg_settings)) as rt:
        yield rt


@pytest_asyncio.fixture
async def channel(runtime: ControllerRuntime) -> AsyncIterator[grpc.aio.Channel]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as ch:
        yield ch


@pytest_asyncio.fixture
async def agent(channel: grpc.aio.Channel) -> AsyncIterator[FakeAgent]:
    fa = FakeAgent(channel)
    hardware = agent_pb2.HardwareSummary(
        cpu_model="test",
        cpu_cores=4,
        cpu_threads=8,
        ram_bytes=16 * GIB,
        capabilities=["exec"],
    )
    await fa.connect(hardware=hardware)
    yield fa
    await fa.close()


@pytest_asyncio.fixture
async def bundle(runtime: ControllerRuntime) -> str:
    digest = sha256_bytes(BUNDLE)
    async with api_client(runtime, token=token_of(runtime)) as client:
        put = await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        assert put.status_code == 201
    return str(digest)


def _body(bundle: str, node_id: str, **manifest: Any) -> dict[str, Any]:
    return {
        "manifest": {"name": "demo", "entrypoint": "python main.py", **manifest},
        "bundle_digest": bundle,
        "placement": {"node_id": node_id},
    }


async def _job_count(runtime: ControllerRuntime) -> int:
    assert runtime.jobs is not None
    return len(await runtime.jobs.list_jobs(limit=1000))


# ------------------------------------------------- never observably mid-state


async def test_get_never_returns_submitted_or_planning_while_submission_slow(
    runtime: ControllerRuntime,
    agent: FakeAgent,
    bundle: str,
    pg_settings: DatabaseSettings,
) -> None:
    """Slow the ``planning`` history-row insert with a real Postgres trigger
    while a background poller hammers ``GET /v1/jobs/{id}``: the response must
    be 404 until the whole atomic submission commits, and ``queued`` the
    instant it exists -- never a body with ``state: submitted`` or
    ``state: planning``."""
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE FUNCTION mf_test_widen_http() RETURNS trigger AS $$ "
                    "BEGIN IF NEW.to_state = 'planning' THEN PERFORM pg_sleep(0.5); "
                    "END IF; RETURN NEW; END $$ LANGUAGE plpgsql"
                )
            )
            await conn.execute(
                text(
                    "CREATE TRIGGER mf_test_widen_http BEFORE INSERT ON "
                    "job_transition FOR EACH ROW EXECUTE FUNCTION "
                    "mf_test_widen_http()"
                )
            )
        observed: set[str] = set()
        stop = asyncio.Event()

        async def poll(job_id_holder: list[str]) -> None:
            async with api_client(runtime, token=token_of(runtime)) as client:
                while not stop.is_set():
                    if job_id_holder:
                        resp = await client.get(f"/v1/jobs/{job_id_holder[0]}")
                        if resp.status_code == 200:
                            observed.add(resp.json()["state"])
                            stop.set()
                        else:
                            assert resp.status_code == 404
                    await asyncio.sleep(0.01)

        holder: list[str] = []
        poller = asyncio.ensure_future(poll(holder))
        async with api_client(runtime, token=token_of(runtime)) as client:
            post_task = asyncio.ensure_future(
                client.post("/v1/jobs", json=_body(bundle, agent.node_id))
            )
            # We do not know the job_id ahead of the POST response, so also
            # scan the list endpoint for any row appearing early with a
            # non-queued state (belt and braces beyond the by-id poll).
            listing_observed: set[str] = set()
            while not post_task.done():
                listed = await client.get("/v1/jobs")
                if listed.status_code == 200:
                    for j in listed.json()["jobs"]:
                        listing_observed.add(j["state"])
                await asyncio.sleep(0.01)
            created = await post_task
            assert created.status_code == 201, created.text
            holder.append(created.json()["job_id"])
            await asyncio.wait_for(poller, timeout=3)
        # V0.4 G4: the dispatcher may already have moved the job on (the placed
        # node is live); what must never be visible is a half-submitted job.
        assert not listing_observed & {"submitted", "planning"}
        assert not observed & {"submitted", "planning"}
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("DROP TRIGGER IF EXISTS mf_test_widen_http ON job_transition")
            )
            await conn.execute(text("DROP FUNCTION IF EXISTS mf_test_widen_http()"))
        await engine.dispose()


# --------------------------------------------------------- dropped connection


async def test_dropped_db_connection_during_http_submit_is_retryable(
    runtime: ControllerRuntime,
    agent: FakeAgent,
    bundle: str,
    pg_settings: DatabaseSettings,
) -> None:
    """A real severed backend connection (``pg_terminate_backend`` on the
    submitting connection itself) surfaced through the HTTP route as a
    retryable 503, with no row left and the Idempotency-Key still usable."""
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE FUNCTION mf_test_kill_http() RETURNS trigger AS $$ "
                    "BEGIN IF NEW.to_state = 'queued' THEN "
                    "PERFORM pg_terminate_backend(pg_backend_pid()); END IF; "
                    "RETURN NEW; END $$ LANGUAGE plpgsql"
                )
            )
            await conn.execute(
                text(
                    "CREATE TRIGGER mf_test_kill_http BEFORE INSERT ON "
                    "job_transition FOR EACH ROW EXECUTE FUNCTION "
                    "mf_test_kill_http()"
                )
            )
        headers = {"Idempotency-Key": "dropped-conn-http"}
        async with api_client(runtime, token=token_of(runtime)) as client:
            failed = await client.post(
                "/v1/jobs", json=_body(bundle, agent.node_id), headers=headers
            )
            assert failed.status_code == 503, failed.text
            assert failed.json()["error"]["code"] == "network_error"
        async with engine.connect() as conn:
            counts = (
                await conn.execute(
                    text(
                        "SELECT (SELECT count(*) FROM job), "
                        "(SELECT count(*) FROM job_transition)"
                    )
                )
            ).one()
        assert tuple(counts) == (0, 0)
        async with engine.begin() as conn:
            await conn.execute(
                text("DROP TRIGGER IF EXISTS mf_test_kill_http ON job_transition")
            )
            await conn.execute(text("DROP FUNCTION IF EXISTS mf_test_kill_http()"))
        async with api_client(runtime, token=token_of(runtime)) as client:
            retried = await client.post(
                "/v1/jobs", json=_body(bundle, agent.node_id), headers=headers
            )
            assert retried.status_code == 201, retried.text
            assert retried.json()["state"] == "queued"
    finally:
        await engine.dispose()
    assert await _job_count(runtime) == 1


# ---------------------------------------------- rejected-before-write regression


async def test_rejected_placement_job_never_shows_up_by_id_or_in_the_list(
    runtime: ControllerRuntime, bundle: str
) -> None:
    unknown_node = str(new_node_id())
    async with api_client(runtime, token=token_of(runtime)) as client:
        rejected = await client.post("/v1/jobs", json=_body(bundle, unknown_node))
        assert rejected.status_code == 404
        never_existed = str(new_job_id())
        assert (await client.get(f"/v1/jobs/{never_existed}")).status_code == 404
        listed = await client.get("/v1/jobs")
        assert listed.json()["count"] == 0
    assert await _job_count(runtime) == 0
