"""V0.10 G7 tester r1 (0.10.8; D270, checklist 37): attack tests on split
progress over a real controller (PostgreSQL, gRPC sessions, HTTPS API, bus).

* snapshot atomicity: while chunks commit under four concurrent readers every
  REST summary / event is internally consistent (counts sum, map == counts,
  lanes sum to the totals) and `done` never goes backwards;
* LOST lane / failed tries: the shown lane state, tries and rates equal the
  rows; a dead lane never feeds the ETA;
* ETA is empty until a live lane commits, never negative / NaN, 0 at the end;
* paging under concurrent commits: no duplicate, no gap, strictly ascending;
  `start` / `limit` / cursor edges;
* access: unknown job 404, non-owner of a Mode 1 job gets "no such job" (no
  existence leak), garbage ids, no token;
* warnings: surfaced in REST and in the event; malformed rows never 500;
* real controller restart: the new run shows the same rows;
* a 10 000-chunk job: summary latency / memory / size.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
import tracemalloc
from collections.abc import AsyncIterator, Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_fake_lanes import FakeLane
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent
from mf_users import create_user, login

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.events.bus import BusEvent, EventType, Subscriber
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(420)]

GIB = 1024**3
ENTRY = "python main.py {items_file} {out}"
HB_S = 0.2
HZ = 2.0
UNKNOWN = "job_01J0000000000000000000ZZZZ"


def _settings(pg_settings: DatabaseSettings, **split: Any) -> Any:
    knobs: dict[str, Any] = {
        "visibility_timeout_s": 120.0,
        "wait_retry_s": 0.2,
        "progress_event_hz": HZ,
        "progress_resync_s": 1.0,
    }
    knobs.update(split)
    return g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        split=knobs,
        liveness=LivenessSettings(
            heartbeat_interval_s=HB_S, miss_allowance_s=0.1, lease_multiplier=3
        ),
    )


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    async with running_controller(_settings(pg_settings)) as rt:
        yield rt


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(runtime, token=token_of(runtime)) as client:
        yield client


@contextlib.asynccontextmanager
async def _lanes(
    runtime: ControllerRuntime, speeds: Sequence[float], **kw: Any
) -> AsyncIterator[list[FakeLane]]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: list[FakeLane] = []
        try:
            for speed in speeds:
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
                lane = FakeLane(fake, runtime, speed_s=speed, **kw)
                lane.start()
                lanes.append(lane)
            yield lanes
        finally:
            for lane in lanes:
                if lane.gate is not None:
                    lane.gate.set()
                await lane.close()
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
    items: int,
    chunk: int,
    name: str = "p",
    split: bool = True,
    retry: dict[str, Any] | None = None,
) -> str:
    built = _tree(tmp_path / name)
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    manifest: dict[str, Any] = {"name": "s", "entrypoint": ENTRY}
    if split:
        manifest["split"] = {
            "over": {"range": {"stop": items}},
            "chunk": {"items": chunk},
        }
    else:
        manifest["entrypoint"] = "python main.py"
    if retry:
        manifest["retry"] = retry
    got = await http.post(
        "/v1/jobs",
        json={
            "manifest": manifest,
            "tree_id": str(built.tree.tree_id),
            "placement": {},
        },
    )
    assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.begin() as conn:
        res = await conn.execute(text(query), params)
        return list(res.all()) if res.returns_rows else []


async def _wait_job(
    runtime: ControllerRuntime, job_id: str, wait_s: float = 200
) -> Any:
    assert runtime.jobs is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while loop.time() < deadline:
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not end")


async def _done_count(runtime: ControllerRuntime, job_id: str) -> int:
    rows = await _sql(
        runtime,
        "SELECT count(*) AS n FROM work_item i JOIN work_queue q "
        "ON q.id = i.queue_id WHERE q.owner_id = :job AND i.state = 'done'",
        job=job_id,
    )
    return int(rows[0].n)


async def _until_done(runtime: ControllerRuntime, job_id: str, n: int) -> None:
    for _ in range(2000):
        if await _done_count(runtime, job_id) >= n:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"never reached {n} done chunks")


def _events(sub: Subscriber, job_id: str) -> list[BusEvent]:
    out = []
    while not sub.queue.empty():
        event = sub.queue.get_nowait()
        if event.type is EventType.SPLIT_PROGRESS and event.data["job_id"] == job_id:
            out.append(event)
    return out


def _consistent(s: dict[str, Any], *, one_item_chunks: bool) -> None:
    """Invariants of ONE summary / event, whatever moves around it."""
    c = s["chunks"]
    assert c["total"] == c["pending"] + c["leased"] + c["done"] + c["failed"], c
    assert all(v >= 0 for v in c.values()), c
    lanes = s["lanes"]
    assert sum(lane["chunks_done"] for lane in lanes) == c["done"], (c, lanes)
    assert sum(lane["items_done"] for lane in lanes) == s["items_done"]
    assert sum(len(lane["current"]) for lane in lanes) == s["live_leases"]
    assert c["leased"] <= s["live_leases"]
    assert s["speculative_live"] <= s["live_leases"]
    if one_item_chunks:
        assert s["items_done"] == c["done"]
    assert 0 <= s["items_done"] <= s["items_total"]
    for lane in lanes:
        rate = lane["rate_items_s"]
        assert rate is None or (math.isfinite(rate) and rate > 0), lane
        if not lane["live"]:
            assert lane["current"] == [], lane
    eta = s["eta_s"]
    assert eta is None or (math.isfinite(eta) and eta >= 0), s
    if eta is None or eta > 0:
        assert s["items_done"] <= s["items_total"]
    if s["rate_items_s"] is not None:
        live = [x["rate_items_s"] for x in lanes if x["live"] and x["rate_items_s"]]
        assert s["rate_items_s"] == pytest.approx(sum(live), rel=1e-5, abs=1e-5)
    el = s["elapsed_s"]
    assert el is None or (math.isfinite(el) and el >= 0)
    if "chunk_map" in s and s["chunk_map"] is not None:
        m = s["chunk_map"]
        assert len(m) == c["total"], (len(m), c)
        assert m.count("d") == c["done"]
        assert m.count("p") == c["pending"]
        assert m.count("f") == c["failed"]
        assert m.count("l") + m.count("s") == c["leased"]


# ------------------------------------------------------------------ atomicity


async def test_every_snapshot_is_self_consistent_while_chunks_commit(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """4 concurrent readers + the bus against 3 lanes committing 1-item chunks
    as fast as they can: no reader ever sees a torn snapshot."""
    assert runtime.events is not None
    sub, _ = runtime.events.subscribe()
    seen_events: list[BusEvent] = []
    async with _lanes(runtime, [0.0, 0.0, 0.0]):
        job_id = await _submit(runtime, http, tmp_path, items=500, chunk=1)
        stop = asyncio.Event()
        reads = 0

        async def reader() -> None:
            nonlocal reads
            last_done = -1
            while not stop.is_set():
                resp = await http.get(
                    f"/v1/jobs/{job_id}/split", params={"map": "true"}
                )
                assert resp.status_code == 200, resp.text
                body = resp.json()
                _consistent(body, one_item_chunks=True)
                assert body["chunks"]["done"] >= last_done
                last_done = body["chunks"]["done"]
                reads += 1

        readers = [asyncio.create_task(reader()) for _ in range(4)]
        try:
            job = await _wait_job(runtime, job_id)
        finally:
            stop.set()
            await asyncio.gather(*readers)
        assert job.state is JobState.SUCCEEDED
        assert reads > 10
        for _ in range(60):
            seen_events.extend(_events(sub, job_id))
            if seen_events and seen_events[-1].data["job_state"] == "succeeded":
                break
            await asyncio.sleep(0.1)
    runtime.events.unsubscribe(sub)
    assert seen_events
    for ev in seen_events:
        _consistent(dict(ev.data), one_item_chunks=True)
    assert seen_events[-1].data["chunks"]["done"] == 500
    final = (await http.get(f"/v1/jobs/{job_id}/split")).json()
    assert final["eta_s"] == 0.0
    assert final["chunks"] == {
        "total": 500,
        "pending": 0,
        "leased": 0,
        "done": 500,
        "failed": 0,
    }


async def test_several_jobs_each_have_a_bounded_stream_ending_in_their_final_state(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.events is not None
    sub, _ = runtime.events.subscribe()
    async with _lanes(runtime, [0.0, 0.0, 0.0]):
        ids = [
            await _submit(runtime, http, tmp_path, items=120, chunk=1, name=f"j{i}")
            for i in range(3)
        ]
        jobs = [await _wait_job(runtime, i) for i in ids]
        assert all(j.state is JobState.SUCCEEDED for j in jobs)
        collected: list[BusEvent] = []
        for _ in range(100):
            while not sub.queue.empty():
                e = sub.queue.get_nowait()
                if e.type is EventType.SPLIT_PROGRESS:
                    collected.append(e)
            last = {e.data["job_id"]: e for e in collected}
            if all(i in last and last[i].data["job_state"] == "succeeded" for i in ids):
                break
            await asyncio.sleep(0.1)
    runtime.events.unsubscribe(sub)
    for job_id in ids:
        mine = [e for e in collected if e.data["job_id"] == job_id]
        assert mine[-1].data["job_state"] == "succeeded", job_id
        assert mine[-1].data["chunks"]["done"] == 120
        gaps = [b.at - a.at for a, b in pairwise(mine)]
        assert all(g >= 1 / HZ - 0.002 for g in gaps), gaps
        dones = [e.data["chunks"]["done"] for e in mine]
        assert dones == sorted(dones)


# ------------------------------------------------------------------ failures


async def test_a_lost_lane_is_shown_lost_and_never_feeds_the_eta(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.02, 0.02, 30.0]) as lanes:
        job_id = await _submit(runtime, http, tmp_path, items=300, chunk=10)
        victim = lanes[2]
        for _ in range(800):
            held = await _sql(
                runtime,
                "SELECT l.id FROM work_lease l JOIN attempt a ON a.id = l.holder "
                "WHERE a.job_id = :job AND a.node_id = :node AND l.state = 'live'",
                job=job_id,
                node=victim.agent.node_id,
            )
            if held:
                break
            await asyncio.sleep(0.01)
        assert held
        await victim.kill()
        snaps: list[dict[str, Any]] = []
        job = None
        for _ in range(4000):
            snaps.append((await http.get(f"/v1/jobs/{job_id}/split")).json())
            job = await runtime.jobs.get_job(job_id) if runtime.jobs else None
            if job is not None and job.state is JobState.SUCCEEDED:
                break
            await asyncio.sleep(0.02)
        assert job is not None and job.state is JobState.SUCCEEDED
        for s in snaps:
            _consistent(s, one_item_chunks=False)
        final = (await http.get(f"/v1/jobs/{job_id}/split")).json()
    attempts = {
        r.id: r.state
        for r in await _sql(
            runtime,
            "SELECT id, state FROM attempt WHERE job_id = :job AND role = 'lane'",
            job=job_id,
        )
    }
    shown = {lane["attempt_id"]: lane for lane in final["lanes"]}
    assert set(shown) == set(attempts)
    assert final["lanes_total"] == len(attempts)
    for att, state in attempts.items():
        assert shown[att]["state"] == state
    dead = [lane for lane in final["lanes"] if lane["state"] == "lost"]
    assert len(dead) == 1 and dead[0]["live"] is False and dead[0]["current"] == []
    # the dead lane's committed chunks (if any) stay attributed to it
    assert sum(lane["chunks_done"] for lane in final["lanes"]) == 30
    # while the victim was dead the summed rate never included it
    for s in snaps:
        lost = {x["attempt_id"] for x in s["lanes"] if not x["live"]}
        live_sum = sum(
            x["rate_items_s"] or 0 for x in s["lanes"] if x["attempt_id"] not in lost
        )
        if s["rate_items_s"] is not None:
            assert s["rate_items_s"] == pytest.approx(live_sum, rel=1e-5, abs=1e-5)


async def test_failed_tries_per_lane_equal_the_lease_rows(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.0, 0.03, 0.03]) as lanes:
        bad = lanes[0]
        bad.fail.update({0, 1, 2, 3, 4, 5})  # whatever it gets first, once
        job_id = await _submit(
            runtime, http, tmp_path, items=200, chunk=10, retry={"max_attempts": 10}
        )

        async def heal() -> None:
            # a transient fault: after its first failed try the lane works
            for _ in range(2000):
                rows = await _sql(
                    runtime,
                    "SELECT count(*) AS n FROM work_lease WHERE state = 'failed' "
                    "AND holder IN (SELECT id FROM attempt WHERE job_id = :job)",
                    job=job_id,
                )
                if rows[0].n >= 1:
                    bad.fail.clear()
                    return
                await asyncio.sleep(0.01)

        healer = asyncio.create_task(heal())
        job = await _wait_job(runtime, job_id)
        await healer
        assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)
        got = (await http.get(f"/v1/jobs/{job_id}/split")).json()
    rows = await _sql(
        runtime,
        "SELECT l.holder, count(*) AS n FROM work_lease l "
        "JOIN work_item i ON i.id = l.item_id JOIN work_queue q ON q.id = i.queue_id "
        "WHERE q.owner_id = :job AND l.state = 'failed' GROUP BY l.holder",
        job=job_id,
    )
    expected = {r.holder: r.n for r in rows}
    shown = {lane["attempt_id"]: lane["failed_tries"] for lane in got["lanes"]}
    for holder, n in expected.items():
        assert shown[holder] == n
    assert sum(shown.values()) == sum(expected.values())
    assert sum(expected.values()) >= 1, "the failing lane never failed a chunk"
    _consistent(got, one_item_chunks=False)


# ------------------------------------------------------------------ ETA


async def test_eta_is_empty_until_a_live_lane_commits_and_never_odd(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.events is not None
    sub, _ = runtime.events.subscribe()
    gate = asyncio.Event()  # closed: no result is ever sent
    async with _lanes(runtime, [0.0, 0.0], gate=gate):
        job_id = await _submit(runtime, http, tmp_path, items=100, chunk=5)
        for _ in range(300):
            s = (await http.get(f"/v1/jobs/{job_id}/split")).json()
            if s["live_leases"] >= 2:
                break
            await asyncio.sleep(0.02)
        assert s["live_leases"] >= 2
        await asyncio.sleep(1.2)  # wall time passes with nothing committed
        s = (await http.get(f"/v1/jobs/{job_id}/split")).json()
        assert s["chunks"]["done"] == 0
        assert s["eta_s"] is None and s["rate_items_s"] is None
        assert all(lane["rate_items_s"] is None for lane in s["lanes"])
        assert all(lane["last_commit_at"] is None for lane in s["lanes"])
        events = _events(sub, job_id)
        assert events and all(e.data["eta_s"] is None for e in events)
        gate.set()
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        end = (await http.get(f"/v1/jobs/{job_id}/split")).json()
        assert end["eta_s"] == 0.0 and end["job_state"] == "succeeded"
        assert end["elapsed_s"] is not None and end["elapsed_s"] >= 1.2
    runtime.events.unsubscribe(sub)


async def test_a_cancelled_job_keeps_its_rows_and_has_no_eta(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.events is not None
    sub, _ = runtime.events.subscribe()
    async with _lanes(runtime, [0.05, 0.05]):
        job_id = await _submit(runtime, http, tmp_path, items=400, chunk=2)
        await _until_done(runtime, job_id, 10)
        cancelled = await http.post(f"/v1/jobs/{job_id}/cancel")
        assert cancelled.status_code in (200, 202), cancelled.text
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.CANCELLED
        got = (await http.get(f"/v1/jobs/{job_id}/split", params={"map": "1"})).json()
        _consistent(got, one_item_chunks=False)
        assert got["job_state"] == "cancelled"
        assert 0 < got["chunks"]["done"] < 200
        assert got["eta_s"] is None  # closed queue, work left: nothing claimed
        assert got["ended_at"] is not None
        seen: list[BusEvent] = []
        for _ in range(60):
            seen.extend(_events(sub, job_id))
            if seen and seen[-1].data["job_state"] == "cancelled":
                break
            await asyncio.sleep(0.1)
        assert seen[-1].data["job_state"] == "cancelled"
        # the numbers shown after the end equal the rows
        want = await _sql(
            runtime,
            "SELECT count(*) FILTER (WHERE i.state = 'done') AS done, count(*) AS n "
            "FROM work_item i JOIN work_queue q ON q.id = i.queue_id "
            "WHERE q.owner_id = :job",
            job=job_id,
        )
        assert (
            got["chunks"]["done"] == want[0].done
            and got["chunks"]["total"] == want[0].n
        )
    runtime.events.unsubscribe(sub)


# ------------------------------------------------------------------ paging


async def test_paging_while_chunks_commit_never_duplicates_or_skips(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.002, 0.002, 0.002]) as lanes:
        job_id = await _submit(runtime, http, tmp_path, items=300, chunk=1)
        node = lanes[0].agent.node_id
        await _until_done(runtime, job_id, 1)
        for flt in ({}, {"state": ["pending"]}, {"state": ["leased", "done"]}):
            walked: list[dict[str, Any]] = []
            cursor = None
            while True:
                params: dict[str, Any] = {"limit": 7, **flt}
                if cursor:
                    params["cursor"] = cursor
                page = (
                    await http.get(f"/v1/jobs/{job_id}/chunks", params=params)
                ).json()
                walked.extend(page["chunks"])
                cursor = page["next_cursor"]
                assert (cursor is None) == (page["count"] < 7 or cursor is None)
                if cursor is None:
                    break
                await asyncio.sleep(0.005)
            idx = [c["chunk_index"] for c in walked]
            assert idx == sorted(set(idx)), "duplicate or unordered chunk in a walk"
            if not flt:
                # states move between pages but the key space is stable: a
                # complete, gapless walk of all 300 chunks
                assert idx == list(range(300))
        # node filter while moving: every row relates to the node
        page = (
            await http.get(
                f"/v1/jobs/{job_id}/chunks", params={"node": node, "limit": 500}
            )
        ).json()
        for c in page["chunks"]:
            assert c["node_id"] == node or node in {x["node_id"] for x in c["leases"]}
        assert (await _wait_job(runtime, job_id)).state is JobState.SUCCEEDED
        by_node: dict[str, int] = {}
        for lane in lanes:
            got = (
                await http.get(
                    f"/v1/jobs/{job_id}/chunks",
                    params={"node": lane.agent.node_id, "limit": 500},
                )
            ).json()
            by_node[lane.agent.node_id] = got["count"]
        assert sum(by_node.values()) == 300  # each chunk belongs to exactly one node


async def test_start_limit_and_cursor_edges(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.0, 0.0]):
        job_id = await _submit(runtime, http, tmp_path, items=23, chunk=5)  # 5 chunks
        assert (await _wait_job(runtime, job_id)).state is JobState.SUCCEEDED
    base = f"/v1/jobs/{job_id}/chunks"

    async def get(**p: Any) -> httpx.Response:
        return await http.get(base, params=p)

    first = (await get(start=0, limit=1)).json()
    assert [c["chunk_index"] for c in first["chunks"]] == [0]
    assert first["next_cursor"] is not None
    last = (await get(start=4, limit=1)).json()
    assert [c["chunk_index"] for c in last["chunks"]] == [4]
    assert last["chunks"][0]["item_count"] == 3 and last["next_cursor"] is None
    for beyond in (5, 6, 9_999):
        r = await get(start=beyond)
        assert r.status_code == 200 and r.json()["chunks"] == []
        assert r.json()["next_cursor"] is None
    for bad in (-1, 10_000, 10**12):
        assert (await get(start=bad)).status_code in (400, 422), bad
    assert (await get(start="x")).status_code in (400, 422)
    assert (await get(limit=1, start=1.5)).status_code in (400, 422)
    # limit bounds
    assert (await get(limit=500)).status_code == 200
    for bad_limit in (0, -1, 501, "x", ""):
        assert (await get(limit=bad_limit)).status_code in (400, 422), bad_limit
    # exact page boundary: limit == remaining -> no next cursor
    page = (await get(limit=5)).json()
    assert page["count"] == 5 and page["next_cursor"] is None
    page = (await get(limit=4)).json()
    assert page["count"] == 4 and page["next_cursor"] is not None
    tail = (await get(limit=4, cursor=page["next_cursor"])).json()
    assert [c["chunk_index"] for c in tail["chunks"]] == [4]
    # a cursor past the end is an empty page, not an error
    from meshforge.controller.api.split_routes import encode_chunk_cursor

    r = await get(cursor=encode_chunk_cursor(9_999))
    assert r.status_code == 200 and r.json()["chunks"] == []
    # invalid cursors / filters
    for bad_cursor in ("", "!!!", "a" * 41, "djF8Y2h1bmt8MTAwMDA"):
        r = await get(cursor=bad_cursor)
        assert r.status_code in (400, 422), (bad_cursor, r.status_code)
    assert (await get(state="bogus")).status_code in (400, 422)
    assert (await get(state="DONE")).status_code in (400, 422)
    assert (await get(node="node_01J0000000000000000000UNKN")).status_code in (400, 200)
    unknown_node = "node_01J00000000000000000000000"
    r = await get(node=unknown_node)
    assert r.status_code == 200 and r.json()["chunks"] == []
    assert (await get(node="x" * 41)).status_code in (400, 422)
    assert (await get(node="'; DROP TABLE work_item;--")).status_code in (400, 422)
    # repeated and duplicate state filters
    r = await get(state=["done", "done", "failed"])
    assert r.status_code == 200 and r.json()["count"] == 5
    # still intact
    assert (await get()).json()["count"] == 5


# ------------------------------------------------------------------ access


async def test_access_unknown_foreign_and_garbage_ids(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime._engine is not None
    await create_user(runtime._engine, "alice", "alice-password-123")
    await create_user(runtime._engine, "mallory", "mallory-password-1")
    async with api_client(runtime) as anon:
        alice = await login(anon, "alice", "alice-password-123")
        mallory = await login(anon, "mallory", "mallory-password-1")
    async with (
        api_client(runtime, token=alice) as as_alice,
        api_client(runtime, token=mallory) as as_mallory,
        api_client(runtime, token="not-a-token") as bad_token,
        api_client(runtime) as anon,
        _lanes(runtime, [0.0]),
    ):
        split_id = await _submit(runtime, as_alice, tmp_path, items=10, chunk=5)
        whole_id = await _submit(
            runtime, as_alice, tmp_path, items=1, chunk=1, name="w", split=False
        )
        for path in ("split", "chunks"):
            # unknown job: 404 "no such job" for user and admin alike
            for who in (as_alice, as_mallory, http):
                r = await who.get(f"/v1/jobs/{UNKNOWN}/{path}")
                assert r.status_code == 404, r.text
                assert r.json()["error"]["message"] == "no such job"
            # a foreign Mode 1 job looks exactly like a missing one (no leak)
            r = await as_mallory.get(f"/v1/jobs/{whole_id}/{path}")
            assert (
                r.status_code == 404 and r.json()["error"]["message"] == "no such job"
            )
            # the owner / admin of a Mode 1 job learn it is not a split job
            for who in (as_alice, http):
                r = await who.get(f"/v1/jobs/{whole_id}/{path}")
                assert r.status_code == 404
                assert r.json()["error"]["message"] == "no such split job"
            # the owner of a split job: ok, even before it is dispatched
            assert (
                await as_alice.get(f"/v1/jobs/{split_id}/{path}")
            ).status_code == 200
            assert (
                await as_mallory.get(f"/v1/jobs/{split_id}/{path}")
            ).status_code == 404
            # no / bad credentials
            assert (await anon.get(f"/v1/jobs/{split_id}/{path}")).status_code == 401
            assert (
                await bad_token.get(f"/v1/jobs/{split_id}/{path}")
            ).status_code == 401
            # garbage ids never reach SQL
            for junk in (
                "job_",
                "JOB_01J0000000000000000000ZZZZ",
                "job_01J0000000000000000000ZZZ%00",
                "..%2f..%2fetc",
                "job_01J0000000000000000000ZZZZ'--",
                "x" * 41,
                "x" * 5000,
            ):
                r = await http.get(f"/v1/jobs/{junk}/{path}")
                assert r.status_code in (400, 404, 422), (junk, r.status_code)
                assert r.status_code != 500
        # state-changing verbs are not offered on these paths
        for verb in ("post", "put", "delete", "patch"):
            r = await getattr(http, verb)(f"/v1/jobs/{split_id}/split")
            assert r.status_code in (404, 405)


# ------------------------------------------------------------------ warnings


async def test_warnings_surface_in_rest_and_event_and_bad_rows_never_500(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.events is not None
    sub, _ = runtime.events.subscribe()
    gate = asyncio.Event()
    gate.set()
    async with _lanes(runtime, [0.01, 0.01], gate=gate):
        job_id = await _submit(runtime, http, tmp_path, items=200, chunk=5)
        await _until_done(runtime, job_id, 3)
        gate.clear()
        await asyncio.sleep(0.8)
        warn = [
            {
                "code": "nondeterministic_chunk",
                "count": 7,
                "chunks": [1, 2],
                "detail": "d",
            }
        ]
        await _sql(
            runtime,
            "UPDATE job SET warnings = CAST(:w AS jsonb) WHERE id = :id",
            w=json.dumps(warn),
            id=job_id,
        )
        got = (await http.get(f"/v1/jobs/{job_id}/split")).json()
        assert got["warnings"] == [{"code": "nondeterministic_chunk", "count": 7}]
        # a frozen job: nobody touches it - the resync alone must publish it
        seen: list[BusEvent] = []
        for _ in range(60):
            seen.extend(_events(sub, job_id))
            if seen and seen[-1].data["warnings"]:
                break
            await asyncio.sleep(0.1)
        assert seen[-1].data["warnings"] == [
            {"code": "nondeterministic_chunk", "count": 7}
        ]
        # malformed warning rows are dropped, never a 500
        for junk in (
            '[{"code": "nope", "count": 1}]',
            '[{"code": "nondeterministic_chunk", "count": "x"}]',
            '[{"code": "nondeterministic_chunk", "count": -4}]',
            '[{"code": "nondeterministic_chunk"}]',
            '{"code": "nondeterministic_chunk"}',
            '"text"',
            "null",
            "[1, null, []]",
        ):
            await _sql(
                runtime,
                "UPDATE job SET warnings = CAST(:w AS jsonb) WHERE id = :id",
                w=junk,
                id=job_id,
            )
            r = await http.get(f"/v1/jobs/{job_id}/split")
            assert r.status_code == 200, (junk, r.text)
            assert r.json()["warnings"] == [], junk
        gate.set()
        assert (await _wait_job(runtime, job_id)).state is JobState.SUCCEEDED
    runtime.events.unsubscribe(sub)


# ------------------------------------------------------------------ restart


async def test_a_restarted_controller_shows_and_publishes_the_same_rows(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings)
    gate = asyncio.Event()
    gate.set()
    async with (
        running_controller(settings) as rt1,
        api_client(rt1, token=token_of(rt1)) as c1,
    ):
        async with _lanes(rt1, [0.01, 0.02], gate=gate) as lanes:
            job_id = await _submit(rt1, c1, tmp_path, items=200, chunk=5)
            await _until_done(rt1, job_id, 8)
            gate.clear()
            await asyncio.sleep(0.5)
            for lane in lanes:  # dead, not merely held: nothing lands later
                await lane.kill()
        # the lanes are gone (their release may have landed a few results):
        # these are the rows the next controller starts from
        await asyncio.sleep(1.0)
        before = (await c1.get(f"/v1/jobs/{job_id}/split", params={"map": "1"})).json()
        _consistent(before, one_item_chunks=False)
        assert before["chunks"]["done"] >= 8
    # a new controller process-equivalent on the same database
    async with running_controller(settings) as rt2:
        assert rt2.events is not None
        sub, _ = rt2.events.subscribe(since=0)
        async with api_client(rt2, token=token_of(rt2)) as c2:
            after = (
                await c2.get(f"/v1/jobs/{job_id}/split", params={"map": "1"})
            ).json()
            _consistent(after, one_item_chunks=False)
            assert after["chunks"]["done"] == before["chunks"]["done"]
            assert after["items_done"] == before["items_done"]
            assert after["chunks"]["total"] == before["chunks"]["total"]
            assert after["chunk_map"].count("d") == before["chunk_map"].count("d")
            # the new tracker publishes this job without anyone touching it
            seen: list[BusEvent] = []
            for _ in range(80):
                seen.extend(_events(sub, job_id))
                if seen:
                    break
                await asyncio.sleep(0.1)
            state_now = (await c2.get(f"/v1/jobs/{job_id}")).json()["state"]
            assert seen, f"no split.progress after the restart (job {state_now})"
            first = seen[0].data
            assert first["chunks"]["done"] == before["chunks"]["done"]
            assert first["items_done"] == before["items_done"]
            assert first["job_state"] not in ("succeeded", "failed", "cancelled")
        rt2.events.unsubscribe(sub)


# ------------------------------------------------------------------ scale


_FABRICATE = """
WITH q AS (SELECT id FROM work_queue WHERE owner_id = :job),
lanes AS (
  SELECT array_agg(id ORDER BY id) AS ids, array_agg(node_id ORDER BY id) AS nodes,
         count(*) AS n
  FROM attempt WHERE job_id = :job AND role = 'lane'),
