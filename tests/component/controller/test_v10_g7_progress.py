"""V0.10 G7 (0.10.8; D270, checklist 37): a split job's progress over a real
controller (PostgreSQL, gRPC sessions, HTTPS API, event bus) with fake lanes
(``mf_fake_lanes``).

* **numbers == rows**: with the lanes frozen mid-run (their results held by a
  gate) the REST summary, the chunk map, every chunk page and the last
  ``split.progress`` event equal an independent SQL count of ``work_item`` /
  ``work_lease``; again at the end;
* **event rate bound**: 300 one-item chunks run as fast as the fakes go - the
  job's ``split.progress`` events are at least ``1 / progress_event_hz`` apart,
  far fewer than the chunk messages, and the last one is the final state;
* **restart rebuild**: a brand-new tracker (= a restarted controller's empty
  memory) on the same database publishes the running job's exact rows on its
  first pass, with no touch at all;
* **REST**: owner-only like every job read (another user: 404), a Mode 1 job is
  404 ``split job``, keyset pages walk every chunk once, ``state`` / ``node``
  filters equal SQL, bad cursor / node -> 400.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Sequence
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
from meshforge.controller.settings import LivenessSettings, SplitSettings
from meshforge.controller.split.progress import SplitProgress
from meshforge.controller.split.progress_events import ProgressTracker
from meshforge.controller.split.progress_reads import ProgressReads
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

GIB = 1024**3
ENTRY = "python main.py {items_file} {out}"
HB_S = 0.2
HZ = 2.0


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        split={
            "visibility_timeout_s": 120.0,  # frozen lanes keep their leases
            "wait_retry_s": 0.2,
            "progress_event_hz": HZ,
            "progress_resync_s": 1.0,
        },
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
) -> str:
    built = _tree(tmp_path / name)
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    got = await http.post(
        "/v1/jobs",
        json={
            "manifest": {
                "name": "s",
                "entrypoint": ENTRY,
                "split": {
                    "over": {"range": {"stop": items}},
                    "chunk": {"items": chunk},
                },
            },
            "tree_id": str(built.tree.tree_id),
            "placement": {},
        },
    )
    assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


async def _sql(runtime: ControllerRuntime, query: str, **params: Any) -> list[Any]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        return list((await conn.execute(text(query), params)).all())


async def _wait_job(runtime: ControllerRuntime, job_id: str) -> Any:
    assert runtime.jobs is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120
    while loop.time() < deadline:
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not end")


# ------------------------------------------------------------------ the oracle

_ITEMS_SQL = """
SELECT i.idx, i.state, (i.payload->>'count')::int AS n,
  (SELECT l.holder FROM work_lease l WHERE l.item_id = i.id
     AND l.seq = i.done_lease_seq) AS winner,
  (SELECT l.site FROM work_lease l WHERE l.item_id = i.id
     AND l.seq = i.done_lease_seq) AS winner_site
