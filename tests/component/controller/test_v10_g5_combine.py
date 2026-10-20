"""V0.10 G5 (0.10.7; D268, D269, D283): a split job's combine over a real
controller (PostgreSQL, gRPC sessions, HTTPS blob API) with fake agents
speaking the lane + combine wire protocol (``mf_fake_lanes``).

* the last commit -> COLLECTING; the combine is placed on the submitter's node
  (else the node with the most committed results), gets the next fencing
  token, reads the parts index and every part with its own fetch token, and
  its output becomes the job's (``GET /v1/jobs/{id}`` ``output_digest``);
* ``combine_error`` fails the job, never retried; an infrastructure failure
  or a LOST combine node is retried on another node;
* commit order does not change the output (different lane speeds, same bytes);
* ``nondeterministic_chunk``: a late second result of a committed chunk with
  another digest -> the job's warning (API), the winner kept; off with
  ``split.deterministic: false``;
* combine and lane attempts never feed the run / output history;
* a cancel while the combine runs -> CANCELLED, not retried.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_fake_lanes import FakeLane, default_output
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.jobs.queue_reads import _history
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

GIB = 1024**3
ENTRY = "python main.py {items_file} {out}"
HB_S = (
    0.5  # lease 1.5 s: survives the ~0.75 s gen-2 GC pauses of a one-process V0.10 run
)
ITEMS = 120
CHUNK = 10
CHUNKS = ITEMS // CHUNK


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        admission_timeout_s=1.2,  # default 0.5 s follows the lease; GC pauses reach 0.75 s
        split={"visibility_timeout_s": 5.0, "wait_retry_s": 0.2},
        liveness=LivenessSettings(
            heartbeat_interval_s=HB_S, miss_allowance_s=0.1, lease_multiplier=3
        ),
    )
    async with running_controller(settings) as rt:
        yield rt


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(runtime, token=token_of(runtime)) as client:
        yield client


@contextlib.asynccontextmanager
async def _lanes(
    runtime: ControllerRuntime,
    speeds: Sequence[float],
    per_lane: Sequence[dict[str, Any]] | None = None,
) -> AsyncIterator[list[FakeLane]]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: list[FakeLane] = []
        try:
            for i, speed in enumerate(speeds):
                fake = FakeAgent(channel)
                await fake.connect(
                    hardware=agent_pb2.HardwareSummary(
                        cpu_model="test",
                        cpu_cores=4,
                        cpu_threads=8,
                        ram_bytes=16 * GIB,
                        capabilities=["exec", "exec-subprocess", "exec-split"],
                    ),
                    max_cpu_jobs=2,
                )
                fake.start_heartbeats(HB_S / 2)
                extra = {} if per_lane is None else per_lane[i]
                lane = FakeLane(fake, runtime, speed_s=speed, **extra)
                lane.start()
                lanes.append(lane)
            yield lanes
        finally:
            for lane in lanes:
                await lane.close()
                if lane.killed:
                    continue
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(lane.agent.close(), 10)


def _tree(root: Path) -> BuiltTree:
    root.mkdir(parents=True)
    (root / "main.py").write_bytes(b"print(1)\n")
    return build_tree(root)


async def _submit(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    tmp_path: Path,
    *,
    name: str = "p",
    submitter: str | None = None,
    split: dict[str, Any] | None = None,
    **manifest: Any,
) -> str:
    built = _tree(tmp_path / name)
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    body: dict[str, Any] = {
        "manifest": {
            "name": "s",
            "entrypoint": ENTRY,
            "split": {
                "over": {"range": {"stop": ITEMS}},
                "chunk": {"items": CHUNK},
                **(split or {}),
            },
            **manifest,
        },
        "tree_id": str(built.tree.tree_id),
        "placement": {},
    }
    if submitter is not None:
        body["submitter_node_id"] = submitter
    got = await http.post("/v1/jobs", json=body)
    assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


async def _wait_job(
    runtime: ControllerRuntime, job_id: str, within: float = 120.0
) -> Any:
    assert runtime.jobs is not None
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not end")


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        return list((await conn.execute(text(query), params)).all())


async def _attempts(runtime: ControllerRuntime, job_id: str) -> list[Any]:
    return await _sql(
        runtime,
        "SELECT id, role, node_id, state, fencing_token, failure_type, detail, "
        "output_digest FROM attempt WHERE job_id = :job ORDER BY fencing_token",
        job=job_id,
    )


def _expected_output() -> bytes:
    """The fake combine concatenates the part blobs in chunk order."""
    return b"".join(
        default_output(list(range(first, min(first + CHUNK, ITEMS))))
        for first in range(0, ITEMS, CHUNK)
    )


async def _blob(http: httpx.AsyncClient, digest: str) -> bytes:
    got = await http.get(f"/v1/blobs/{digest.removeprefix('sha256:')}")
    assert got.status_code == 200, got.text
    return got.content


# ------------------------------------------------------------------ happy path


async def test_combine_on_the_submitter_node_and_its_output_is_the_jobs(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.002, 0.02, 0.02]) as lanes:
        submitter = lanes[2].agent.node_id
        job_id = await _submit(runtime, http, tmp_path, submitter=submitter)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        rows = await _attempts(runtime, job_id)
        combines = [r for r in rows if r.role == "combine"]
        assert len(combines) == 1
        combine = combines[0]
        assert combine.node_id == submitter  # D283 pin 2: the submitter first
        assert combine.state == "done"
        # the combine is the job's newest attempt (current fencing token)
        assert combine.fencing_token == max(r.fencing_token for r in rows)
        assert lanes[2].combines == [combine.id]
        # it read every part, in chunk order, with its own token
        parts = lanes[2].combine_parts[combine.id]
        assert len(parts) == CHUNKS
        # the job's output = the combine's output
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        assert detail["output_digest"] == combine.output_digest
        assert {a["role"] for a in detail["attempts"]} == {"lane", "combine"}
        assert detail["warnings"] == []
        data = await _blob(http, combine.output_digest)
        assert data == _expected_output()
        assert job.split is not None and job.split.combine_ms == 250
        history = [
            (r.from_state, r.to_state)
            for r in await _sql(
                runtime,
                "SELECT from_state, to_state FROM job_transition WHERE job_id = :j "
                "ORDER BY seq",
                j=job_id,
            )
        ]
        assert history[-2:] == [("running", "collecting"), ("collecting", "succeeded")]


async def test_combine_prefers_the_node_with_the_most_results(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.002, 0.06, 0.06]) as lanes:
        job_id = await _submit(runtime, http, tmp_path)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        sites = await _sql(
            runtime,
            "SELECT l.site, count(*) AS n FROM work_item i "
            "JOIN work_queue q ON q.id = i.queue_id "
            "JOIN work_lease l ON l.item_id = i.id AND l.seq = i.done_lease_seq "
            "WHERE q.owner_id = :job GROUP BY l.site ORDER BY n DESC",
            job=job_id,
        )
        assert sites[0].n > sites[1].n, sites
        (combine,) = [
            r for r in await _attempts(runtime, job_id) if r.role == "combine"
        ]
        assert combine.node_id == sites[0].site
        assert combine.node_id == lanes[0].agent.node_id


# ------------------------------------------------------------------ failures


async def test_combine_error_fails_the_job_and_is_never_retried(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    refuse = {
        "combine_fail": (
            FailureType.COMBINE_ERROR,
            "header_mismatch: out.csv: the header of part 00003 differs",
            False,
        )
    }
    async with _lanes(runtime, [0.002, 0.002], [refuse, refuse]):
        job_id = await _submit(runtime, http, tmp_path, retry={"max_attempts": 5})
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.FAILED
        assert job.failure_type is FailureType.COMBINE_ERROR
        assert "header_mismatch" in (job.failure_detail or "")
        await asyncio.sleep(0.5)  # a retry would have been placed by now
        combines = [r for r in await _attempts(runtime, job_id) if r.role == "combine"]
        assert [(c.state, c.failure_type) for c in combines] == [
            ("failed", "combine_error")
        ]
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        assert detail["output_digest"] is None


async def test_a_retryable_combine_failure_is_retried_on_another_node(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    flaky = {"combine_fail": (FailureType.TRANSFER_ERROR, "connection reset", True)}
    async with _lanes(runtime, [0.002, 0.002], [flaky, {}]) as lanes:
        job_id = await _submit(
            runtime,
            http,
            tmp_path,
            submitter=lanes[0].agent.node_id,
            retry={"max_attempts": 3, "backoff_s": 0},
        )
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        combines = [r for r in await _attempts(runtime, job_id) if r.role == "combine"]
        assert [(c.node_id, c.state) for c in combines] == [
            (lanes[0].agent.node_id, "failed"),
            (lanes[1].agent.node_id, "done"),  # the failed node is avoided
        ]
        assert await _blob(http, combines[1].output_digest) == _expected_output()


async def test_a_lost_combine_node_is_retried_on_another_node(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.002, 0.002], [{"combine_hold": True}, {}]) as lanes:
        victim = lanes[0]
        job_id = await _submit(
            runtime,
            http,
            tmp_path,
            submitter=victim.agent.node_id,
            retry={"max_attempts": 3, "backoff_s": 0},
        )
        for _ in range(400):
            if victim.combines and victim.combine_parts:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the combine never reached the submitter's node")
        await victim.kill()
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        combines = [r for r in await _attempts(runtime, job_id) if r.role == "combine"]
        assert [(c.node_id, c.state, c.failure_type) for c in combines] == [
            (victim.agent.node_id, "lost", "node_lost"),
            (lanes[1].agent.node_id, "done", None),
        ]
        assert await _blob(http, combines[1].output_digest) == _expected_output()


async def test_cancel_while_the_combine_runs_ends_cancelled(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.002], [{"combine_hold": True}]) as lanes:
        job_id = await _submit(runtime, http, tmp_path, retry={"max_attempts": 3})
        for _ in range(400):
            if lanes[0].combine_parts:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("no combine started")
        got = await http.post(f"/v1/jobs/{job_id}/cancel")
        assert got.status_code == 202, got.text  # asked of the combine's node
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.CANCELLED
        await asyncio.sleep(0.5)
        combines = [r for r in await _attempts(runtime, job_id) if r.role == "combine"]
        assert len(combines) == 1


# ------------------------------------------------------------------ determinism


async def test_commit_order_does_not_change_the_output(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    outputs: list[str] = []
    orders: list[list[int]] = []
    for n, speeds in enumerate(([0.002, 0.03, 0.05], [0.05, 0.002, 0.01])):
        async with _lanes(runtime, speeds):
            job_id = await _submit(runtime, http, tmp_path, name=f"p{n}")
            job = await _wait_job(runtime, job_id)
            assert job.state is JobState.SUCCEEDED, job.failure_detail
        (combine,) = [
            r for r in await _attempts(runtime, job_id) if r.role == "combine"
        ]
        outputs.append(combine.output_digest)
        orders.append(
            [
                r.idx
                for r in await _sql(
                    runtime,
                    "SELECT i.idx FROM work_item i JOIN work_queue q "
                    "ON q.id = i.queue_id WHERE q.owner_id = :job "
                    "ORDER BY i.done_at, i.idx",
                    job=job_id,
                )
            ]
        )
    assert orders[0] != list(range(CHUNKS)) or orders[1] != list(range(CHUNKS))
    assert outputs[0] == outputs[1]
    assert await _blob(http, outputs[0]) == _expected_output()


def _lane_a_output(items: Sequence[str | int]) -> bytes:
    return b"A" + default_output(items)


async def test_a_second_result_with_another_digest_is_a_job_warning(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Lane A's first chunk result arrives after its lease expired and lane B
    committed that chunk: same chunk, other bytes -> ``nondeterministic_chunk``
    (D269), the committed (B's) result kept."""
    late = {"delay_first_result_s": 7.0, "output_of": _lane_a_output}
    async with _lanes(runtime, [0.002, 0.05], [late, {}]) as lanes:
        job_id = await _submit(runtime, http, tmp_path)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        a_chunk = lanes[0].ledger[0][0]
        for _ in range(200):
            detail = (await http.get(f"/v1/jobs/{job_id}")).json()
            if detail["warnings"]:
                break
            await asyncio.sleep(0.05)
        assert detail["warnings"] == [
            {
                "code": "nondeterministic_chunk",
                "count": 1,
                "chunks": [a_chunk],
                "detail": detail["warnings"][0]["detail"],
            }
        ]
        assert "first committed result was kept" in detail["warnings"][0]["detail"]
        assert runtime.dispatcher is not None
        assert runtime.dispatcher.split.stats.nondeterministic == 1
        # the winner is B's output: the combined output is the honest one
        assert await _blob(http, detail["output_digest"]) == _expected_output()


