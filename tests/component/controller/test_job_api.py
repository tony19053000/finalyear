"""``/v1/jobs`` over real HTTP + real PostgreSQL + a real gRPC session (D50, D44).

The placed node is a hand-driven agent stream (``FakeAgent``) whose Hello
advertises the capabilities under test, so ONLINE/connected is real controller
state, not a stub.
"""

from __future__ import annotations

import asyncio
import os
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
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

BUNDLE = os.urandom(4096)
GIB = 1024**3


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    async with running_controller(g2_settings(pg_settings)) as rt:
        yield rt


@pytest_asyncio.fixture
async def channel(runtime: ControllerRuntime) -> AsyncIterator[grpc.aio.Channel]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as ch:
        yield ch


@pytest_asyncio.fixture
async def agents(channel: grpc.aio.Channel) -> AsyncIterator[Any]:
    made: list[FakeAgent] = []

    async def connect(*caps: str, gpus: int = 0) -> FakeAgent:
        agent = FakeAgent(channel)
        made.append(agent)
        hardware = agent_pb2.HardwareSummary(
            cpu_model="test",
            cpu_cores=4,
            cpu_threads=8,
            ram_bytes=16 * GIB,
            capabilities=list(caps),
            gpus=[
                agent_pb2.HardwareSummary.Gpu(index=i, model="g", vram_bytes=4 * GIB)
                for i in range(gpus)
            ],
        )
        await agent.connect(hardware=hardware)
        return agent

    yield connect
    for agent in made:
        await agent.close()


@pytest_asyncio.fixture
async def bundle(runtime: ControllerRuntime) -> str:
    digest = sha256_bytes(BUNDLE)
    async with api_client(runtime, token=token_of(runtime)) as client:
        assert (
            await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        ).status_code == 201
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