FROM work_item i JOIN work_queue q ON q.id = i.queue_id
WHERE q.owner_id = :job ORDER BY i.idx
"""
_LIVE_SQL = """
SELECT l.holder, i.idx, l.speculative FROM work_lease l
JOIN work_item i ON i.id = l.item_id JOIN work_queue q ON q.id = i.queue_id
WHERE q.owner_id = :job AND l.state = 'live' ORDER BY i.idx, l.seq
"""
_FAILED_SQL = """
SELECT l.holder, count(*) AS n FROM work_lease l
JOIN work_item i ON i.id = l.item_id JOIN work_queue q ON q.id = i.queue_id
WHERE q.owner_id = :job AND l.state = 'failed' GROUP BY l.holder
"""


async def _oracle(runtime: ControllerRuntime, job_id: str) -> dict[str, Any]:
    """The expected progress numbers, counted independently from the rows."""
    items = await _sql(runtime, _ITEMS_SQL, job=job_id)
    live = await _sql(runtime, _LIVE_SQL, job=job_id)
    failed = {r.holder: r.n for r in await _sql(runtime, _FAILED_SQL, job=job_id)}
    by_state: dict[str, int] = {"pending": 0, "leased": 0, "done": 0, "failed": 0}
    lanes: dict[str, dict[str, Any]] = {}
    for r in items:
        by_state[r.state] += 1
        if r.state == "done":
            lane = lanes.setdefault(
                r.winner, {"chunks_done": 0, "items_done": 0, "current": []}
            )
            lane["chunks_done"] += 1
            lane["items_done"] += r.n
    for r in live:
        lanes.setdefault(r.holder, {"chunks_done": 0, "items_done": 0, "current": []})[
            "current"
        ].append(r.idx)
    speculative = {r.idx for r in live if r.speculative}
    chars = {"pending": "p", "leased": "l", "done": "d", "failed": "f"}
    chunk_map = "".join(
        "s" if r.state == "leased" and r.idx in speculative else chars[r.state]
        for r in items
    )
    return {
        "chunks": {"total": len(items), **by_state},
        "items_done": sum(r.n for r in items if r.state == "done"),
        "live_leases": len(live),
        "speculative_live": len(speculative),
        "lanes": lanes,
        "failed": failed,
        "chunk_map": chunk_map,
        "done_sites": {r.idx: r.winner_site for r in items if r.state == "done"},
    }


def _compare(summary: dict[str, Any], want: dict[str, Any]) -> None:
    """The shown numbers equal the rows (checklist 37)."""
    assert summary["chunks"] == want["chunks"]
    assert summary["items_done"] == want["items_done"]
    assert summary["live_leases"] == want["live_leases"]
    assert summary["speculative_live"] == want["speculative_live"]
    shown = {lane["attempt_id"]: lane for lane in summary["lanes"]}
    for holder, expected in want["lanes"].items():
        lane = shown[holder]
        assert lane["chunks_done"] == expected["chunks_done"], holder
        assert lane["items_done"] == expected["items_done"], holder
        assert sorted(c["chunk_index"] for c in lane["current"]) == sorted(
            expected["current"]
        )
        assert lane["failed_tries"] == want["failed"].get(holder, 0)
    for holder, lane in shown.items():
        if holder not in want["lanes"]:
            assert lane["chunks_done"] == 0 and lane["current"] == []
    assert (
        sum(lane["chunks_done"] for lane in summary["lanes"])
        == (want["chunks"]["done"])
    )


async def _stable(runtime: ControllerRuntime, job_id: str) -> dict[str, Any]:
    """Wait until the rows stop changing (the gate holds every result)."""
    last = None
    for _ in range(200):
        now = await _oracle(runtime, job_id)
        if now == last:
            return now
        last = now
        await asyncio.sleep(0.3)
    raise AssertionError("the job's rows never settled")


def _events(sub: Subscriber, job_id: str) -> list[BusEvent]:
    out = []
    while not sub.queue.empty():
        event = sub.queue.get_nowait()
        if event.type is EventType.SPLIT_PROGRESS and event.data["job_id"] == job_id:
            out.append(event)
    return out


async def _latest_event(
    sub: Subscriber, job_id: str, seen: list[BusEvent], want: dict[str, Any]
) -> dict[str, Any]:
    """The tracker's newest event once it reflects ``want`` (within the resync)."""
    for _ in range(100):
        seen.extend(_events(sub, job_id))
        if seen and seen[-1].data["chunks"] == want["chunks"]:
            return dict(seen[-1].data)
        await asyncio.sleep(0.1)
    raise AssertionError(f"no split.progress event matched the rows: {seen[-1:]}")


# ------------------------------------------------------------------ tests


