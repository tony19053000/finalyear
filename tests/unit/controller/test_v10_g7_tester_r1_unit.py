"""V0.10 G7 tester r1 (D270): randomised attacks on the pure parts of split
progress - the tracker's rate bound / final-state delivery under bursty touches
(fake source and clock), rate / ETA never negative / NaN / infinite, the event's
size (the bus-ring concern), chunk cursor fuzzing."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import random
from collections.abc import Collection, Sequence
from dataclasses import replace
from itertools import pairwise

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.states import JobState
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.split_routes import (
    decode_chunk_cursor,
    encode_chunk_cursor,
)
from meshforge.controller.events.bus import DEFAULT_RING_SIZE
from meshforge.controller.settings import SplitSettings
from meshforge.controller.split.progress import (
    MAX_LANES,
    ChunkCounts,
    CurrentChunk,
    LaneProgress,
    SplitProgress,
    eta_seconds,
    lane_rate,
)
from meshforge.controller.split.progress_events import ProgressTracker

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)


def at(s: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=s)


def _lane(n: int, *, current: int = 2) -> LaneProgress:
    return LaneProgress(
        attempt_id=f"att_01J00000000000000000{n:06d}",
        node_id=f"node_01J00000000000000000{n:06d}",
        state="running",
        live=True,
        chunks_done=123456,
        items_done=1234567,
        failed_tries=12,
        current=tuple(CurrentChunk(9000 + k, 5, False, at(1)) for k in range(current)),
        rate_items_s=1234.567891,
        started_at=at(0),
        first_lease_at=at(0),
        last_commit_at=at(4),
        ended_at=None,
    )


def _progress(
    job_id: str = "job_01J0000000000000000000AAAA",
    done: int = 1,
    lanes: int = 3,
    **kw: object,
) -> SplitProgress:
    base = SplitProgress(
        job_id=job_id,
        owner_user_id="usr_1",
        job_state=JobState.RUNNING,
        queue_state="open",
        items_total=100,
        chunk_items=10,
        chunks=ChunkCounts(total=10, pending=9 - done, leased=1, done=done, failed=0),
        items_done=10 * done,
        live_leases=1,
        speculative_live=0,
        speculated=0,
        lanes=tuple(_lane(n) for n in range(lanes)),
        lanes_total=lanes,
        rate_items_s=2.5,
        eta_s=36.0,
        started_at=at(0),
        ended_at=None,
        elapsed_s=5.0,
        predicted_s=40.0,
        combine_ms=None,
        warnings=(("nondeterministic_chunk", 3),),
        computed_at=at(5),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------------ tracker


class _Source:
    def __init__(self, jobs: Sequence[str]) -> None:
        self.state: dict[str, SplitProgress] = {}
        self.jobs = list(jobs)
        self.reads = 0

    async def progress(self, job_id: str, *, with_map: bool = False) -> SplitProgress:
        self.reads += 1
        return self.state[job_id]

    async def running_split_jobs(self, *, limit: int) -> Sequence[str]:
        return [
            j for j in self.jobs if self.state[j].job_state is not JobState.SUCCEEDED
        ][:limit]

    async def split_job_ids(self, job_ids: Collection[str]) -> set[str]:
        return {j for j in job_ids if j in self.state}


class _Clock:
    now = 5000.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.asyncio
async def test_random_bursts_keep_the_rate_bound_and_the_last_state(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    jobs = [f"job_{i}" for i in range(rng.randint(1, 4))]
    source, clock = _Source(jobs), _Clock()
    published: list[tuple[float, SplitProgress]] = []
    hz = rng.choice([1.0, 2.0, 5.0])
    cfg = SplitSettings(progress_event_hz=hz, progress_resync_s=rng.choice([0.7, 3.0]))
    tracker = ProgressTracker(
        source,
        publish=lambda p: published.append((clock.now, p)),
        settings=cfg,
        clock=clock,
    )
    for j in jobs:
        source.state[j] = _progress(j, done=0, items_done=0)
    for _ in range(rng.randint(200, 600)):
        for j in jobs:
            cur = source.state[j]
            if cur.job_state is JobState.SUCCEEDED or rng.random() < 0.5:
                continue
            done = min(10, cur.chunks.done + rng.randint(0, 3))
            ended = done == 10
            source.state[j] = _progress(
                j,
                done=min(done, 9) if not ended else 9,
                chunks=ChunkCounts(
                    10, 0 if ended else 9 - done, 0 if ended else 1, done, 0
                )
                if ended
                else ChunkCounts(10, 9 - done, 1, done, 0),
                items_done=done * 10,
                job_state=JobState.SUCCEEDED if ended else JobState.RUNNING,
            )
            if rng.random() < 0.8:
                tracker.touch(j)
        if rng.random() < 0.05:
            tracker.touch(None)
        clock.now += rng.choice([0.0, 0.001, 0.01, 0.1, 0.3, 1.0])
        await tracker.step()
    # quiesce: a bounded number of passes finishes every pending read
    for _ in range(4):
        clock.now += 1.0 / hz + 0.01
        for j in jobs:
            tracker.touch(j)
        await tracker.step()
    clock.now += 1.0 / hz + 0.01
    await tracker.step()
    for j in jobs:
        mine = [(t, p) for t, p in published if p.job_id == j]
        # rate bound: publishes of one job are at least 1/hz apart
        for (t1, _), (t2, _) in pairwise(mine):
            assert t2 - t1 >= 1.0 / hz - 1e-9, (seed, j, t2 - t1)
        # never the same progress twice in a row; done never goes backwards
        for (_, a), (_, b) in pairwise(mine):
            assert a.comparable() != b.comparable()
            assert b.chunks.done >= a.chunks.done
        # the last published state is the source's last state (final delivery)
        assert mine, (seed, j)
        assert mine[-1][1].comparable() == source.state[j].comparable(), (seed, j)


@pytest.mark.asyncio
async def test_a_thousand_jobs_burst_is_read_in_bounded_passes() -> None:
    n = 1000
    jobs = [f"job_{i}" for i in range(n)]
    source, clock = _Source(jobs), _Clock()
    out: list[SplitProgress] = []
    for j in jobs:
        source.state[j] = _progress(j)
    tracker = ProgressTracker(
        source,
        publish=out.append,
        settings=SplitSettings(progress_event_hz=2.0, progress_resync_s=60.0),
        clock=clock,
    )
    for j in jobs:
        tracker.touch(j)
    await tracker.step()
    assert source.reads <= 64, source.reads  # MAX_READS_PER_STEP
    for _ in range(20):
        await tracker.step()
    assert len(out) == n  # every job eventually read, once
    assert len({p.job_id for p in out}) == n


@pytest.mark.asyncio
async def test_run_loop_never_loses_the_final_state_with_a_slow_sink() -> None:
    """The real `run()` loop on real time: a sink that raises once, a burst of
    touches, then silence - the last state still arrives."""
    source = _Source(["job_a"])
    source.state["job_a"] = _progress("job_a", done=0)
    got: list[SplitProgress] = []
    calls = 0

    def sink(p: SplitProgress) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("sink broke")
        got.append(p)

    tracker = ProgressTracker(
        source,
        publish=sink,
        settings=SplitSettings(progress_event_hz=20.0, progress_resync_s=0.5),
    )
    task = asyncio.create_task(tracker.run())
    try:
        for d in range(1, 10):
            source.state["job_a"] = _progress("job_a", done=d)
            tracker.touch("job_a")
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.8)
    finally:
        tracker.request_stop()
        await asyncio.wait_for(task, 5)
    assert got and got[-1].chunks.done == 9


# ------------------------------------------------------------------ rate / ETA


@pytest.mark.parametrize("seed", range(20))
def test_rate_and_eta_are_never_negative_nan_or_infinite(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    for _ in range(500):
        n = rng.randint(0, 9)
        commits = [
            (at(rng.choice([0, 0, 1, rng.uniform(-5, 50)])), rng.randint(0, 50))
            for _ in range(n)
        ]
        first = rng.choice([None, at(0), at(rng.uniform(-3, 60))])
        rate = lane_rate(commits, first_lease_at=first)
        assert rate is None or (math.isfinite(rate) and rate >= 0), (commits, first)
        rates = [rate, rng.choice([None, 0.0, 1e-9, 1e9, 3.5])]
        eta = eta_seconds(
            remaining_items=rng.randint(-3, 10_000),
            live_rates=rates,
            queue_open=rng.random() < 0.7,
        )
        assert eta is None or (math.isfinite(eta) and eta >= 0), (rates, eta)


def test_a_lane_rate_of_zero_items_is_not_reported_as_a_rate_to_eta() -> None:
    # commits whose items sum to 0 (odd payload) -> rate 0.0 -> no ETA claimed
    rate = lane_rate([(at(1), 0), (at(3), 0)], first_lease_at=at(0))
    assert eta_seconds(remaining_items=10, live_rates=[rate], queue_open=True) is None


# ------------------------------------------------------------------ event size


def test_event_size_with_many_lanes_for_the_bus_ring_judgement() -> None:
    """The bus ring holds 10 000 events (count, not bytes). Record what one
    event weighs: a realistic 4-lane job and the 64-lane cap."""
    four = len(json.dumps(_progress(lanes=4).to_data()).encode())
    cap = len(json.dumps(_progress(lanes=MAX_LANES).to_data()).encode())
    assert four < 8_000, four
    assert cap < 80_000, cap
    # worst case retained by the ring if every slot were one 64-lane event
    assert DEFAULT_RING_SIZE * cap < 800_000_000
    # the numbers the report quotes (visible with -s)
    print(  # noqa: T201
        f"G7-EVENT-SIZE 4 lanes={four} B, 64 lanes={cap} B, ring worst "
        f"{DEFAULT_RING_SIZE * cap / 1e6:.0f} MB, ring typical "
        f"{DEFAULT_RING_SIZE * four / 1e6:.0f} MB"
    )


def test_event_data_is_strict_json_for_odd_floats() -> None:
    p = _progress(eta_s=0.0, rate_items_s=None)
    json.dumps(p.to_data(), allow_nan=False)


# ------------------------------------------------------------------ cursors


def test_cursor_round_trip_and_fuzz() -> None:
    for i in (0, 1, 9_999):
        assert decode_chunk_cursor(encode_chunk_cursor(i)) == i
    rng = random.Random(7)  # noqa: S311
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_=+/ é٠"
    for _ in range(3000):
        junk = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 45)))
        try:
            idx = decode_chunk_cursor(junk)
        except ApiError:
            continue
        assert 0 <= idx < 10_000  # anything accepted is a sane index
    import base64

    for raw in (
        b"v1|chunk|\xd9\xa0",  # Arabic-indic digit zero
        b"v1|chunk|1_0",
        b"v1|chunk|+5",
        b"v1|chunk| 5",
        b"v1|chunk|0x10",
        b"v1|chunk|10000",
        b"v1|chunk|00000000",
        b"v1|chunk|-1",
        b"v1|chunk|5\n",
    ):
        cur = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        with pytest.raises(ApiError):
            decode_chunk_cursor(cur)


def test_split_progress_events_never_reach_another_user_even_for_an_unowned_job() -> (
    None
):
    from meshforge.controller.events.bus import Audience, EventBus, EventType

    bus = EventBus()
    sub_a, _ = bus.subscribe()
    for owner in ("usr_alice", None):
        bus.publish(
            EventType.SPLIT_PROGRESS,
            _progress().to_data(),
            audience=Audience.OWNER,
            owner_user_id=owner,
        )
    events = [sub_a.queue.get_nowait() for _ in range(2)]
    alice_sees = [e.visible_to("usr_alice", admin=False) for e in events]
    mallory_sees = [e.visible_to("usr_mallory", admin=False) for e in events]
    anonymous = [e.visible_to(None, admin=False) for e in events]
    admin = [e.visible_to("usr_root", admin=True) for e in events]
    assert alice_sees == [True, False]
    assert mallory_sees == [False, False]
    assert anonymous == [False, False]  # an unowned job matches no None user
    assert admin == [True, True]
