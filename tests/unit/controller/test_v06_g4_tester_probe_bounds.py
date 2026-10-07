"""V0.6 G4 tester round 1 (D111): the probe scheduler and prober under attack.

Written from the decision text, not from the implementation: every bound is
checked by a randomised walk with an injected clock (many nodes, lost results,
timeouts, node loss, aborts) against an independent model, plus targeted cases
for restart seeding, hostile results and settings that defeat a bound.
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
import random
from dataclasses import dataclass, field
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.network.links import LinkRow
from meshforge.controller.network.scheduler import (
    BUDGET_WINDOW_S,
    ProbeConfig,
    ProbeNode,
    ProbeScheduler,
)
from meshforge.controller.network.service import NetworkProber
from meshforge.controller.network.store import ProbeSample
from meshforge.controller.peer_signing import PeerTokenIssuer
from meshforge.controller.settings import NetworkSettings

MIB = 1024 * 1024
KEY = Ed25519PrivateKey.generate()
WALL = 1_800_000_000.0


def ids(n: int) -> list[str]:
    return [f"node_{i:026d}" for i in range(n)]


# --------------------------------------------------------------- random walk


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_random_walk_keeps_interval_one_per_node_and_budget(seed: int) -> None:
    """20 nodes (380 ordered pairs), random results/losses/timeouts/aborts/node
    losses: no pair twice inside the interval, no node in two probes at once,
    and the bytes started in any 60 s window never exceed the budget."""
    rng = random.Random(seed)  # noqa: S311
    cfg = ProbeConfig(
        interval_s=300.0,
        gap_s=1.0,
        jitter=0.5,
        budget_bytes_per_min=30 * MIB,  # room for two worst-case probes
        timeout_s=40.0,
        busy_cpu_percent=80.0,
    )
    sched = ProbeScheduler(cfg, rng=random.Random(seed), now=0.0)  # noqa: S311
    nodes = ids(20)
    cost: dict[str, tuple[float, int]] = {}  # probe id -> (started, charged)
    last_start: dict[tuple[str, str], float] = {}
    t = 0.0
    started_total = 0
    for _ in range(6000):
        t += rng.uniform(0.2, 3.0)
        sched.expire(t)
        # random end of a probe in flight
        for pid in list(sched.in_flight):
            roll = rng.random()
            if roll < 0.15:
                moved = rng.choice([0, 1, 12 * MIB, 10**15, -5])
                sched.finish(pid, moved, t)
                cost[pid] = (cost[pid][0], max(0, min(moved, cfg.worst_case_bytes)))
            elif roll < 0.18:
                sched.forget_node(sched.in_flight[pid].src)
            elif roll < 0.19:
                sched.abort(pid)
                cost[pid] = (cost[pid][0], 0)
        # a result for nothing, at any time
        assert sched.finish("nope", 5, t) is None
        probe_nodes = [
            ProbeNode(
                n,
                "127.0.0.1:1",
                gpu_job=rng.random() < 0.1,
                cpu_percent=rng.choice([None, 5.0, 79.9, 80.0, 80.1, 99.0]),
            )
            for n in nodes
            if rng.random() < 0.9
        ]
        before = {x for f in sched.in_flight.values() for x in (f.src, f.dst)}
        planned = sched.pick(probe_nodes, t)
        if planned is None:
            continue
        started_total += 1
        by_id = {n.node_id: n for n in probe_nodes}
        for end in (planned.src, planned.dst):
            assert end not in before, "a node in two probes at once"
            node = by_id[end]
            assert not node.gpu_job
            assert node.cpu_percent is None or node.cpu_percent <= 80.0
        pair = (planned.src, planned.dst)
        assert pair not in last_start or t - last_start[pair] >= cfg.interval_s
        last_start[pair] = t
        cost[planned.probe_id] = (t, cfg.worst_case_bytes)
        in_window = sum(c for s, c in cost.values() if t - BUDGET_WINDOW_S < s <= t)
        assert in_window <= cfg.budget_bytes_per_min, (t, in_window)
    assert started_total > 50, "the walk must actually probe"


def test_reservation_is_not_leaked_by_lost_results_and_budget_recovers() -> None:
    """Results that never arrive: the timeout frees the nodes, and the budget is
    not exhausted for ever - after the window a probe starts again."""
    cfg = ProbeConfig(
        gap_s=1.0,
        jitter=0.0,
        timeout_s=10.0,
        budget_bytes_per_min=2 * 12 * MIB,
    )
    sched = ProbeScheduler(cfg, rng=random.Random(1), now=0.0)  # noqa: S311
    nodes = [ProbeNode(n, "127.0.0.1:1") for n in ids(8)]
    t = 1.0
    starts: list[float] = []
    while t < 400:
        sched.expire(t)
        if sched.pick(nodes, t) is not None:
            starts.append(t)
        t += 1.0
    assert starts, "lost results must not exhaust the budget for ever"
    # 8 nodes -> 4 disjoint pairs at most; budget allows 2 per minute
    for i, s in enumerate(starts):
        assert sum(1 for x in starts[: i + 1] if s - 60 < x <= s) <= 2
    assert starts[-1] > 300, "still probing at the end of the run"
    assert sched.spent(t) <= cfg.budget_bytes_per_min


def test_every_pair_of_a_small_fabric_is_eventually_probed() -> None:
    """Fairness: with 6 nodes (30 ordered pairs), generous budget, results
    arriving at once, every ordered pair is probed within a bounded time."""
    cfg = ProbeConfig(interval_s=100.0, gap_s=1.0, jitter=0.0)
    sched = ProbeScheduler(cfg, rng=random.Random(2), now=0.0)  # noqa: S311
    nodes = [ProbeNode(n, "127.0.0.1:1") for n in ids(6)]
    seen: set[tuple[str, str]] = set()
    t = 1.0
    while t < 400 and len(seen) < 30:
        planned = sched.pick(nodes, t)
        if planned is not None:
            seen.add((planned.src, planned.dst))
            sched.finish(planned.probe_id, 1000, t)
        t += 1.0
    assert len(seen) == 30, len(seen)


def test_pick_cost_is_linear_enough_for_many_nodes() -> None:
    """A tick over 60 nodes (3540 pairs) is well under a second."""
    import time

    cfg = ProbeConfig(gap_s=0.1, jitter=0.0)
    sched = ProbeScheduler(cfg, rng=random.Random(3), now=0.0)  # noqa: S311
    nodes = [ProbeNode(n, "127.0.0.1:1") for n in ids(60)]
    started = time.perf_counter()
    assert sched.pick(nodes, 1.0) is not None
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("cpu", "busy"), [(80.0, False), (80.0001, True), (None, False), (100.0, True)]
)
def test_cpu_threshold_is_strictly_above_80(cpu: float | None, busy: bool) -> None:
    sched = ProbeScheduler(ProbeConfig(), rng=random.Random(1), now=0.0)  # noqa: S311
    assert sched.busy(ProbeNode("a", "h:1", cpu_percent=cpu)) is busy


def test_a_busy_node_is_neither_source_nor_target() -> None:
    cfg = ProbeConfig(gap_s=1.0, jitter=0.0)
    sched = ProbeScheduler(cfg, rng=random.Random(1), now=0.0)  # noqa: S311
    a, b, c = ids(3)
    nodes = [
        ProbeNode(a, "h:1"),
        ProbeNode(b, "h:1", gpu_job=True),
        ProbeNode(c, "h:1"),
    ]
    t = 1.0
    for _ in range(10):
        planned = sched.pick(nodes, t)
        if planned is not None:
            assert b not in (planned.src, planned.dst)
            sched.finish(planned.probe_id, 1, t)
        t += 1000.0


# ----------------------------------------------------------------- restarts


class Clock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@dataclass
class FakeLive:
    node_id: str
    peer_endpoint: str = "127.0.0.1:47120"
    state: NodeState = NodeState.ONLINE
    ended: bool = False
    session_id: str = "sess"


@dataclass
class FakeManager:
    lives: list[FakeLive]
    sent: list[tuple[str, str, Any]] = field(default_factory=list)

    def snapshot(self) -> list[FakeLive]:
        return list(self.lives)

    def send_command(self, node_id: str, name: str, payload: Any) -> str | None:
        self.sent.append((node_id, name, payload))
        return "cmd"


@dataclass
class FakeStore:
    rows: list[LinkRow] = field(default_factory=list)
    written: list[ProbeSample] = field(default_factory=list)
    gpu_calls: int = 0
    gpu_fail: bool = False

    async def links(self) -> list[LinkRow]:
        return list(self.rows)

    async def record(self, sample: ProbeSample, **_: Any) -> bool:
        self.written.append(sample)
        return True

    async def gpu_busy(self) -> set[str]:
        self.gpu_calls += 1
        if self.gpu_fail:
            raise RuntimeError("database is down")
        return set()


def prober(
    lives: list[FakeLive], store: FakeStore, clock: Clock, wall_offset: float = 0.0
) -> tuple[NetworkProber, FakeManager]:
    manager = FakeManager(lives)
    p = NetworkProber(
        manager,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        PeerTokenIssuer(KEY, clock=lambda: WALL),
        NetworkSettings(probe_gap_s=1.0, probe_jitter=0.0),
        clock=clock,
        wall_clock=lambda: dt.datetime.fromtimestamp(WALL + wall_offset, dt.UTC),
        gpu_busy=store.gpu_busy,
        rng=random.Random(1),  # noqa: S311
    )
    return p, manager


def row(src: str, dst: str, measured: float) -> LinkRow:
    return LinkRow(src, dst, 1.0, 5e7, 2, dt.datetime.fromtimestamp(measured, dt.UTC))


@pytest.mark.asyncio
async def test_future_measured_at_never_blocks_a_pair_longer_than_one_interval() -> (
    None
):
    a, b = ids(2)
    store = FakeStore([row(a, b, WALL + 86_400), row(b, a, WALL + 86_400)])
    clock = Clock(10_000.0)
    p, _manager = prober([FakeLive(a), FakeLive(b)], store, clock)
    await p.restore()
    clock.t += 901.0 + 2.0
    got = None
    for _ in range(5):
        got = await p.tick() or got
        clock.t += 1.0
    assert got is not None, "a clock step back starved the pair for a day"


@pytest.mark.asyncio
async def test_rows_naming_unknown_or_offline_nodes_are_harmless() -> None:
    a, b, ghost = ids(3)
    store = FakeStore(
        [
            row(ghost, a, WALL - 10),
            row(a, ghost, WALL - 10),
            row("", "", WALL - 10),
            row("x" * 500, a, WALL - 10),
        ]
    )
    clock = Clock(10_000.0)
    p, _manager = prober([FakeLive(a), FakeLive(b)], store, clock)
    assert await p.restore() == 4
    clock.t = p.scheduler.next_at
    planned = await p.tick()
    assert planned is not None and {planned.src, planned.dst} == {a, b}


@pytest.mark.asyncio
async def test_seeded_pairs_do_not_starve_never_measured_pairs() -> None:
    a, b, c = ids(3)
    store = FakeStore([row(a, b, WALL - 5), row(b, a, WALL - 5)])
    clock = Clock(10_000.0)
    p, _manager = prober([FakeLive(a), FakeLive(b), FakeLive(c)], store, clock)
    await p.restore()
    seen: set[tuple[str, str]] = set()
    for _ in range(40):
        clock.t = max(clock.t + 1.0, p.scheduler.next_at)
        planned = await p.tick()
        if planned is not None:
            seen.add((planned.src, planned.dst))
            p.scheduler.finish(planned.probe_id, 10, clock.t)
    # the four pairs with c came first and the two seeded ones waited
    assert {(a, c), (c, a), (b, c), (c, b)} <= seen
    assert (a, b) not in seen and (b, a) not in seen


@pytest.mark.asyncio
async def test_crash_loop_never_probes_a_fresh_pair() -> None:
    """A controller restarting every 20 s re-reads the table each time: a pair
    measured 100 s ago is never probed, however often we restart."""
    a, b = ids(2)
    store = FakeStore([row(a, b, WALL - 100), row(b, a, WALL - 100)])
    sent = 0
    for _ in range(25):
        clock = Clock(5_000.0)
        p, manager = prober([FakeLive(a), FakeLive(b)], store, clock)
        await p.restore()
        for _ in range(30):
            clock.t += 1.0
            await p.tick()
        sent += len(manager.sent)
    assert sent == 0


@pytest.mark.xfail(strict=True, reason="T-2: probe_interval_s may exceed stale_after_s")
def test_a_link_cannot_be_permanently_stale_by_configuration() -> None:
    """Interval and staleness interact: with the probe interval longer than
    ``stale_after_s`` every link is stale between probes (and, for a pair that
    keeps failing, for ever). The settings must refuse the combination."""
    with pytest.raises(ValueError, match=r"stale|interval"):
        NetworkSettings(probe_interval_s=7200.0, stale_after_s=3600.0)


@pytest.mark.xfail(
    strict=True,
    reason="T-3: probe_bytes=0 accepted; every probe then fails bad_numbers",
)
def test_a_probe_of_zero_bytes_cannot_be_configured() -> None:
    """``probe_bytes = 0`` makes every agent report ``bw = 0`` - which the
    controller rejects as bad numbers: a matrix that can never fill."""
    with pytest.raises(ValueError):
        NetworkSettings(probe_bytes=0)


# --------------------------------------------------------------- hostile input


async def start_one(
    p: NetworkProber, manager: FakeManager, clock: Clock
) -> agent_pb2.ProbeRequest:
    clock.t = p.scheduler.next_at
    planned = await p.tick()
    assert planned is not None
    return manager.sent[-1][2]  # type: ignore[no-any-return]


def good(cmd: agent_pb2.ProbeRequest, **kw: Any) -> agent_pb2.NetProbeResult:
    base: dict[str, Any] = {
        "probe_id": cmd.probe_id,
        "target_node_id": cmd.target_node_id,
        "ok": True,
        "rtt_ms": 2.0,
        "bw_bytes_s": 50e6,
        "bytes": 12 * MIB,
    }
    base.update(kw)
    return agent_pb2.NetProbeResult(**base)


BAD_NUMBERS = [
    {"rtt_ms": math.nan},
    {"rtt_ms": math.inf},
    {"rtt_ms": -1.0},
    {"rtt_ms": 1e12},
    {"bw_bytes_s": math.nan},
    {"bw_bytes_s": math.inf},
    {"bw_bytes_s": -1.0},
    {"bw_bytes_s": 0.0},
    {"bw_bytes_s": 1e15},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_NUMBERS, ids=lambda d: str(d))
async def test_impossible_numbers_are_never_stored(bad: dict[str, float]) -> None:
    a, b = ids(2)
    store, clock = FakeStore(), Clock(100.0)
    p, manager = prober([FakeLive(a), FakeLive(b)], store, clock)
    cmd = await start_one(p, manager, clock)
    src = manager.sent[-1][0]
    p.on_result(src, good(cmd, **bad))
    await p.drain()
    assert store.written == []
    assert p.failed == 1


@pytest.mark.xfail(
    strict=True,
    reason="T-1: 0 < bw is the only lower bound; 1e-300 B/s reaches the matrix",
)
@pytest.mark.asyncio
@pytest.mark.parametrize("bw", [1e-300, 5e-324, 1e-9])
async def test_a_vanishing_positive_bandwidth_is_not_stored(bw: float) -> None:
    """``0 < bw`` is the only lower bound; a link of 1e-300 B/s would give the
    planner an infinite transfer time (``Infinity`` in the stored decision)."""
    a, b = ids(2)
    store, clock = FakeStore(), Clock(100.0)
    p, manager = prober([FakeLive(a), FakeLive(b)], store, clock)
    cmd = await start_one(p, manager, clock)
    p.on_result(manager.sent[-1][0], good(cmd, bw_bytes_s=bw))
    await p.drain()
    assert store.written == [], "an unusable bandwidth reached the matrix"


@pytest.mark.asyncio
async def test_replay_unknown_and_foreign_results_do_nothing() -> None:
    a, b, c = ids(3)
    store, clock = FakeStore(), Clock(100.0)
    p, manager = prober([FakeLive(a), FakeLive(b), FakeLive(c)], store, clock)
    cmd = await start_one(p, manager, clock)
    src = manager.sent[-1][0]
    other = next(n for n in (a, b, c) if n not in (src, cmd.target_node_id))
    p.on_result(other, good(cmd))  # a third node answers
    p.on_result(src, good(cmd, target_node_id=other))  # wrong target
    p.on_result(src, good(cmd, probe_id="never-issued"))
    p.on_result(src, good(cmd, probe_id="x" * 100_000))
    assert p.scheduler.in_flight, "the real probe is still in flight"
    p.on_result(src, good(cmd))
    p.on_result(src, good(cmd))  # replayed after a reconnect
    await p.drain()
    assert len(store.written) == 1
    assert p.measured == 1


@pytest.mark.asyncio
async def test_a_failure_with_garbage_error_text_is_sanitised() -> None:
    a, b = ids(2)
    store, clock = FakeStore(), Clock(100.0)
    p, manager = prober([FakeLive(a), FakeLive(b)], store, clock)
    cmd = await start_one(p, manager, clock)
    p.on_result(
        manager.sent[-1][0],
        good(cmd, ok=False, error="\x1b]0;pwn\x07\x1b[31m" + "x" * 10_000),
    )
    ((_, _, failure),) = p.failure_rows()
    assert "\x1b" not in failure.error and len(failure.error) <= 32


@pytest.mark.xfail(
    strict=True, reason="T-4: reported bytes=0 refunds the whole reservation"
)
@pytest.mark.asyncio
async def test_an_ok_result_that_claims_no_bytes_still_costs_budget() -> None:
    """A result whose ``bw`` says it downloaded megabytes but whose ``bytes``
    says 0 must not refund the whole reservation (a lying or buggy agent would
    keep the fabric budget free for ever)."""
    a, b = ids(2)
    store, clock = FakeStore(), Clock(100.0)
    p, manager = prober([FakeLive(a), FakeLive(b)], store, clock)
    cmd = await start_one(p, manager, clock)
    p.on_result(manager.sent[-1][0], good(cmd, bytes=0))
    assert p.scheduler.spent(clock.t) > 0


@pytest.mark.xfail(strict=True, reason="T-5: a failing due tick is retried every 50 ms")
@pytest.mark.asyncio
async def test_a_failing_gpu_query_does_not_spin_the_loop() -> None:
    """With the database down the due tick fails; ``run`` must back off rather
    than retry (and log a traceback) every 50 ms."""
    import asyncio

    a, b = ids(2)
    store, clock = FakeStore(gpu_fail=True), Clock(100.0)
    p, _manager = prober([FakeLive(a), FakeLive(b)], store, clock)
    clock.t = p.scheduler.next_at  # due, and the clock never moves again
    task = asyncio.create_task(p.run())
    await asyncio.sleep(1.5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert store.gpu_calls <= 5, f"{store.gpu_calls} failing DB reads in 1.5 s"


def test_stagger_spacing_stays_inside_the_jitter_band_and_the_first_probe_waits() -> (
    None
):
    cfg = ProbeConfig(
        gap_s=20.0, jitter=0.5, interval_s=10.0, budget_bytes_per_min=10**9
    )
    sched = ProbeScheduler(cfg, rng=random.Random(9), now=1000.0)  # noqa: S311
    nodes = [ProbeNode(n, "h:1") for n in ids(10)]
    assert sched.pick(nodes, 1000.0) is None, "a restart must not probe at once"
    assert sched.pick(nodes, 1000.0 + 20.0 * 0.5 - 0.001) is None
    starts: list[float] = []
    t = 1000.0
    while len(starts) < 40:
        t += 0.25
        planned = sched.pick(nodes, t)
        if planned is not None:
            starts.append(t)
            sched.finish(planned.probe_id, 1, t)
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert min(gaps) >= 20.0 * 0.5 - 1e-9
    assert max(gaps) <= 20.0 * 1.5 + 0.25 + 1e-9
    assert len({round(g, 2) for g in gaps}) > 5, "the gap is not jittered"