todo AS (
  SELECT i.id, i.idx FROM work_item i, q
  WHERE i.queue_id = q.id AND i.state = 'pending' AND i.idx >= :from_idx),
-- every 7th chunk had a failed first try (history rows the summary must count)
failed_try AS (
  INSERT INTO work_lease (item_id, seq, holder, site, state, speculative,
                          leased_at, deadline, ended_at)
  SELECT t.id, 1, lanes.ids[1 + t.idx % lanes.n], lanes.nodes[1 + t.idx % lanes.n],
         'failed', false, now() - interval '2 hour' + t.idx * interval '10 ms',
         now() - interval '1 hour', now() - interval '2 hour' + t.idx * interval '11 ms'
  FROM todo t, lanes WHERE t.idx % 7 = 0
  RETURNING item_id),
won AS (
  INSERT INTO work_lease (item_id, seq, holder, site, state, speculative,
                          leased_at, deadline, ended_at)
  SELECT t.id, CASE WHEN t.idx % 7 = 0 THEN 2 ELSE 1 END,
         lanes.ids[1 + (t.idx + 1) % lanes.n], lanes.nodes[1 + (t.idx + 1) % lanes.n],
         'completed', false, now() - interval '1 hour' + t.idx * interval '10 ms',
         now() + interval '1 hour', now() - interval '1 hour' + t.idx * interval '25 ms'
  FROM todo t, lanes
  RETURNING item_id, seq, ended_at)