async def test_submit_records_submitted_planning_queued(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    agent = await agents("exec", "exec-docker")
    async with api_client(runtime, token=token_of(runtime)) as client:
        created = await client.post("/v1/jobs", json=_body(bundle, agent.node_id))
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["state"] == "queued" and body["created"] is True
        detail = (await client.get(f"/v1/jobs/{body['job_id']}")).json()
        assert [(t["from_state"], t["to_state"]) for t in detail["transitions"]] == [
            (None, "submitted"),
            ("submitted", "planning"),
            ("planning", "queued"),
        ]
        assert detail["placement"] == {"mode": "offload", "node_id": agent.node_id}
        assert detail["manifest"]["entrypoint"] == ["python", "main.py"]
        assert detail["attempts"] == []
        listed = (await client.get("/v1/jobs")).json()
        assert listed["count"] == 1 and listed["jobs"][0]["job_id"] == body["job_id"]
        local = await client.post(
            "/v1/jobs",
            json={
                **_body(bundle, agent.node_id),
                "placement": {"node_id": agent.node_id, "local": True},
            },
        )
        assert local.status_code == 201
        detail = (await client.get(f"/v1/jobs/{local.json()['job_id']}")).json()
        assert detail["placement"]["mode"] == "local"


async def test_idempotency_same_key_same_body_same_job(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    agent = await agents("exec")
    headers = {"Idempotency-Key": "submit-1"}
    async with api_client(runtime, token=token_of(runtime)) as client:
        first = await client.post(
            "/v1/jobs", json=_body(bundle, agent.node_id), headers=headers
        )
        # Same meaning, different spelling: the canonical form decides.
        again = await client.post(
            "/v1/jobs",
            json=_body(bundle, agent.node_id, entrypoint=["python", "main.py"]),
            headers=headers,
        )
        assert first.status_code == 201 and again.status_code == 200
        assert again.json()["job_id"] == first.json()["job_id"]
        assert again.json()["created"] is False
        other = await client.post(
            "/v1/jobs",
            json=_body(bundle, agent.node_id, entrypoint="python other.py"),
            headers=headers,
        )
        assert other.status_code == 409
        assert other.json()["error"]["code"] == "conflict"
    assert await _job_count(runtime) == 1


async def test_concurrent_duplicate_posts_make_one_row(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    agent = await agents("exec")
    async with api_client(runtime, token=token_of(runtime)) as client:
        responses = await asyncio.gather(
            *[
                client.post(
                    "/v1/jobs",
                    json=_body(bundle, agent.node_id),
                    headers={"Idempotency-Key": "race-key"},
                )
                for _ in range(8)
            ]
        )
    assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
    assert len({r.json()["job_id"] for r in responses}) == 1
    # Atomic submission: no loser can observe the winner mid-flight.
    assert {r.json()["state"] for r in responses} == {"queued"}
    assert await _job_count(runtime) == 1


async def test_submission_failing_after_the_insert_leaves_no_row(
    runtime: ControllerRuntime,
    agents: Any,
    bundle: str,
    pg_settings: DatabaseSettings,
) -> None:
    """A real database error on the last history row -> 503 and nothing stored;
    the same Idempotency-Key then succeeds (it was never burned)."""
    agent = await agents("exec")
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "CREATE FUNCTION mf_test_break() RETURNS trigger AS $$ BEGIN "
                    "IF NEW.to_state = 'queued' THEN RAISE EXCEPTION 'injected'; "
                    "END IF; RETURN NEW; END $$ LANGUAGE plpgsql"
                )
            )
            await conn.execute(
                text(
                    "CREATE TRIGGER mf_test_break BEFORE INSERT ON job_transition "
                    "FOR EACH ROW EXECUTE FUNCTION mf_test_break()"
                )
            )
        headers = {"Idempotency-Key": "broken-db"}
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
                await conn.execute(text("DROP TRIGGER mf_test_break ON job_transition"))
            retried = await client.post(
                "/v1/jobs", json=_body(bundle, agent.node_id), headers=headers
            )
            assert retried.status_code == 201, retried.text
            assert retried.json()["state"] == "queued"
    finally:
        await engine.dispose()
    assert await _job_count(runtime) == 1


async def test_missing_bundle_is_409_with_the_digest_and_no_row(
    runtime: ControllerRuntime, agents: Any
) -> None:
    agent = await agents("exec")
    missing = str(sha256_bytes(b"never uploaded"))
    async with api_client(runtime, token=token_of(runtime)) as client:
        response = await client.post("/v1/jobs", json=_body(missing, agent.node_id))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert missing in response.json()["error"]["message"]
    assert await _job_count(runtime) == 0


async def test_placement_refusals_create_no_row(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    no_exec = await agents("exec-docker")
    cpu_only = await agents("exec", "exec-docker")
    gone = await agents("exec")
    await gone.close()
    cases = [
        (str(new_node_id()), {}, 404, "not_found"),
        (no_exec.node_id, {}, 409, "no_feasible_plan"),
        (cpu_only.node_id, {"resources": {"gpu": "required"}}, 409, "no_feasible_plan"),
        (cpu_only.node_id, {"resources": {"ram_gb": 64}}, 409, "no_feasible_plan"),
    ]
    async with api_client(runtime, token=token_of(runtime)) as client:
        for node_id, manifest, status, code in cases:
            response = await client.post(
                "/v1/jobs", json=_body(bundle, node_id, **manifest)
            )
            assert response.status_code == status, (node_id, response.text)
            assert response.json()["error"]["code"] == code
        for _ in range(100):  # the clean close lands OFFLINE
            response = await client.post("/v1/jobs", json=_body(bundle, gone.node_id))
            if response.status_code == 409:
                break
            await asyncio.sleep(0.05)
        assert response.json()["error"]["code"] == "no_feasible_plan"
    assert await _job_count(runtime) == 0


async def test_gpu_job_on_a_gpu_node(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    gpu = await agents("exec", "exec-docker", "exec-gpu", gpus=1)
    async with api_client(runtime, token=token_of(runtime)) as client:
        ok = await client.post(
            "/v1/jobs",
            json=_body(
                bundle, gpu.node_id, resources={"gpu": "required", "vram_gb": 4}
            ),
        )
        assert ok.status_code == 201, ok.text
        big = await client.post(
            "/v1/jobs",
            json=_body(
                bundle, gpu.node_id, resources={"gpu": "required", "vram_gb": 5}
            ),
        )
        assert big.status_code == 409


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda b: {**b, "extra": 1}, "extra"),
        (lambda b: {**b, "bundle_digest": "sha256:XYZ"}, "bundle_digest"),
        (lambda b: {**b, "manifest": {**b["manifest"], "split": {}}}, "V0.10"),
        (
            lambda b: {**b, "manifest": {**b["manifest"], "resources": {"gpus": 1}}},
            "resources.gpus",
        ),
        (lambda b: {**b, "placement": {"node_id": "../x"}}, "node_<ULID>"),
    ],
)
async def test_invalid_requests_are_400_and_create_nothing(
    runtime: ControllerRuntime, agents: Any, bundle: str, mutate: Any, fragment: str
) -> None:
    agent = await agents("exec")
    async with api_client(runtime, token=token_of(runtime)) as client:
        response = await client.post(
            "/v1/jobs", json=mutate(_body(bundle, agent.node_id))
        )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_input"
    assert fragment in response.json()["error"]["message"]
    assert await _job_count(runtime) == 0