async def test_shown_numbers_equal_the_rows_mid_run_and_at_the_end(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.events is not None
    sub, _ = runtime.events.subscribe()
    gate = asyncio.Event()
    gate.set()
    async with _lanes(runtime, [0.01, 0.03, 0.03], gate=gate):
        job_id = await _submit(runtime, http, tmp_path, items=600, chunk=10)
        for _ in range(400):
            done = await _sql(
                runtime,
                "SELECT count(*) AS n FROM work_item i JOIN work_queue q "
                "ON q.id = i.queue_id WHERE q.owner_id = :job AND i.state = 'done'",
                job=job_id,
            )
            if done and done[0].n >= 20:
                break
            await asyncio.sleep(0.02)
        gate.clear()  # freeze: every lane's next result waits
        want = await _stable(runtime, job_id)
        assert 20 <= want["chunks"]["done"] < 60
        assert want["live_leases"] >= 1

        got = await http.get(f"/v1/jobs/{job_id}/split", params={"map": "true"})
        assert got.status_code == 200, got.text
        summary = got.json()
        _compare(summary, want)
        assert summary["chunk_map"] == want["chunk_map"]
        assert summary["job_state"] == "running"
        assert summary["queue_state"] == "open"
        assert summary["items_total"] == 600 and summary["chunk_items"] == 10
        assert summary["started_at"] is not None and summary["elapsed_s"] >= 0
        # ETA = remaining / the live lanes' measured rates (never assumed)
        rates = [
            lane["rate_items_s"]
            for lane in summary["lanes"]
            if lane["live"] and lane["rate_items_s"] is not None
        ]
        assert rates, summary["lanes"]
        assert summary["rate_items_s"] == pytest.approx(sum(rates), rel=1e-6)
        assert summary["eta_s"] == pytest.approx(
            (600 - want["items_done"]) / sum(rates), rel=1e-3
        )

        # every chunk page together equals the rows too
        pages, cursor = [], None
        while True:
            params: dict[str, Any] = {"limit": 7}
            if cursor:
                params["cursor"] = cursor
            page = (await http.get(f"/v1/jobs/{job_id}/chunks", params=params)).json()
            pages.extend(page["chunks"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert [c["chunk_index"] for c in pages] == list(range(60))
        assert "".join(c["state"][0] for c in pages) == want["chunk_map"].replace(
            "s", "l"
        )
        assert sum(c["item_count"] for c in pages) == 600
        for c in pages:
            if c["state"] == "done":
                assert c["node_id"] == want["done_sites"][c["chunk_index"]]

        # the latest event says the same as the rows
        seen: list[BusEvent] = []
        event = await _latest_event(sub, job_id, seen, want)
        _compare(event, want)
        assert "chunk_map" not in event

        gate.set()
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        final = await _oracle(runtime, job_id)
        got = (await http.get(f"/v1/jobs/{job_id}/split")).json()
        _compare(got, final)
        assert got["chunks"]["done"] == 60 and got["items_done"] == 600
        assert got["job_state"] == "succeeded" and got["eta_s"] == 0.0
        assert got["ended_at"] is not None and got["queue_state"] == "done"
        for _ in range(100):
            seen.extend(_events(sub, job_id))
            if seen[-1].data["job_state"] == "succeeded":
                break
            await asyncio.sleep(0.1)
        assert seen[-1].data["job_state"] == "succeeded"
        _compare(dict(seen[-1].data), final)
    runtime.events.unsubscribe(sub)


async def test_events_are_rate_bounded_and_the_last_one_is_final(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.events is not None and runtime.split_progress is not None
    sub, _ = runtime.events.subscribe()
    async with _lanes(runtime, [0.0, 0.0, 0.0]):
        job_id = await _submit(runtime, http, tmp_path, items=300, chunk=1)
        job = await _wait_job(runtime, job_id)
        assert job.state is JobState.SUCCEEDED
        seen: list[BusEvent] = []
        for _ in range(100):
            seen.extend(_events(sub, job_id))
            if seen and seen[-1].data["job_state"] == "succeeded":
                break
            await asyncio.sleep(0.1)
    assert seen[-1].data["job_state"] == "succeeded"
    assert seen[-1].data["chunks"]["done"] == 300
    assert seen[-1].data["items_done"] == 300
    gaps = [b.at - a.at for a, b in itertools.pairwise(seen)]
    # one read (and so at most one event) per 1/hz per job; `at` is rounded
    # to the millisecond
    assert all(g >= 1 / HZ - 0.002 for g in gaps), gaps
    assert runtime.dispatcher is not None
    chunk_messages = runtime.dispatcher.split.stats.requests + 300
    assert len(seen) < chunk_messages / 10, (len(seen), chunk_messages)
    assert runtime.split_progress.stats.touches >= chunk_messages
    # the chunks really were committed one by one: what the events coalesced
    dones = [e.data["chunks"]["done"] for e in seen]
    assert dones == sorted(dones)  # never backwards
    runtime.events.unsubscribe(sub)


async def test_a_fresh_tracker_rebuilds_from_the_rows_without_any_touch(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    gate = asyncio.Event()
    gate.set()
    async with _lanes(runtime, [0.01, 0.02], gate=gate):
        job_id = await _submit(runtime, http, tmp_path, items=200, chunk=5)
        for _ in range(400):
            got = (await http.get(f"/v1/jobs/{job_id}/split")).json()
            if got["chunks"]["done"] >= 10:
                break
            await asyncio.sleep(0.02)
        gate.clear()
        want = await _stable(runtime, job_id)
        # a restarted controller: empty memory, same database
        assert runtime._engine is not None
        published: list[SplitProgress] = []
        fresh = ProgressTracker(
            ProgressReads(runtime._engine),
            publish=published.append,
            settings=SplitSettings(progress_event_hz=HZ),
        )
        await fresh.step()
        mine = [p for p in published if p.job_id == job_id]
        assert len(mine) == 1, published
        _compare(mine[0].to_data(), want)
        assert fresh.stats.touches == 0 and fresh.stats.resyncs == 1
        # a second pass with no change publishes nothing
        fresh.touch(job_id)
        await asyncio.sleep(1 / HZ)
        await fresh.step()
        assert len([p for p in published if p.job_id == job_id]) == 1
        assert fresh.stats.unchanged >= 1
        gate.set()
        assert (await _wait_job(runtime, job_id)).state is JobState.SUCCEEDED


async def test_rest_access_paging_and_filters(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime._engine is not None
    await create_user(runtime._engine, "alice", "alice-password-123")
    await create_user(runtime._engine, "mallory", "mallory-password-1")
    async with api_client(runtime) as anon:
        alice = await login(anon, "alice", "alice-password-123")
        mallory = await login(anon, "mallory", "mallory-password-1")
        no_auth = await anon.get("/v1/jobs/job_01J0000000000000000000000/split")
        assert no_auth.status_code == 401
    async with (
        api_client(runtime, token=alice) as as_alice,
        api_client(runtime, token=mallory) as as_mallory,
        _lanes(runtime, [0.0, 0.0]) as lanes,
    ):
        job_id = await _submit(runtime, as_alice, tmp_path, items=95, chunk=10)
        assert (await _wait_job(runtime, job_id)).state is JobState.SUCCEEDED
        for path in ("split", "chunks"):
            hidden = await as_mallory.get(f"/v1/jobs/{job_id}/{path}")
            assert hidden.status_code == 404, hidden.text
            assert hidden.json()["error"]["message"] == "no such job"
            mine = await as_alice.get(f"/v1/jobs/{job_id}/{path}")
            assert mine.status_code == 200, mine.text
            admin = await http.get(f"/v1/jobs/{job_id}/{path}")
            assert admin.status_code == 200
        summary = (await as_alice.get(f"/v1/jobs/{job_id}/split")).json()
        assert summary["chunks"]["total"] == 10 and summary["items_done"] == 95
        # node filter == SQL: the chunks each node's result was committed for
        rows = await _oracle(runtime, job_id)
        for lane in lanes:
            node = lane.agent.node_id
            page = (
                await as_alice.get(
                    f"/v1/jobs/{job_id}/chunks", params={"node": node, "limit": 500}
                )
            ).json()
            assert [c["chunk_index"] for c in page["chunks"]] == sorted(
                i for i, site in rows["done_sites"].items() if site == node
            )
        only = (
            await as_alice.get(
                f"/v1/jobs/{job_id}/chunks", params={"state": ["pending", "failed"]}
            )
        ).json()
        assert only["chunks"] == [] and only["next_cursor"] is None
        last = (
            await as_alice.get(f"/v1/jobs/{job_id}/chunks", params={"limit": 500})
        ).json()["chunks"][-1]
        assert last["item_count"] == 5 and last["first_item"] == 90
        one = (
            await as_alice.get(
                f"/v1/jobs/{job_id}/chunks", params={"start": 4, "limit": 1}
            )
        ).json()
        assert [c["chunk_index"] for c in one["chunks"]] == [4]
        assert one["next_cursor"] is not None
        both = await as_alice.get(
            f"/v1/jobs/{job_id}/chunks",
            params={"start": 1, "cursor": one["next_cursor"]},
        )
        assert both.status_code == 400
        for bad in (
            {"cursor": "nope"},
            {"cursor": "djF8Y2h1bmt8LTE"},  # v1|chunk|-1
            {"node": "not-a-node"},
            {"limit": 0},
            {"limit": 501},
            {"state": "sleeping"},
        ):
            refused = await as_alice.get(f"/v1/jobs/{job_id}/chunks", params=bad)
            assert refused.status_code in (400, 422), (bad, refused.text)
        bad_id = await as_alice.get("/v1/jobs/not-a-job/split")
        assert bad_id.status_code == 400
    # a Mode 1 job has no split progress
    built = _tree(tmp_path / "whole")
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    whole = await http.post(
        "/v1/jobs",
        json={
            "manifest": {"name": "w", "entrypoint": "python main.py"},
            "tree_id": str(built.tree.tree_id),
            "placement": {},
        },
    )
    assert whole.status_code == 201, whole.text
    whole_id = whole.json()["job_id"]
    for path in ("split", "chunks"):
        got = await http.get(f"/v1/jobs/{whole_id}/{path}")
        assert got.status_code == 404
        assert got.json()["error"]["message"] == "no such split job"
    # a split job not dispatched yet: zero counts, no queue, empty pages
    queued = await _submit(runtime, http, tmp_path, items=10, chunk=5, name="q")
    early = (await http.get(f"/v1/jobs/{queued}/split", params={"map": "1"})).json()
    assert early["queue_state"] is None and early["chunks"]["total"] == 0
    assert early["chunk_map"] == "" and early["eta_s"] is None
    empty = (await http.get(f"/v1/jobs/{queued}/chunks")).json()
    assert empty == {
        "job_id": queued,
        "chunks": [],
        "count": 0,
        "next_cursor": None,
    }
    assert json.dumps(early)  # plain JSON
