"""V0.10 G7 (D270): pure parts of split progress - lane rate, ETA, the event
tracker's coalescing / rate bound / bounds (fake source and clock), chunk rows,
chunk cursors and the event's audience."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Collection, Sequence
from dataclasses import replace

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.states import JobState
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.split_routes import (
    decode_chunk_cursor,
    encode_chunk_cursor,
    progress_out,
)
from meshforge.controller.events.bus import Audience, EventBus, EventType
from meshforge.controller.settings import SplitSettings
from meshforge.controller.split.progress import (
    ChunkCounts,
    CurrentChunk,
    LaneProgress,
    SplitProgress,
    eta_seconds,
    lane_rate,
    payload_items,
)
from meshforge.controller.split.progress_events import ProgressTracker
from meshforge.controller.split.progress_reads import chunk_row
from meshforge.workqueue.core import ItemState, LeaseState
from meshforge.workqueue.reads import ItemView, LeaseView

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)


def at(s: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=s)


# ------------------------------------------------------------------ rate / ETA


def test_lane_rate_is_steady_state_over_the_window() -> None:
    # commits at 10, 12, 14 s of 10 items each: 20 items in 4 s (the first
    # commit's items are the start of the window, not inside it)
    commits = [(at(14), 10), (at(10), 10), (at(12), 10)]
    assert lane_rate(commits, first_lease_at=at(0)) == 5.0


def test_lane_rate_with_one_commit_counts_from_the_first_lease() -> None:
    assert lane_rate([(at(8), 4)], first_lease_at=at(0)) == 0.5
    assert lane_rate([(at(8), 4)], first_lease_at=None) is None


def test_lane_rate_without_measurable_time_is_none() -> None:
    assert lane_rate([], first_lease_at=at(0)) is None
    assert lane_rate([(at(0), 5)], first_lease_at=at(0)) is None
    # two commits at the same instant: fall back to the first-lease span
    assert lane_rate([(at(4), 2), (at(4), 2)], first_lease_at=at(0)) == 1.0


def test_eta_divides_remaining_by_the_live_measured_rates_only() -> None:
    assert eta_seconds(remaining_items=100, live_rates=[2.0, 3.0], queue_open=True) == (
        20.0
    )
    assert eta_seconds(
        remaining_items=100, live_rates=[None, 4.0], queue_open=True
    ) == (25.0)
    # nothing measured: no ETA (never an assumption)
    assert eta_seconds(remaining_items=10, live_rates=[None], queue_open=True) is None
    assert eta_seconds(remaining_items=10, live_rates=[], queue_open=True) is None
    assert eta_seconds(remaining_items=10, live_rates=[0.0], queue_open=True) is None
    # done: 0; closed with work left (failed / cancelled): unknown
    assert eta_seconds(remaining_items=0, live_rates=[], queue_open=False) == 0.0
    assert eta_seconds(remaining_items=5, live_rates=[9.0], queue_open=False) is None


def test_payload_items_is_defensive() -> None:
    assert payload_items({"first": 0, "count": 7}) == 7
    for odd in (None, [], {"count": "7"}, {"count": True}, {"count": -1}, {}):
        assert payload_items(odd) == 0


# ------------------------------------------------------------------ fixtures


def _lane(**kw: object) -> LaneProgress:
    base = LaneProgress(
        attempt_id="att_1",
        node_id="node_1",
        state="running",
        live=True,
        chunks_done=1,
        items_done=10,
        failed_tries=0,
        current=(CurrentChunk(3, 1, False, at(1)),),
        rate_items_s=2.5,
        started_at=at(0),
        first_lease_at=at(0),
        last_commit_at=at(4),
        ended_at=None,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def _progress(job_id: str = "job_1", done: int = 1, **kw: object) -> SplitProgress:
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
        lanes=(_lane(),),
        lanes_total=1,
        rate_items_s=2.5,
        eta_s=36.0,
        started_at=at(0),
        ended_at=None,
        elapsed_s=5.0,
        predicted_s=40.0,
        combine_ms=None,
        warnings=(),
        computed_at=at(5),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


class FakeSource:
    def __init__(self) -> None:
        self.state: dict[str, SplitProgress] = {}
        self.running: list[str] = []
        self.split: set[str] = set()
        self.reads: list[str] = []
        self.kind_lookups: list[list[str]] = []
        self.fail_next = 0

    async def progress(
        self, job_id: str, *, with_map: bool = False
    ) -> SplitProgress | None:
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("database down")
        self.reads.append(job_id)
        return self.state.get(job_id)

    async def running_split_jobs(self, *, limit: int) -> Sequence[str]:
        return self.running[:limit]

    async def split_job_ids(self, job_ids: Collection[str]) -> set[str]:
        self.kind_lookups.append(list(job_ids))
        return {j for j in job_ids if j in self.split}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _tracker(
    source: FakeSource, clock: Clock, **settings: object
) -> tuple[ProgressTracker, list[SplitProgress]]:
    out: list[SplitProgress] = []
    cfg = SplitSettings(progress_event_hz=2.0, progress_resync_s=60.0, **settings)  # type: ignore[arg-type]
    return ProgressTracker(source, publish=out.append, settings=cfg, clock=clock), out


# ------------------------------------------------------------------ tracker


@pytest.mark.asyncio
async def test_first_pass_rebuilds_every_running_split_job_without_touches() -> None:
    source, clock = FakeSource(), Clock()
    source.running = ["job_a", "job_b"]
    source.split = {"job_a", "job_b"}
    source.state = {j: _progress(j) for j in source.running}
    tracker, out = _tracker(source, clock)
    await tracker.step()
    assert sorted(p.job_id for p in out) == ["job_a", "job_b"]
    assert tracker.stats.touches == 0


@pytest.mark.asyncio
async def test_touches_coalesce_and_reads_are_at_most_hz_per_job() -> None:
    source, clock = FakeSource(), Clock()
    source.split = {"job_a"}
    source.state["job_a"] = _progress("job_a", done=1)
    tracker, out = _tracker(source, clock)
    await tracker.step()  # resync: nothing running
    tracker.touch("job_a")
    await tracker.step()
    assert len(out) == 1
    # 1 000 touches within the same 0.5 s: no read, no event
    for done in range(2, 1002):
        source.state["job_a"] = _progress("job_a", done=min(done, 9))
        tracker.touch("job_a")
        clock.now += 0.0004
        wait = await tracker.step()
        assert wait > 0
    assert len(source.reads) == 1 and len(out) == 1
    # ... and the coalesced read happens once the interval passed: the
    # latest state is published, nothing in between
    clock.now += 0.5
    await tracker.step()
    assert len(out) == 2 and out[-1].chunks.done == 9
    assert tracker.stats.touches == 1001


@pytest.mark.asyncio
async def test_an_unchanged_read_publishes_nothing_time_fields_ignored() -> None:
    source, clock = FakeSource(), Clock()
    source.split = {"job_a"}
    source.state["job_a"] = _progress("job_a")
    tracker, out = _tracker(source, clock)
    tracker.touch("job_a")
    await tracker.step()
    source.state["job_a"] = _progress(
        "job_a", computed_at=at(99), elapsed_s=99.0
    )  # only time moved
    clock.now += 1
    tracker.touch("job_a")
    await tracker.step()
    assert len(out) == 1 and tracker.stats.unchanged == 1
    source.state["job_a"] = _progress("job_a", done=2)
    clock.now += 1
    tracker.touch("job_a")
    await tracker.step()
    assert len(out) == 2


@pytest.mark.asyncio
async def test_mode1_jobs_are_looked_up_once_and_ignored() -> None:
    source, clock = FakeSource(), Clock()
    tracker, out = _tracker(source, clock)
    await tracker.step()
    for _ in range(5):
        tracker.touch("job_mode1")
        await tracker.step()
        clock.now += 1
    assert source.kind_lookups == [["job_mode1"]]  # cached as "not split"
    assert source.reads == [] and out == []


@pytest.mark.asyncio
async def test_touch_none_rereads_every_known_unfinished_job() -> None:
    source, clock = FakeSource(), Clock()
    source.split = {"job_a", "job_b"}
    source.running = ["job_a", "job_b"]
    source.state = {
        "job_a": _progress("job_a"),
        "job_b": _progress("job_b", job_state=JobState.SUCCEEDED, queue_state="done"),
    }
    tracker, _ = _tracker(source, clock)
    await tracker.step()
    source.reads.clear()
    clock.now += 1
    tracker.touch(None)  # a visibility timeout released some lease
    await tracker.step()
    assert source.reads == ["job_a"]  # the ended job is not re-read


@pytest.mark.asyncio
async def test_the_dirty_set_is_bounded_and_drops_are_counted() -> None:
    source, clock = FakeSource(), Clock()
    tracker, _ = _tracker(source, clock, progress_max_jobs=2)
    for n in range(20):
        tracker.touch(f"job_{n}")
    assert tracker.stats.dropped_touches == 12  # 4 x progress_max_jobs kept


@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_job_dirty() -> None:
    source, clock = FakeSource(), Clock()
    source.split = {"job_a"}
    source.state["job_a"] = _progress("job_a")
    tracker, out = _tracker(source, clock)
    await tracker.step()
    tracker.touch("job_a")
    source.fail_next = 1
    with pytest.raises(OSError):
        await tracker.step()
    clock.now += 1
    await tracker.step()
    assert len(out) == 1  # read again, published


@pytest.mark.asyncio
async def test_run_survives_errors_and_stops_on_request() -> None:
    source = FakeSource()
    source.split = {"job_a"}
    source.state["job_a"] = _progress("job_a")
    source.fail_next = 1
    out: list[SplitProgress] = []
    tracker = ProgressTracker(
        source,
        publish=out.append,
        settings=SplitSettings(progress_event_hz=20.0, progress_resync_s=0.5),
    )
    task = asyncio.create_task(tracker.run())
    tracker.touch("job_a")
    for _ in range(100):
        if out:
            break
        await asyncio.sleep(0.02)
    assert out and tracker.stats.errors == 1
    tracker.request_stop()
    await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
async def test_a_raising_sink_does_not_stop_the_tracker() -> None:
    source, clock = FakeSource(), Clock()
    source.split = {"job_a"}
    source.state["job_a"] = _progress("job_a")

    def boom(_: SplitProgress) -> None:
        raise RuntimeError("sink")

    tracker = ProgressTracker(
        source, publish=boom, settings=SplitSettings(), clock=clock
    )
    tracker.touch("job_a")
    await tracker.step()
    assert tracker.stats.published == 1


# ------------------------------------------------------------------ shapes


def test_event_data_and_rest_body_are_the_same_numbers() -> None:
    p = _progress(warnings=(("nondeterministic_chunk", 2),))
    data = p.to_data()
    body = progress_out(p).model_dump(mode="json")
    assert body["chunks"] == data["chunks"]
    assert body["lanes"][0]["current"] == data["lanes"][0]["current"]
    assert (
        body["warnings"]
        == data["warnings"]
        == [{"code": "nondeterministic_chunk", "count": 2}]
    )
    assert "chunk_map" not in data and body["chunk_map"] is None


def test_split_progress_events_reach_only_the_owner_and_admins() -> None:
    bus = EventBus()
    event = bus.publish(
        EventType.SPLIT_PROGRESS,
        _progress().to_data(),
        audience=Audience.OWNER,
        owner_user_id="usr_1",
    )
    assert event.visible_to("usr_1", admin=False)
    assert not event.visible_to("usr_2", admin=False)
    assert event.visible_to("usr_2", admin=True)
    assert event.frame()["type"] == "split.progress"


def _view(**kw: object) -> ItemView:
    lease = LeaseView(
        seq=2,
        holder="att_x",
        site="node_x",
        state=LeaseState.LIVE,
        speculative=False,
        leased_at=at(1),
        deadline=at(61),
        ended_at=None,
    )
    base = ItemView(
        id=7,
        idx=3,
        payload={"first": 30, "count": 10},
        state=ItemState.LEASED,
        attempts=1,
        speculations=0,
        result_digest=None,
        done_at=None,
        failed_kind=None,
        failed_sites=("node_y",),
        live=(lease,),
        winner=None,
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def test_chunk_row_names_the_holder_or_the_winner() -> None:
    row = chunk_row(_view())
    assert (row.chunk_index, row.first_item, row.item_count) == (3, 30, 10)
    assert row.node_id == "node_x" and row.attempt_id == "att_x"
    assert row.failed_nodes == ("node_y",) and len(row.leases) == 1
    copy = replace(
        _view().live[0], seq=3, holder="att_z", site="node_z", speculative=True
    )
    # the original holder is named, not the speculative copy
    both = chunk_row(_view(live=(copy, _view().live[0])))
    assert both.node_id == "node_x"
    winner = replace(copy, state=LeaseState.COMPLETED, speculative=True)
    done = chunk_row(
        _view(
            state=ItemState.DONE,
            live=(),
            winner=winner,
            result_digest="sha256:" + "0" * 64,
            done_at=at(9),
        )
    )
    assert done.node_id == "node_z" and done.attempt_id == "att_z"
    assert done.leases == ()
    odd = chunk_row(_view(payload={"first": True}, live=()))
    assert odd.first_item is None and odd.item_count == 0 and odd.node_id is None


def test_chunk_cursor_round_trip_and_refusals() -> None:
    for index in (0, 1, 9_999):
        assert decode_chunk_cursor(encode_chunk_cursor(index)) == index
    for bad in ("", "x" * 41, "!!!", encode_chunk_cursor(10_000), "djF8Y2h1bmt8LTE"):
        with pytest.raises(ApiError):
            decode_chunk_cursor(bad)