async def test_no_warning_when_the_job_is_not_deterministic(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    late = {"delay_first_result_s": 7.0, "output_of": _lane_a_output}
    async with _lanes(runtime, [0.002, 0.05], [late, {}]) as lanes:
        job_id = await _submit(runtime, http, tmp_path, split={"deterministic": False})
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        for _ in range(120):  # the late result is in once lane A is DONE
            states = [r.state for r in await _attempts(runtime, job_id)]
            if all(s == "done" for s in states):
                break
            await asyncio.sleep(0.05)
        assert lanes[0].results_sent
        detail = (await http.get(f"/v1/jobs/{job_id}")).json()
        assert detail["warnings"] == []


# ------------------------------------------------------------------ history


async def test_combine_and_lane_attempts_never_feed_the_run_history(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.002, 0.002]):
        job_id = await _submit(runtime, http, tmp_path)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
    (row,) = await _sql(runtime, "SELECT fingerprint FROM job WHERE id = :j", j=job_id)
    assert row.fingerprint
    phases = await _sql(
        runtime,
        "SELECT a.role, count(*) AS n FROM attempt_phase p JOIN attempt a "
        "ON a.id = p.attempt_id WHERE a.job_id = :j AND p.phase = 'run' "
        "GROUP BY a.role",
        j=job_id,
    )
    assert {r.role for r in phases} >= {"combine"}  # it has a run phase...
    assert runtime._engine is not None
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(runtime._engine) as db:
        history = await _history(db, [row.fingerprint], 20)
    # ...that no history reads (a whole run of this workload never happened)
    found = history.get(row.fingerprint)
    assert found is None or (not found.by_node_s and found.output_samples == 0)
