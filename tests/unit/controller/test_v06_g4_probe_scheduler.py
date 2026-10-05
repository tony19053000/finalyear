"""V0.6 G4 (D111): every probe bound of :class:`ProbeScheduler`, with an
injected clock and a seeded RNG - interval per ordered pair, one probe per node
at a time, the fabric-wide byte budget, busy nodes (GPU job, CPU > 80 %), the
jittered stagger, timeouts, node loss; plus EWMA and staleness arithmetic."""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import replace

import pytest

from meshforge.controller.network.links import age_s, ewma, is_stale
from meshforge.controller.network.scheduler import (
    BUDGET_WINDOW_S,
    ProbeConfig,
    ProbeNode,
    ProbeScheduler,
)

MIB = 1024 * 1024
CFG = ProbeConfig(
    interval_s=900.0,
    gap_s=10.0,
    jitter=0.5,
    budget_bytes_per_min=48 * MIB,
    probe_bytes=4 * MIB,
    probe_max_bytes=8 * MIB,
    timeout_s=60.0,
    busy_cpu_percent=80.0,
)


def nodes(*ids: str, **kw: object) -> list[ProbeNode]:
    return [ProbeNode(i, f"10.0.0.{n}:47120", **kw) for n, i in enumerate(ids, 1)]  # type: ignore[arg-type]


def sched(cfg: ProbeConfig = CFG, seed: int = 7, now: float = 0.0) -> ProbeScheduler:
    return ProbeScheduler(cfg, rng=random.Random(seed), now=now)  # noqa: S311


def due(s: ProbeScheduler) -> float:
    return s.next_at


# ----------------------------------------------------------------- stagger


def test_nothing_starts_before_the_first_jittered_gap() -> None:
    s = sched()
    assert 5.0 <= s.next_at <= 15.0  # gap 10 s, jitter 0.5
    assert s.pick(nodes("a", "b"), s.next_at - 0.01) is None
    assert s.pick(nodes("a", "b"), s.next_at) is not None


def test_one_probe_per_gap_and_gaps_stay_within_the_jitter() -> None:
    s = sched(replace(CFG, budget_bytes_per_min=10**12))
    pool = nodes("a", "b", "c", "d", "e", "f")
    t = s.next_at
    started = s.pick(pool, t)
    assert started is not None
    gap = s.next_at - t
    assert 5.0 <= gap <= 15.0
    # another pair is free, but the gap has not passed
    assert s.pick(pool, t + gap / 2) is None


@pytest.mark.parametrize("seed", range(20))
def test_jitter_bounds_hold_for_any_seed(seed: int) -> None:
    s = sched(seed=seed)
    for _ in range(50):
        before = s.next_at
        s.pick((), before)
        assert 5.0 <= s.next_at - before <= 15.0


# ------------------------------------------------------------ interval


def test_a_pair_is_probed_at_most_once_per_interval() -> None:
    s = sched()
    pool = nodes("a", "b")
    first = s.pick(pool, due(s))
    assert first is not None and (first.src, first.dst) == ("a", "b")
    s.finish(first.probe_id, 12 * MIB, due(s))
    second = s.pick(pool, due(s))
    assert second is not None and (second.src, second.dst) == ("b", "a")
    s.finish(second.probe_id, 12 * MIB, due(s))
    # both directions done: nothing until a's pair's interval has passed
    t = due(s)
    while t < 900.0 - 1.0:
        assert s.pick(pool, t) is None
        t = due(s)
    later = s.pick(pool, 900.0 + 60.0)
    assert later is not None and (later.src, later.dst) == ("a", "b")


def test_a_failed_probe_still_counts_for_the_interval() -> None:
    s = sched()
    pool = nodes("a", "b")
    first = s.pick(pool, due(s))
    assert first is not None
    s.finish(first.probe_id, 0, due(s))  # failed: nothing moved
    nxt = s.pick(pool, due(s))
    assert nxt is not None and (nxt.src, nxt.dst) == ("b", "a")


