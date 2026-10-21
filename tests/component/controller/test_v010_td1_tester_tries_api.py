"""V0.10 TD1 tester (independent): hostile chunk stderr, end to end.

A (fake) lane reports a stderr tail full of terminal escape sequences, CRs and
C1 controls; the stored ``chunk_record.stderr_tail`` and the API
``GET /v1/jobs/{id}/chunks/{n}/tries`` must hold none of them but keep the
readable text - for failed tries and done tries alike, over a real controller
(PostgreSQL, gRPC sessions, HTTPS API).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any

import httpx
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
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(300)]

GIB = 1024**3
ENTRY = "python main.py {items_file} {out}"
HB_S = 0.5
ITEMS = 24
CHUNK = 2
CHUNKS = ITEMS // CHUNK
FAST = 0.1
FAILED_TAIL_BYTES = 4096
OK_TAIL_BYTES = 2048


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        admission_timeout_s=1.2,
        split={
            "visibility_timeout_s": 60.0,
            "wait_retry_s": 0.05,
            "chunk_stderr_tail_bytes": FAILED_TAIL_BYTES,
            "chunk_stderr_ok_bytes": OK_TAIL_BYTES,
            "speculation_cache_s": 30.0,
        },
        telemetry={"record_interval_s": 0.2},
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
    runtime: ControllerRuntime, per_lane: Sequence[dict[str, Any]]
) -> AsyncIterator[list[FakeLane]]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: list[FakeLane] = []
        try:
            for extra in per_lane:
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
                lane = FakeLane(
                    fake, runtime, **{"speed_s": FAST, "measured": True, **extra}
                )
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
    split: dict[str, Any] | None = None,
) -> str:
    built = _tree(tmp_path / "p")
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
            "retry": {"max_attempts": 3, "backoff_s": 0},
        },
        "tree_id": str(built.tree.tree_id),
        "placement": {},
    }
    got = await http.post("/v1/jobs", json=body)
    assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


async def _wait_job(
    runtime: ControllerRuntime, job_id: str, within: float = 120
) -> Any:
    assert runtime.jobs is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not end")


async def _until(check: Callable[[], Any], within: float = 30.0) -> Any:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while loop.time() < deadline:
        got = check()
        if asyncio.iscoroutine(got):
            got = await got
        if got:
            return got
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        return list((await conn.execute(text(query), params)).all())


async def _chunk_rows(runtime: ControllerRuntime, job_id: str) -> list[Any]:
    return await _sql(
        runtime,
        "SELECT * FROM chunk_record WHERE job_id = :j ORDER BY chunk_index, lease_seq",
        j=job_id,
    )


HOSTILE: dict[int, bytes] = {
    0: b"\x1b[2J\x1b[H\x1b[31mFATAL\x1b[0m: boom\r\nline2\n",
    1: b"\x1b]0;pwned title\x07\x1b]52;c;ZXZpbA==\x07clipboard attempt\n",
    2: b"progress 10%\rprogress 50%\rprogress 100%\rdone\n",
    3: "\u009b31mc1 csi\u009d0;t\u009c and \u0085nel \x00nul \x7fdel\n".encode(),
    4: b"\x1bPq-sixel\x1b\\after dcs\n\x1b]0;unterminated title\nreal error here\n",
}
BAD = r"[\x00-\x08\x0b-\x1f\x7f-\x9f]"


def _hostile(chunk: agent_pb2.GrantedChunk) -> bytes:
    return HOSTILE.get(chunk.chunk_index % 5, b"plain\n")


async def test_hostile_stderr_is_clean_in_the_db_and_the_tries_api(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    import re

    async with _lanes(
        runtime,
        [
            {"fail": set(range(CHUNKS)), "fail_once": True, "stderr_of": _hostile},
            {"stderr_of": _hostile},
            {"stderr_of": _hostile},
        ],
    ):
        job_id = await _submit(runtime, http, tmp_path, split={"speculative": False})
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, job.failure_detail
        rows = await _chunk_rows(runtime, job_id)
    tails = [r for r in rows if r.stderr_tail]
    assert any(r.outcome == "failed" for r in tails)
    assert any(r.outcome == "committed" for r in tails)
    for r in tails:
        assert not re.search(BAD, r.stderr_tail), (r.chunk_index, r.stderr_tail)
        assert "\x1b" not in r.stderr_tail and "\r" not in r.stderr_tail
    by_kind: dict[int, str] = {}
    for r in tails:
        by_kind[r.chunk_index % 5] = r.stderr_tail
    assert by_kind[0].startswith("FATAL: boom\nline2")
    assert "pwned title" in by_kind[1] or "clipboard attempt" in by_kind[1]
    assert "clipboard attempt" in by_kind[1] and "\x07" not in by_kind[1]
    assert by_kind[2] == "progress 10%\nprogress 50%\nprogress 100%\ndone\n"
    assert "nel" in by_kind[3] and "nul" in by_kind[3] and "del" in by_kind[3]
    assert "after dcs" in by_kind[4] and "real error here" in by_kind[4]

    # the REST view agrees, for every chunk, and the JSON has no escape either
    for chunk in range(CHUNKS):
        got = await http.get(f"/v1/jobs/{job_id}/chunks/{chunk}/tries")
        assert got.status_code == 200, got.text
        assert "\\u001b" not in got.text and "\\u0000" not in got.text
        for t in got.json()["tries"]:
            tail = t["stderr_tail"]
            if tail is not None:
                assert not re.search(BAD, tail), (chunk, tail)
                assert tail == next(
                    r.stderr_tail
                    for r in rows
                    if r.chunk_index == chunk and r.lease_seq == t["lease_seq"]
                )


def _only_controls(chunk: agent_pb2.GrantedChunk) -> bytes:
    return b"\x1b[2J\x00\x07\x1b]0;x\x07"


async def test_all_control_stderr_is_stored_as_null_not_empty(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(
        runtime,
        [{"stderr_of": _only_controls}, {"stderr_of": _only_controls}],
    ):
        job_id = await _submit(runtime, http, tmp_path, split={"speculative": False})
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED, job.failure_detail
        rows = await _chunk_rows(runtime, job_id)
    assert rows and all(r.stderr_tail is None for r in rows), [
        r.stderr_tail for r in rows
    ]
