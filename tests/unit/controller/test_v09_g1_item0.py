"""V0.9 G1 item 0 (D226 non-blocking list from the G0 review).

1. The start snapshot copies an explicit LiveSample field allowlist; a sample
   with EVERY field filled serialises as strict JSON.
2. ``clock_offset``: the reported bound rounds up; the remote stamp's
   resolution widens the bound; ``ClockWindow`` keeps only recent exchanges so
   a stale minimum-RTT exchange cannot hide drift.
3. THREAT_MODEL RE-I5: ``GET /v1/clock`` is authenticated, read-only and
   stateless.
4. ``/v1/clock``'s time source is injected through the app state.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
from typing import Any, cast

import pytest

from meshforge.common.clock_offset import (
    ClockExchange,
    ClockOffset,
    ClockOffsetError,
    ClockWindow,
    estimate_offset,
    round_up,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------ 0.1 snapshot


def _full_sample() -> Any:
    """A LiveSample whose every field holds a non-default value."""
    from meshforge.controller.resources import DiskSample, GpuSample, LiveSample

    now = dt.datetime(2026, 10, 17, 12, 0, 0, 654_321, tzinfo=dt.UTC)
    values: dict[str, Any] = {
        "session_epoch": 7,
        "sequence": 42,
        "observed_at": now,
        "received_at": 100.0,
        "cpu_percent": 12.5,
        "ram_used_bytes": 3 * 1024**3,
        "ram_total_bytes": 16 * 1024**3,
        "gpus": (GpuSample(0, 55.0, 2 * 1024**3, 8 * 1024**3),),
        "disks": (DiskSample("/", 10 * 1024**3, 100 * 1024**3),),
        "received_wall": now + dt.timedelta(seconds=1),
    }
    names = {f.name for f in dataclasses.fields(LiveSample)}
    # A new LiveSample field must be given a value here (and a decision in
    # spec.SNAPSHOT_SAMPLE_FIELDS / _EXCLUDED) - the test fails otherwise.
    assert names == set(values), sorted(names ^ set(values))
    return LiveSample(**values)


def test_every_live_sample_field_is_either_copied_or_excluded_on_purpose() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.spec import (
        SNAPSHOT_SAMPLE_EXCLUDED,
        SNAPSHOT_SAMPLE_FIELDS,
    )
    from meshforge.controller.resources import LiveSample

    names = [f.name for f in dataclasses.fields(LiveSample)]
    assert set(SNAPSHOT_SAMPLE_FIELDS).isdisjoint(SNAPSHOT_SAMPLE_EXCLUDED)
    assert sorted(names) == sorted(SNAPSHOT_SAMPLE_FIELDS + SNAPSHOT_SAMPLE_EXCLUDED)


def test_a_snapshot_of_a_sample_with_every_field_filled_is_strict_json() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.spec import SNAPSHOT_SAMPLE_FIELDS, start_snapshot

    sample = _full_sample()
    snapshot = start_snapshot(
        hardware={"capabilities": ["cpu"]},
        hardware_scores={"cpu": 1.0},
        node_state="online",
        rtt_ms=1.25,
        sample=sample,
        monotonic_now=102.5,
        wall_now=dt.datetime(2026, 10, 17, 12, 0, 5, tzinfo=dt.UTC),
        gpu_indices=(0,),
    )
    text = json.dumps(snapshot, allow_nan=False)  # a datetime raises TypeError
    resources = json.loads(text)["resources"]
    assert set(resources) == {*SNAPSHOT_SAMPLE_FIELDS, "age_s"}
    assert resources["observed_at"] == "2026-10-17T12:00:00.654321+00:00"
    assert resources["age_s"] == 2.5
    assert resources["gpus"] == [
        {
            "index": 0,
            "util_percent": 55.0,
            "vram_used_bytes": 2 * 1024**3,
            "vram_total_bytes": 8 * 1024**3,
        }
    ]
    assert resources["disks"] == [
        {"mount": "/", "free_bytes": 10 * 1024**3, "total_bytes": 100 * 1024**3}
    ]


def test_the_allowlist_snapshot_keeps_the_shape_the_old_asdict_one_had() -> None:
    """Readers of attempt.start_snapshot (planner telemetry, V0.9 records)
    see exactly the keys and values the pre-G1 asdict snapshot produced."""
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.jobs.spec import start_snapshot

    sample = _full_sample()
    old = dataclasses.asdict(sample)
    old["observed_at"] = sample.observed_at.isoformat()
    old["age_s"] = 2.5
    del old["received_at"], old["received_wall"]
    new = start_snapshot(
        hardware={},
        hardware_scores=None,
        node_state="online",
        rtt_ms=None,
        sample=sample,
        monotonic_now=102.5,
        wall_now=dt.datetime(2026, 10, 17, tzinfo=dt.UTC),
        gpu_indices=(),
    )["resources"]
    assert json.loads(json.dumps(new)) == json.loads(json.dumps(old))


# ------------------------------------------------------------ 0.2 clock_offset


@pytest.mark.parametrize(
    ("bound", "shown"),
    [(1.0, 1.0), (1.5, 1.5), (1.0004, 1.001), (1.0005, 1.001), (0.0001, 0.001)],
)
def test_the_reported_bound_rounds_up_never_down(bound: float, shown: float) -> None:
    clock = ClockOffset(offset_ms=0.0, bound_ms=bound, rtt_ms=1.0, exchanges=1)
    reported = clock.as_dict()["clock_offset_bound_ms"]
    assert reported == shown
    assert reported >= bound


def test_round_up_keeps_non_finite_values_and_never_goes_below() -> None:
    assert math.isinf(round_up(math.inf, 3))
    assert math.isnan(round_up(math.nan, 3))
    for i in range(2000):
        value = i * 0.000731
        assert round_up(value, 3) >= value
        assert round_up(value, 3) - value < 0.001 + 1e-12


def test_the_remote_resolution_widens_the_bound_and_contains_the_truth() -> None:
    """A Windows agent on Python 3.12 stamps ``time.time()`` in ~15.6 ms
    ticks: a stamp up to one tick early. The bound must cover it."""
    tick = 15.625
    true_offset = 720 * 15.625 - 0.1 - 10_001.0  # remote stamp 15.5 ms early
    sent, received = 10_000.0, 10_002.0  # RTT 2 ms
    true_remote = (sent + 1.0) + true_offset
    stamp = math.floor(true_remote / tick) * tick  # truncated: early
    plain = estimate_offset([ClockExchange(sent, stamp, received)])
    assert abs(plain.offset_ms - true_offset) > plain.bound_ms  # misses
    aware = estimate_offset(
        [ClockExchange(sent, stamp, received)], remote_resolution_ms=tick
    )
    assert abs(aware.offset_ms - true_offset) <= aware.bound_ms
    assert aware.bound_ms == pytest.approx((2.0 + tick) / 2.0)


@pytest.mark.parametrize("tick", [-1.0, math.nan, math.inf])
def test_a_bad_remote_resolution_is_refused(tick: float) -> None:
    with pytest.raises(ClockOffsetError):
        estimate_offset([ClockExchange(0.0, 0.0, 1.0)], remote_resolution_ms=tick)


def test_the_window_keeps_the_last_n_exchanges() -> None:
    window = ClockWindow(max_count=3, max_age_ms=1e9)
    for i in range(5):
        window.add(ClockExchange(i * 10.0, 0.0, i * 10.0 + 1))
    assert [e.sent_ms for e in window.exchanges()] == [20.0, 30.0, 40.0]
    assert len(window) == 3


def test_the_window_drops_exchanges_older_than_t_on_the_local_clock() -> None:
    window = ClockWindow(max_count=100, max_age_ms=60_000.0)
    window.add(ClockExchange(0.0, 0.0, 1.0))
    window.add(ClockExchange(30_000.0, 0.0, 30_001.0))
    window.add(ClockExchange(61_000.0, 0.0, 61_002.0))
    assert [e.sent_ms for e in window.exchanges()] == [30_000.0, 61_000.0]


def test_a_stale_tight_exchange_cannot_hide_drift() -> None:
    """D226: one tight (RTT 0.2 ms) exchange long ago, then the remote clock
    drifted 40 ms while every later exchange had RTT 100 ms. Over ALL
    exchanges the old tight one still overlaps the newer wide ones and wins
    with a tight, wrong bound; the window forgets it."""
    old = ClockExchange(0.0, 0.1 + 0.0, 0.2)  # offset 0, bound 0.1
    later = [
        ClockExchange(t, t + 50.0 + 40.0, t + 100.0)  # offset 40, bound 50
        for t in (600_000.0, 610_000.0, 620_000.0)
    ]
    everything = estimate_offset([old, *later])
    assert everything.consistent and everything.bound_ms < 1.0
    assert abs(everything.offset_ms - 40.0) > everything.bound_ms  # hidden drift
    window = ClockWindow(max_count=32, max_age_ms=300_000.0)
    for exchange in [old, *later]:
        window.add(exchange)
    recent = estimate_offset(window.exchanges())
    assert abs(recent.offset_ms - 40.0) <= recent.bound_ms


def test_a_local_clock_that_went_back_clears_the_window() -> None:
    window = ClockWindow(max_count=10, max_age_ms=1e9)
    window.add(ClockExchange(1000.0, 0.0, 1001.0))
    window.add(ClockExchange(500.0, 0.0, 501.0))
    assert [e.sent_ms for e in window.exchanges()] == [500.0]


@pytest.mark.parametrize(
    ("count", "age"), [(0, 1.0), (1, 0.0), (1, -1.0), (1, math.inf), (1, math.nan)]
)
def test_window_limits_are_validated(count: int, age: float) -> None:
    with pytest.raises(ValueError):
        ClockWindow(max_count=count, max_age_ms=age)


# ------------------------------------------- 0.3 / 0.4 the /v1/clock route


class _M:
    def get(self, node_id: str) -> None:
        return None

    def __len__(self) -> int:
        return 0


class _R:
    async def list_nodes(self, **_: object) -> list[object]:
        return []


class _T:
    def ring(self, node_id: str) -> None:
        return None


class _CountingClock:
    def __init__(self, start: float) -> None:
        self.now = start
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        self.now += 0.25
        return self.now


def _client(wall_clock: Any, headers: dict[str, str] | None = None) -> Any:
    pytest.importorskip("fastapi", reason="needs the controller extra")
    import httpx
    from mf_users import static_auth

    from meshforge.controller.http import create_http_app

    app = create_http_app(
        cast("Any", _M()),
        auth=static_auth(None),
        registry=cast("Any", _R()),
        resources=cast("Any", _T()),
        clock=lambda: 1.0,
        wall_clock=wall_clock,
        web_dist=None,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://api.test",
        headers=headers or {},
    )


@pytest.mark.asyncio
async def test_the_clock_source_is_the_one_injected_into_the_app() -> None:
    from mf_users import ADMIN_TOKEN, bearer

    first, second = _CountingClock(1_000.0), _CountingClock(5_000.0)
    async with (
        _client(first, bearer(ADMIN_TOKEN)) as a,
        _client(second, bearer(ADMIN_TOKEN)) as b,
    ):
        got_a = (await a.get("/v1/clock")).json()["server_time"]
        got_b = (await b.get("/v1/clock")).json()["server_time"]
    # Two apps in one process, each with its own source (no module global).
    assert (got_a, got_b) == (1000.25, 5000.25)


def test_routes_has_no_module_level_clock_any_more() -> None:
    pytest.importorskip("fastapi", reason="needs the controller extra")
    from meshforge.controller.api import routes

    assert not hasattr(routes, "wall_clock")


@pytest.mark.security
@pytest.mark.asyncio
async def test_clock_without_a_user_is_401_and_never_reads_the_clock() -> None:
    """RE-I5: the clock is only disclosed to an authenticated user."""
    clock = _CountingClock(1_000.0)
    async with _client(clock) as c:
        r = await c.get("/v1/clock")
    assert r.status_code == 401
    assert "server_time" not in r.text
    assert clock.calls == 0


@pytest.mark.security
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_clock_is_read_only(method: str) -> None:
    """RE-I5: no verb but GET; a mutating verb never reaches the clock."""
    from mf_users import ADMIN_TOKEN, bearer

    clock = _CountingClock(1_000.0)
    async with _client(clock, bearer(ADMIN_TOKEN)) as c:
        r = await c.request(method, "/v1/clock")
    assert r.status_code in {403, 405}
    assert clock.calls == 0


@pytest.mark.security
@pytest.mark.asyncio
async def test_clock_is_stateless_one_read_per_request_and_not_cacheable() -> None:
    """RE-I5: each answer reads the injected wall clock exactly once and
    nothing else (no registry, no session, no DB), and is no-store."""
    from mf_users import ADMIN_TOKEN, bearer

    clock = _CountingClock(1_000.0)
    async with _client(clock, bearer(ADMIN_TOKEN)) as c:
        answers = [await c.get("/v1/clock") for _ in range(50)]
    assert all(a.status_code == 200 for a in answers)
    assert all("no-store" in a.headers["cache-control"] for a in answers)
    assert clock.calls == 50
    assert [a.json()["server_time"] for a in answers][-1] == 1_000.0 + 50 * 0.25