async def test_body_and_key_bounds(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    agent = await agents("exec")
    limit = runtime.settings.controller.jobs.max_request_bytes
    async with api_client(runtime, token=token_of(runtime)) as client:
        big = await client.post(
            "/v1/jobs",
            content=b"{" + b" " * limit + b"}",
            headers={"content-type": "application/json"},
        )
        assert big.status_code == 413
        garbage = await client.post("/v1/jobs", content=b"[" * 100_000)
        assert garbage.status_code == 400
        for key, ok in [
            ("k" * 128, True),
            ("k" * 129, False),
            ("has space", False),
            ("tab\tkey", False),
            ("é", False),
        ]:
            response = await client.post(
                "/v1/jobs",
                json=_body(bundle, agent.node_id),
                headers={"Idempotency-Key": key.encode("utf-8")},
            )
            assert (response.status_code == 201) is ok, (key, response.text)
            if not ok:
                assert response.json()["error"]["code"] == "invalid_input"
    assert await _job_count(runtime) == 1


async def test_list_and_get_bounds(runtime: ControllerRuntime) -> None:
    async with api_client(runtime, token=token_of(runtime)) as client:
        assert (await client.get("/v1/jobs", params={"limit": 500})).status_code == 200
        assert (await client.get("/v1/jobs", params={"limit": 501})).status_code == 400
        assert (await client.get("/v1/jobs", params={"limit": 0})).status_code == 400
        assert (
            await client.get("/v1/jobs", params={"state": "bogus"})
        ).status_code == 400
        assert (await client.get("/v1/jobs/not-a-job")).status_code == 400
        assert (await client.get("/v1/jobs/" + "x" * 41)).status_code == 400
        missing = await client.get("/v1/jobs/job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3")
        assert missing.status_code == 404


async def test_every_protected_route_needs_the_token_and_only_healthz_is_open(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    agent = await agents("exec")
    digest = bundle.removeprefix("sha256:")
    protected = [
        ("POST", "/v1/jobs", _body(bundle, agent.node_id)),
        ("GET", "/v1/jobs", None),
        ("GET", "/v1/jobs/job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3", None),
        ("PUT", f"/v1/blobs/{digest}", None),
        ("HEAD", f"/v1/blobs/{digest}", None),
        ("GET", f"/v1/blobs/{digest}", None),
    ]
    for token in (None, "wrong-token-" + "x" * 40):
        async with api_client(runtime, token=token) as client:
            for method, url, body in protected:
                response = await client.request(method, url, json=body)
                assert response.status_code == 401, (method, url, token)
                if method != "HEAD":
                    assert response.json()["error"]["code"] == "unauthorized"
    async with api_client(runtime) as anonymous:
        # V0.7 G4 (D148): the D14 node/session reads need a user too; only the
        # health probe and the (data-free) OpenAPI schema stay open.
        for url in ("/healthz", "/v1/openapi.json"):
            assert (await anonymous.get(url)).status_code == 200, url
        for url in ("/v1/nodes", f"/v1/nodes/{agent.node_id}", "/v1/sessions"):
            assert (await anonymous.get(url)).status_code == 401, url
    assert await _job_count(runtime) == 0


async def test_fetch_token_never_reads_jobs(
    runtime: ControllerRuntime, bundle: str
) -> None:
    assert runtime.fetch_tokens is not None
    token = runtime.fetch_tokens.mint("att_x", [sha256_bytes(BUNDLE)], ttl_s=60)
    async with api_client(runtime, token=token.get_secret_value()) as client:
        assert (await client.get("/v1/jobs")).status_code == 401
        assert (await client.post("/v1/jobs", json={})).status_code == 401
