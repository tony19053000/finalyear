"""V0.6 G2: ``POST /v1/jobs`` with automatic placement (D103, D104, D106, D107)
over real HTTP + PostgreSQL + real gRPC sessions (hand-driven ``FakeAgent``s).

* no ``placement.node_id`` = auto; the submit-time pass checks only the static
  rules over every enrolled node (an offline but capable node is fine);
* nobody could ever run it -> ``409 no_feasible_plan`` with every node's
  ``{node_id, reject_code, reject_text}`` and **no row**;
* the scheduling fields land on the job (priority, policy, retry, submitter,
  seeded, project bytes, fingerprint); hostile values are refused (bounds);
* idempotency still means "same request": a different policy is a conflict.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

BUNDLE = os.urandom(5000)
GIB = 1024**3


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    # No dispatch here: these tests are about the submit-time pass only.
    async with running_controller(
        g2_settings(pg_settings, jobs={"dispatch_interval_s": 60.0})
    ) as rt:
        yield rt


@pytest_asyncio.fixture
async def channel(runtime: ControllerRuntime) -> AsyncIterator[grpc.aio.Channel]:
    async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch:
        yield ch


@pytest_asyncio.fixture
async def agents(channel: grpc.aio.Channel) -> AsyncIterator[Any]:
    made: list[FakeAgent] = []

    async def connect(*caps: str, gpus: int = 0, **hello: Any) -> FakeAgent:
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
        await agent.connect(hardware=hardware, **hello)
        return agent

    yield connect
    for agent in made:
        await agent.close()


@pytest_asyncio.fixture
async def bundle(runtime: ControllerRuntime) -> str:
    digest = sha256_bytes(BUNDLE)
    async with api_client(runtime, token=token_of(runtime)) as client:
        put = await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        assert put.status_code == 201
    return str(digest)


def _auto(bundle: str, **extra: Any) -> dict[str, Any]:
    manifest = {"name": "demo", "entrypoint": "python main.py"}
    manifest.update(extra.pop("manifest", {}))
    return {"manifest": manifest, "bundle_digest": bundle, **extra}


async def _job_count(runtime: ControllerRuntime) -> int:
    assert runtime.jobs is not None
    return len(await runtime.jobs.list_jobs(limit=1000))


async def test_no_node_at_all_is_409_with_no_row(
    runtime: ControllerRuntime, bundle: str
) -> None:
    async with api_client(runtime, token=token_of(runtime)) as client:
        refused = await client.post("/v1/jobs", json=_auto(bundle))
    assert refused.status_code == 409, refused.text
    error = refused.json()["error"]
    assert error["code"] == "no_feasible_plan"
    assert error["rejections"] == []
    assert "no node" in error["message"]
    assert await _job_count(runtime) == 0


async def test_static_infeasible_lists_every_node_and_writes_nothing(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    no_exec = await agents("docker")
    cpu_only = await agents("exec", "exec-subprocess")
    small_gpu = await agents("exec", "exec-docker", "exec-gpu", gpus=1)
    body = _auto(bundle, manifest={"resources": {"gpu": "required", "vram_gb": 8}})
    async with api_client(runtime, token=token_of(runtime)) as client:
        for attempt in range(2):  # an Idempotency-Key retry is refused the same way
            refused = await client.post(
                "/v1/jobs", json=body, headers={"idempotency-key": "k-409"}
            )
            assert refused.status_code == 409, (attempt, refused.text)
    error = refused.json()["error"]
    assert error["code"] == "no_feasible_plan"
    by_node = {r["node_id"]: r for r in error["rejections"]}
    assert by_node[no_exec.node_id]["reject_code"] == "not_exec"
    assert by_node[cpu_only.node_id]["reject_code"] == "no_gpu"
    assert by_node[small_gpu.node_id]["reject_code"] == "vram_short"
    assert all(r["reject_text"] for r in error["rejections"])
    assert len(error["rejections"]) == 3
    assert await _job_count(runtime) == 0


async def test_owner_slot_settings_are_static_rules(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    gpu_only = await agents(
        "exec", "exec-docker", "exec-gpu", gpus=1, max_cpu_jobs=0, max_gpu_jobs=1
    )
    async with api_client(runtime, token=token_of(runtime)) as client:
        cpu_job = await client.post("/v1/jobs", json=_auto(bundle))
        assert cpu_job.status_code == 409
        (rejection,) = cpu_job.json()["error"]["rejections"]
        assert rejection == {
            "node_id": gpu_only.node_id,
            "reject_code": "no_cpu_slots",
            "reject_text": rejection["reject_text"],
        }
        gpu_job = await client.post(
            "/v1/jobs", json=_auto(bundle, manifest={"resources": {"gpu": "required"}})
        )
        assert gpu_job.status_code == 201, gpu_job.text


async def test_an_offline_capable_node_is_enough_and_fields_are_stored(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    capable = await agents("exec", "exec-subprocess")
    await capable.close()  # gone: liveness is ignored at submit (D104)
    submitter = str(new_node_id())
    body = _auto(
        bundle,
        manifest={
            "priority": -7,
            "policy": "least_loaded",
            "retry": {"max_attempts": 5, "backoff_s": 0, "on_lost": False},
        },
        placement={"policy": "random"},
        submitter_node_id=submitter,
        seeded=True,
    )
    async with api_client(runtime, token=token_of(runtime)) as client:
        created = await client.post(
            "/v1/jobs", json=body, headers={"idempotency-key": "k-auto"}
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["job_id"]
        detail = (await client.get(f"/v1/jobs/{job_id}")).json()
        assert detail["placement"] == {
            "mode": "auto",
            "node_id": None,
            "policy": "random",
        }
        assert detail["priority"] == -7
        assert detail["submitter_node_id"] == submitter
        assert detail["state"] == "queued"
        replay = await client.post(
            "/v1/jobs", json=body, headers={"idempotency-key": "k-auto"}
        )
        assert replay.status_code == 200 and replay.json()["job_id"] == job_id
        other = await client.post(
            "/v1/jobs",
            json={**body, "placement": {"policy": "cost_model"}},
            headers={"idempotency-key": "k-auto"},
        )
        assert other.status_code == 409
        assert other.json()["error"]["code"] == "conflict"
    assert runtime.jobs is not None
    job = await runtime.jobs.get_job(job_id)
    assert job is not None
    plan = job.scheduling
    assert plan.policy == "random"  # the request overrides the manifest
    assert (plan.max_attempts, plan.retry_backoff_s, plan.on_lost_retry) == (
        5,
        0,
        False,
    )
    assert plan.submitter_node_id == submitter and plan.submitter_seeded
    assert plan.input_bytes == len(BUNDLE)
    assert plan.fingerprint is not None and len(plan.fingerprint) == 64
    assert plan.queued_at is not None
    assert job.placement == {"mode": "auto", "policy": "random"}


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        ({"submitter_node_id": "node_nope"}, "submitter_node_id"),
        ({"submitter_node_id": "x" * 41}, "submitter_node_id"),
        ({"seeded": True}, "seeded"),
        ({"placement": {"local": True}}, "local"),
        ({"placement": {"policy": "fastest"}}, "policy"),
        ({"placement": {"node_id": ""}}, "node_id"),
        ({"manifest": {"priority": 11}}, "priority"),
        ({"manifest": {"retry": {"max_attempts": 11}}}, "max_attempts"),
    ],
)
async def test_hostile_or_inconsistent_requests_are_400(
    runtime: ControllerRuntime,
    agents: Any,
    bundle: str,
    change: dict[str, Any],
    fragment: str,
) -> None:
    await agents("exec", "exec-subprocess")
    async with api_client(runtime, token=token_of(runtime)) as client:
        refused = await client.post("/v1/jobs", json=_auto(bundle, **change))
    assert refused.status_code == 400, refused.text
    assert fragment in refused.json()["error"]["message"]
    assert await _job_count(runtime) == 0


async def test_a_pinned_request_hashes_and_reads_as_in_v04(
    runtime: ControllerRuntime, agents: Any, bundle: str
) -> None:
    from meshforge.controller.api.job_models import Placement
    from meshforge.controller.api.job_routes import request_hash

    agent = await agents("exec", "exec-subprocess")
    pinned = Placement(mode="offload", node_id=agent.node_id)
    manifest = {"name": "m"}
    # the V0.4 formula: placement dumped as exactly {mode, node_id}
    import hashlib
    import json

    v04 = hashlib.sha256(
        json.dumps(
            {
                "manifest": manifest,
                "bundle_digest": bundle,
                "placement": {"mode": "offload", "node_id": agent.node_id},
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    assert request_hash(manifest, bundle, pinned) == v04