def test_oldest_pair_first_then_node_id_order() -> None:
    s = sched(replace(CFG, budget_bytes_per_min=10**12))
    pool = nodes("c", "a", "b")
    seen = []
    for _ in range(6):
        p = s.pick(pool, due(s))
        assert p is not None
        seen.append((p.src, p.dst))
        s.finish(p.probe_id, 1, due(s))
    assert seen == [
        ("a", "b"),
        ("a", "c"),
        ("b", "a"),
        ("b", "c"),
        ("c", "a"),
        ("c", "b"),
    ]


def test_a_node_without_peer_endpoint_is_never_a_target() -> None:
    s = sched()
    pool = [ProbeNode("a", "10.0.0.1:47120"), ProbeNode("b", "")]
    p = s.pick(pool, due(s))
    assert p is not None and (p.src, p.dst) == ("b", "a")
    s.finish(p.probe_id, 1, due(s))
    assert s.pick(pool, due(s)) is None


# -------------------------------------------------------- one per node


def test_a_node_takes_part_in_one_probe_at_a_time() -> None:
    s = sched(replace(CFG, budget_bytes_per_min=10**12))
    pool = nodes("a", "b", "c", "d")
    first = s.pick(pool, due(s))
    assert first is not None and (first.src, first.dst) == ("a", "b")
    second = s.pick(pool, due(s))
    assert second is not None and {second.src, second.dst} == {"c", "d"}
    # all four engaged
    assert s.pick(pool, due(s)) is None
    s.finish(first.probe_id, 1, due(s))
    third = s.pick(pool, due(s))
    assert third is not None and {third.src, third.dst} <= {"a", "b"}


# -------------------------------------------------------------- budget


def test_the_budget_blocks_a_probe_whose_worst_case_does_not_fit() -> None:
    cfg = replace(CFG, budget_bytes_per_min=12 * MIB)
    s = sched(cfg)
    pool = nodes("a", "b", "c", "d")
    first = s.pick(pool, due(s))
    assert first is not None
    assert s.spent(due(s)) == 12 * MIB
    # c and d are free, but one worst case is already reserved
    assert s.pick(pool, due(s)) is None


def test_a_finished_probe_is_charged_what_it_moved_for_one_window() -> None:
    cfg = replace(CFG, budget_bytes_per_min=16 * MIB)
    s = sched(cfg)
    pool = nodes("a", "b", "c", "d")
    t0 = due(s)
    first = s.pick(pool, t0)
    assert first is not None
    s.finish(first.probe_id, 4 * MIB, t0 + 1)  # moved 4 MiB, not 12
    assert s.spent(t0 + 1) == 4 * MIB
    second = s.pick(pool, due(s))  # 4 + 12 <= 16 now
    assert second is not None
    s.finish(second.probe_id, 12 * MIB, due(s))
    assert s.pick(pool, due(s)) is None  # 16 MiB spent within the window
    assert s.spent(t0 + BUDGET_WINDOW_S + 0.01) <= 12 * MIB  # first one aged out


def test_budget_never_exceeded_over_a_long_run() -> None:
    """Any 60 s window: started worst cases never exceed the budget."""
    cfg = replace(CFG, gap_s=1.0, budget_bytes_per_min=30 * MIB)
    s = sched(cfg, seed=3)
    pool = nodes(*"abcdefgh")
    starts: list[float] = []
    t = 0.0
    while t < 3600.0:
        t = due(s)
        p = s.pick(pool, t)
        if p is not None:
            starts.append(t)
            s.finish(p.probe_id, 12 * MIB, t + 0.5)
    assert starts
    for i, start in enumerate(starts):
        in_window = [x for x in starts[i:] if x < start + BUDGET_WINDOW_S]
        assert len(in_window) * 12 * MIB <= 30 * MIB


