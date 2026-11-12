"""V1.0 G10 BUG-2 (D443) at the controller: a lane that ends ``FAILED
(env_error)`` because its node could not build the job's env.

A real controller (PostgreSQL, gRPC sessions, HTTPS blob API, the real planner
+ dispatcher) with fake agents speaking the lane protocol (``mf_fake_lanes``;
``env_fail`` is what the real agent's lane now does after one failed build -
the agent side is ``tests/unit/agent/test_v1_0_g10_lane_env_failure.py``).

* two GPU nodes, one cannot build the env: its lane ends after **one** grant,
  its lease goes back to the queue uncounted (no failed chunk try), ``(node,
  env)`` is known-bad (D116/D433), and the job **succeeds** on the other lane;
* one GPU node that cannot build the env: the job ends ``FAILED(env_error)``
  promptly (no re-plan for a non-retryable failure; never "runs on until
  cancelled" - lab job_01M72QTRYT43884YFENTMYY7N9).
* D448 backstop: an **older** agent that reports every chunk failed
  ``env_error`` (retryable) runs one chunk only - the controller ends its lane
  ``FAILED(env_error)``, releases the chunk uncounted, marks known-bad, and
  the job succeeds elsewhere (or fails ``env_error`` with no other lane).
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
from mf_fake_lanes import ENV_FAIL_HASH, FakeLane
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import build_tree
from meshforge.controller.jobs.env_health import env_key
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
GPU = ("exec", "exec-docker", "exec-gpu", "exec-split")
ENV_FAIL = (
    FailureType.ENV_ERROR,
    "env build failed on this node: setup_failed: requirements install failed (exit 1)",
    False,
)
CHUNKS = 30


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        split={
            "visibility_timeout_s": 5.0,
            "wait_retry_s": 0.2,
            "require_measured_rates": False,
        },
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
    runtime: ControllerRuntime, nodes: dict[str, tuple[float, str]]
) -> AsyncIterator[dict[str, FakeLane]]:
    """``nodes``: name -> (seconds per chunk, mode): ``ok``; ``env_fail`` (a
    D443 agent: the lane ends FAILED(env_error)); ``old_env_fail`` (an agent
    before D443: every chunk reported failed env_error, retryable - D448)."""
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: dict[str, FakeLane] = {}
        try:
            for name, (speed, mode) in nodes.items():
                fake = FakeAgent(channel)
                await fake.connect(
                    hardware=agent_pb2.HardwareSummary(
                        cpu_model="test",
                        cpu_cores=4,
                        cpu_threads=8,
                        ram_bytes=16 * GIB,
                        capabilities=list(GPU),
                        gpus=[
                            agent_pb2.HardwareSummary.Gpu(
                                index=0, model="RTX 3050", vram_bytes=4 * GIB
                            )
                        ],
                    ),
                    max_cpu_jobs=2,
                    max_gpu_jobs=1,
                )
                fake.start_heartbeats(HB_S / 2)
                lane = FakeLane(
                    fake,
                    runtime,
                    speed_s=speed,
                    env_fail=ENV_FAIL if mode == "env_fail" else None,
                )
                if mode == "old_env_fail":
                    lane.fail = set(range(CHUNKS))
                    lane.fail_kind = FailureType.ENV_ERROR
                    lane.fail_retryable = True
                lane.start()
                lanes[name] = lane
            yield lanes
        finally:
            for lane in lanes.values():
                await lane.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(lane.agent.close(), 10)


async def _submit(
    runtime: ControllerRuntime, tmp_path: Path, http: Any, *, min_nodes: int
) -> tuple[str, str]:
    root = tmp_path / "p"
    root.mkdir()
    (root / "main.py").write_bytes(b"import torch\n")
    (root / "requirements.txt").write_bytes(b"torch==2.14.0\n")
    built = build_tree(root)
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    split: dict[str, Any] = {
        "over": {"range": {"stop": CHUNKS * 2}},
        "chunk": {"items": 2},
        "speculative": False,
    }
    if min_nodes > 1:
        split["min_nodes"] = min_nodes
    manifest = {
        "name": "gpu-split-env",
        "entrypoint": "python main.py {items_file} {out}",
        "env": {"base": "python-cpu", "requirements": "requirements.txt"},
        "resources": {"gpu": "required"},
        "split": split,
    }
    got = await http.post(
        "/v1/jobs",
        json={
            "manifest": manifest,
            "tree_id": str(built.tree.tree_id),
            "placement": {},
        },
    )
    assert got.status_code == 201, got.text
    key = env_key(
        {"env": manifest["env"]}, tree_id=str(built.tree.tree_id), bundle_digest=None
    )
    return got.json()["job_id"], key


async def _wait_end(runtime: ControllerRuntime, job_id: str) -> Any:
    assert runtime.jobs is not None
    job = None
    for _ in range(2400):
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"the job never ended: {job}")


def _lane_starts(lane: FakeLane) -> list[agent_pb2.StartAttempt]:
    return [
        m.start_attempt
        for m in lane.agent.messages("start_attempt")
        if m.start_attempt.HasField("lane")
    ]


async def test_a_lane_whose_env_fails_ends_once_and_the_job_finishes_elsewhere(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    nodes = {"broken": (0.001, "env_fail"), "good": (0.02, "ok")}
    async with (
        _fabric(runtime, nodes) as lanes,
        api_client(runtime, token=token_of(runtime)) as http,
    ):
        job_id, key = await _submit(runtime, tmp_path, http, min_nodes=2)
        job = await _wait_end(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)

        broken, good = lanes["broken"], lanes["good"]
        assert len(_lane_starts(broken)) == 1  # never placed again for this env
        assert len(broken.env_failed) == 1  # one build, one grant
        assert broken.ledger == []
        assert len(good.ledger) == CHUNKS  # every chunk ran on the good lane

        rows = await _sql(
            runtime,
            "SELECT node_id, state, failure_type FROM attempt "
            "WHERE job_id = :job AND role = 'lane'",
            job=job_id,
        )
        by_node = {r.node_id: (r.state, r.failure_type) for r in rows}
        assert by_node[broken.agent.node_id] == ("failed", "env_error")
        assert by_node[good.agent.node_id][0] == "done"
        # the env-failed chunk's lease went back uncounted: no failed try
        failed_tries = await _sql(
            runtime,
            "SELECT chunk_index FROM chunk_record "
            "WHERE job_id = :job AND outcome = 'failed'",
            job=job_id,
        )
        assert failed_tries == []
        assert runtime.known_bad_envs is not None
        assert runtime.known_bad_envs.is_bad(broken.agent.node_id, key)
        (entry,) = runtime.known_bad_envs.for_node(broken.agent.node_id)
        assert entry.env_hash == ENV_FAIL_HASH
        assert not runtime.known_bad_envs.is_bad(good.agent.node_id, key)


async def test_the_only_lane_failing_its_env_fails_the_job_cleanly(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    async with (
        _fabric(runtime, {"broken": (0.001, "env_fail")}) as lanes,
        api_client(runtime, token=token_of(runtime)) as http,
    ):
        job_id, key = await _submit(runtime, tmp_path, http, min_nodes=1)
        job = await _wait_end(runtime, job_id)
        assert job.state is JobState.FAILED
        assert job.failure_type is FailureType.ENV_ERROR, job.failure_detail
        assert "env build failed on this node" in (job.failure_detail or "")
        broken = lanes["broken"]
        assert len(_lane_starts(broken)) == 1  # no re-plan onto the same node
        assert len(broken.env_failed) == 1
        assert runtime.known_bad_envs is not None
        assert runtime.known_bad_envs.is_bad(broken.agent.node_id, key)


async def test_an_old_agents_chunk_env_error_ends_its_lane(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    nodes = {"old": (0.001, "old_env_fail"), "good": (0.02, "ok")}
    async with (
        _fabric(runtime, nodes) as lanes,
        api_client(runtime, token=token_of(runtime)) as http,
    ):
        job_id, key = await _submit(runtime, tmp_path, http, min_nodes=2)
        job = await _wait_end(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        old, good = lanes["old"], lanes["good"]
        assert len(_lane_starts(old)) == 1
        assert len(old.ledger) == 1  # one chunk (= one env build), never more
        assert len(good.ledger) == CHUNKS  # every chunk ran on "good"
        rows = await _sql(
            runtime,
            "SELECT node_id, state, failure_type FROM attempt "
            "WHERE job_id = :job AND role = 'lane'",
            job=job_id,
        )
        by_node = {r.node_id: (r.state, r.failure_type) for r in rows}
        assert by_node[old.agent.node_id] == ("failed", "env_error")
        failed_tries = await _sql(
            runtime,
            "SELECT chunk_index FROM chunk_record "
            "WHERE job_id = :job AND outcome = 'failed'",
            job=job_id,
        )
        assert failed_tries == []  # released, not counted against the chunk
        assert runtime.known_bad_envs is not None
        assert runtime.known_bad_envs.is_bad(old.agent.node_id, key)
        cancels = [
            m.cancel_attempt
            for m in old.agent.messages("cancel_attempt")
            if m.cancel_attempt.reason == "env_error"
        ]
        assert len(cancels) == 1  # the agent is told to stop that lane


async def test_an_old_agent_alone_fails_the_job_env_error_after_one_chunk(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    async with (
        _fabric(runtime, {"old": (0.001, "old_env_fail")}) as lanes,
        api_client(runtime, token=token_of(runtime)) as http,
    ):
        job_id, _key = await _submit(runtime, tmp_path, http, min_nodes=1)
        job = await _wait_end(runtime, job_id)
        assert job.state is JobState.FAILED
        assert job.failure_type is FailureType.ENV_ERROR, job.failure_detail
        assert "reported per chunk" in (job.failure_detail or "")
        assert len(lanes["old"].ledger) == 1