UPDATE work_item i SET state = 'done', lease_seq = w.seq, done_lease_seq = w.seq,
  attempts = w.seq, result_digest = 'sha256:' || repeat('ab', 32), done_at = w.ended_at
FROM won w WHERE i.id = w.item_id
"""


async def test_a_10000_chunk_job_summary_is_fast_small_and_equals_the_rows(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """The real dispatcher creates the 10 000 chunks; the lanes are frozen; 7 000
    of them are then committed in bulk (with 1 400 failed first tries as
    history) so the read has realistic row counts."""
    gate = asyncio.Event()
    gate.set()
    async with _lanes(runtime, [0.0, 0.0, 0.0], gate=gate):
        job_id = await _submit(runtime, http, tmp_path, items=10_000, chunk=1)
        await _until_done(runtime, job_id, 20)
        gate.clear()
        await asyncio.sleep(1.0)
        await _sql(runtime, _FABRICATE, job=job_id, from_idx=3_000)
        await _sql(
            runtime,
            "UPDATE work_queue SET done_count = (SELECT count(*) FROM work_item i "
            "WHERE i.queue_id = work_queue.id AND i.state = 'done') "
            "WHERE owner_id = :job",
            job=job_id,
        )
        timings: list[float] = []
        peak = 0
        size = 0
        body: dict[str, Any] = {}
        for _ in range(8):
            tracemalloc.start()
            t0 = time.perf_counter()
            r = await http.get(f"/v1/jobs/{job_id}/split", params={"map": "true"})
            timings.append(time.perf_counter() - t0)
            peak = max(peak, tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()
            assert r.status_code == 200, r.text
            body = r.json()
            size = len(r.content)
        _consistent(body, one_item_chunks=True)
        rows = await _sql(
            runtime,
            "SELECT count(*) FILTER (WHERE i.state = 'done') AS done, count(*) AS n "
            "FROM work_item i JOIN work_queue q ON q.id = i.queue_id "
            "WHERE q.owner_id = :job",
            job=job_id,
        )
        assert body["chunks"]["total"] == rows[0].n == 10_000
        assert body["chunks"]["done"] >= 7_000
        assert abs(body["chunks"]["done"] - rows[0].done) <= 6  # lanes were frozen
        assert len(body["chunk_map"]) == 10_000
        failed_rows = await _sql(
            runtime,
            "SELECT count(*) AS n FROM work_lease l JOIN work_item i ON i.id = l.item_id "
            "JOIN work_queue q ON q.id = i.queue_id WHERE q.owner_id = :job "
            "AND l.state = 'failed'",
            job=job_id,
        )
        assert (
            sum(x["failed_tries"] for x in body["lanes"]) == failed_rows[0].n >= 1_000
        )
        # a 500-chunk page at the far end, and the light summary
        t0 = time.perf_counter()
        page = await http.get(
            f"/v1/jobs/{job_id}/chunks", params={"start": 9_500, "limit": 500}
        )
        page_s = time.perf_counter() - t0
        assert page.status_code == 200 and page.json()["count"] == 500
        t0 = time.perf_counter()
        light = await http.get(f"/v1/jobs/{job_id}/split")
        light_s = time.perf_counter() - t0
        timings.sort()
        print(  # noqa: T201
            f"G7-SCALE summary+map: median {timings[len(timings) // 2] * 1000:.0f} ms "
            f"max {timings[-1] * 1000:.0f} ms; size {size} B; tracemalloc peak "
            f"{peak / 1e6:.1f} MB; light {light_s * 1000:.0f} ms {len(light.content)} B; "
            f"chunks(500) {page_s * 1000:.0f} ms"
        )
        assert timings[-1] < 3.0, timings
        assert light_s < 3.0 and page_s < 3.0
        assert size < 64_000, size  # 10 000 map chars + a few lanes
        assert peak < 100_000_000, peak