def test_a_budget_smaller_than_one_probe_never_starts_one() -> None:
    cfg = replace(CFG, budget_bytes_per_min=11 * MIB)
    s = sched(cfg)
    assert s.pick(nodes("a", "b"), due(s)) is None


# ---------------------------------------------------------------- busy


def test_a_node_running_a_gpu_job_is_neither_source_nor_target() -> None:
    s = sched()
    pool = [
        ProbeNode("a", "h:1", gpu_job=True),
        ProbeNode("b", "h:2"),
        ProbeNode("c", "h:3"),
    ]
    picked = []
    for _ in range(4):
        p = s.pick(pool, due(s))
        if p is not None:
            picked.append((p.src, p.dst))
            s.finish(p.probe_id, 1, due(s))
    assert picked and all("a" not in pair for pair in picked)


@pytest.mark.parametrize(
    ("cpu", "skipped"), [(80.0, False), (80.1, True), (None, False)]
)
def test_cpu_above_the_threshold_is_skipped(cpu: float | None, skipped: bool) -> None:
    s = sched()
    pool = [ProbeNode("a", "h:1", cpu_percent=cpu), ProbeNode("b", "h:2")]
    p = s.pick(pool, due(s))
    if skipped:
        assert p is None
    else:
        assert p is not None and (p.src, p.dst) == ("a", "b")


# -------------------------------------------------- timeout / node loss


def test_a_probe_without_result_times_out_and_frees_its_nodes() -> None:
    s = sched()
    pool = nodes("a", "b")
    t0 = due(s)
    p = s.pick(pool, t0)
    assert p is not None
    assert s.expire(t0 + 59.9) == []
    gone = s.expire(t0 + 60.0)
    assert [pid for pid, _ in gone] == [p.probe_id] and s.timed_out == 1
    assert not s.in_flight
    # a late result for it is unknown now
    assert s.finish(p.probe_id, 1, t0 + 61) is None


def test_forget_node_frees_its_probes() -> None:
    s = sched(replace(CFG, budget_bytes_per_min=10**12))
    pool = nodes("a", "b", "c", "d")
    p1 = s.pick(pool, due(s))
    p2 = s.pick(pool, due(s))
    assert p1 is not None and p2 is not None
    assert s.forget_node("b") == [p1.probe_id]
    assert list(s.in_flight) == [p2.probe_id]


def test_abort_frees_without_charging() -> None:
    s = sched()
    p = s.pick(nodes("a", "b"), due(s))
    assert p is not None
    s.abort(p.probe_id)
    assert s.spent(due(s)) == 0 and s.started == 0


def test_same_seed_same_choices() -> None:
    def run(seed: int) -> list[tuple[str, str, str]]:
        s = sched(seed=seed)
        out = []
        for _ in range(10):
            p = s.pick(nodes("a", "b", "c"), due(s))
            if p is not None:
                out.append((p.probe_id, p.src, p.dst))
                s.finish(p.probe_id, 1, due(s))
        return out

    assert run(5) == run(5)


# ------------------------------------------------------- EWMA / staleness


def test_ewma_first_sample_then_weighted() -> None:
    assert ewma(None, 100.0, 0.3) == 100.0
    assert ewma(100.0, 200.0, 0.3) == pytest.approx(130.0)
    assert ewma(130.0, 200.0, 0.3) == pytest.approx(151.0)
    with pytest.raises(ValueError, match="alpha"):
        ewma(1.0, 2.0, 0.0)


def test_staleness_boundary_and_clock_step_back() -> None:
    now = dt.datetime(2026, 10, 6, 12, tzinfo=dt.UTC)
    hour = dt.timedelta(hours=1)
    assert not is_stale(now - hour, now, 3600.0)
    assert is_stale(now - hour - dt.timedelta(seconds=1), now, 3600.0)
    assert age_s(now + hour, now) == 0.0
