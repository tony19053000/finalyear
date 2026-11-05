"""V1.0 G9-B item 4: Mode 3 GPU lanes on laptops without Docker.

A real controller (PostgreSQL, gRPC sessions, HTTPS blob API, the real
planner + dispatcher) with fake agents speaking the lane protocol
(``mf_fake_lanes``). The fabric is what the lab has: a **venv-only** GPU laptop
(Windows without Docker: ``exec-venv`` + ``exec-gpu``), a Docker GPU laptop,
and a laptop with **no GPU**. GPUs are declared in the fake Hello - this
machine's own GPU never decides anything.

A split job with ``resources.gpu: required`` must:

* run lanes on the two GPU laptops only - never on the no-GPU one (the
  decision names it ``no_gpu``) - and the venv-only laptop is eligible;
* give every lane a GPU the node declared (``ExecutionSpec.gpu_indices``),
  one GPU slot each;
* share the chunks by speed (the faster GPU node pulls more chunks);
* succeed with every chunk committed once.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_fake_lanes import FakeLane
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
]

GIB = 1024**3
HB_S = 0.2
VENV_GPU = ("exec", "exec-venv", "exec-gpu", "exec-split")
DOCKER_GPU = ("exec", "exec-docker", "exec-gpu", "exec-split")
NO_GPU = ("exec", "exec-venv", "exec-subprocess", "exec-split")


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        split={"visibility_timeout_s": 5.0, "wait_retry_s": 0.2},
        liveness=LivenessSettings(
            heartbeat_interval_s=HB_S, miss_allowance_s=0.1, lease_multiplier=3
        ),
    )
    async with running_controller(settings) as rt:
        yield rt


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        return list((await conn.execute(text(query), params)).all())


@contextlib.asynccontextmanager
async def _fabric(
    runtime: ControllerRuntime,
) -> AsyncIterator[dict[str, FakeLane]]:
    nodes = {
        "venv_gpu": (VENV_GPU, 1, 0.005),
        "docker_gpu": (DOCKER_GPU, 1, 0.05),
        "no_gpu": (NO_GPU, 0, 0.001),  # fastest of all: still must not get a lane
    }
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: dict[str, FakeLane] = {}
        try:
            for name, (caps, gpus, speed) in nodes.items():
                fake = FakeAgent(channel)
                await fake.connect(
                    hardware=agent_pb2.HardwareSummary(
                        cpu_model="test",
                        cpu_cores=4,
                        cpu_threads=8,
                        ram_bytes=16 * GIB,
                        capabilities=list(caps),
                        gpus=[
                            agent_pb2.HardwareSummary.Gpu(
                                index=i, model="RTX 3050", vram_bytes=4 * GIB
                            )
                            for i in range(gpus)
                        ],
                    ),
                    max_cpu_jobs=2,
                    max_gpu_jobs=gpus,
                )
                fake.start_heartbeats(HB_S / 2)
                lane = FakeLane(fake, runtime, speed_s=speed)
                lane.start()
                lanes[name] = lane
            yield lanes
        finally:
            for lane in lanes.values():
                await lane.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(lane.agent.close(), 10)


async def test_gpu_lanes_go_to_gpu_nodes_only_incl_a_venv_only_one(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "main.py").write_bytes(b"print(1)\n")
    built = build_tree(root)
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    async with (
        _fabric(runtime) as lanes,
        api_client(runtime, token=token_of(runtime)) as http,
    ):
        got = await http.post(
            "/v1/jobs",
            json={
                "manifest": {
                    "name": "gpu-split",
                    "entrypoint": "python main.py {items_file} {out}",
                    "env": {"base": "python-cpu"},
                    "resources": {"gpu": "required"},
                    "split": {
                        "over": {"range": {"stop": 300}},
                        "chunk": {"items": 10},
                        "speculative": False,
                        "min_nodes": 2,
                    },
                },
                "tree_id": str(built.tree.tree_id),
                "placement": {},
            },
        )
        assert got.status_code == 201, got.text
        job_id = got.json()["job_id"]
        assert runtime.jobs is not None
        job = None
        for _ in range(2400):
            job = await runtime.jobs.get_job(job_id)
            assert job is not None
            if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
                break
            await asyncio.sleep(0.05)
        assert job is not None and job.state is JobState.SUCCEEDED, job and (
            job.failure_type,
            job.failure_detail,
        )

        def lane_starts(name: str) -> list[agent_pb2.StartAttempt]:
            return [
                m.start_attempt
                for m in lanes[name].agent.messages("start_attempt")
                if m.start_attempt.HasField("lane")
            ]

        assert lane_starts("no_gpu") == []
        assert lanes["no_gpu"].ledger == []
        for name in ("venv_gpu", "docker_gpu"):
            starts = lane_starts(name)
            assert len(starts) == 1, (name, starts)
            assert list(starts[0].spec.gpu_indices) == [0], name
        ran = {name: len(lane.ledger) for name, lane in lanes.items()}
        assert ran["venv_gpu"] + ran["docker_gpu"] == 30
        assert ran["venv_gpu"] > ran["docker_gpu"], ran  # pull = by speed

        rows = await _sql(
            runtime,
            "SELECT node_id, slot FROM attempt WHERE job_id = :job AND role = 'lane'",
            job=job_id,
        )
        by_node = {lanes[n].agent.node_id: n for n in lanes}
        assert sorted(by_node[r.node_id] for r in rows) == ["docker_gpu", "venv_gpu"]
        assert {r.slot for r in rows} == {"gpu"}

        decisions = (await http.get(f"/v1/jobs/{job_id}/decisions")).json()
        no_gpu_id = lanes["no_gpu"].agent.node_id
        codes = {
            c.get("reject_code")
            for d in decisions["decisions"]
            for c in d["candidates"]
            if c.get("node_id") == no_gpu_id
        }
        assert codes == {"no_gpu"}, decisions
