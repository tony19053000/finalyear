"""V0.6 G4 fix round 1, resume run (D131): what
``test_v06_g4_fix1_estimator.py`` leaves open.

* the bandwidth floor **at ingestion**: the prober stores a result at the
  floor, refuses one just under it as ``bw_below_floor`` (default and
  configured floor), and a later good result clears the pair's failure;
* the floor **in the estimator** when the config itself is unusable (zero,
  negative, NaN - a hand-built :class:`PlannerConfig`): no division by zero;
* the transfer term over 300 seeded random fabrics with hostile numbers:
  seconds = the sum of the per-tree seconds, each tree's source is a node that
  holds *that* tree (or the controller / the default), the term's source is the
  weakest one used, everything finite, and the node order of the snapshot
  changes nothing (checklist 16);
* scaled compute, the Lead's case from both ends: X (score 100) ran 10 s,
  Y (score 10) ran 11 s;
* every stored decision of those fabrics is strict JSON.

Pure: no database, an injected clock and ``now``, seeded RNG.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.network.service import BW_BELOW_FLOOR, NetworkProber
from meshforge.controller.network.store import ProbeSample
from meshforge.controller.peer_signing import PeerTokenIssuer
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    TermSource,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.estimator import MAX_TERM_S, estimate, weakest
from meshforge.controller.planner.types import ControllerLinkStat, LinkStat
from meshforge.controller.settings import NetworkSettings

pytestmark = pytest.mark.unit

MB = 1_000_000
NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
EXEC = frozenset({"exec", "exec-subprocess"})
A = "node_01J00000000000000000000000"
B = "node_01J00000000000000000000001"
KEY = Ed25519PrivateKey.generate()

# ------------------------------------------------- the floor at ingestion


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
    written: list[ProbeSample] = field(default_factory=list)

    async def record(self, sample: ProbeSample, **_: Any) -> bool:
        self.written.append(sample)
        return True


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def prober(**settings: Any) -> tuple[NetworkProber, FakeManager, FakeStore, Clock]:
    clock, store = Clock(), FakeStore()
    manager = FakeManager([FakeLive(A), FakeLive(B)])

    async def busy() -> set[str]:
        return set()

    made = NetworkProber(
        manager,  # type: ignore[arg-type]
        store,  # type: ignore[arg-type]
        PeerTokenIssuer(KEY, clock=lambda: 1_800_000_000.0),
        NetworkSettings(probe_gap_s=1.0, probe_jitter=0.0, **settings),
        clock=clock,
        gpu_busy=busy,
        rng=random.Random(1),  # noqa: S311
    )
    return made, manager, store, clock


async def probe_once(
    p: NetworkProber, manager: FakeManager, clock: Clock, bw: float
) -> tuple[str, str]:
    """Start the next due probe and answer it with ``bw``; (src, dst)."""
    clock.t = max(clock.t, p.scheduler.next_at)
    planned = await p.tick()
    assert planned is not None
    src, _name, cmd = manager.sent[-1]
    p.on_result(
        src,
        agent_pb2.NetProbeResult(
            probe_id=cmd.probe_id,
            target_node_id=cmd.target_node_id,
            ok=True,
            rtt_ms=2.5,
            bw_bytes_s=bw,
            bytes=4 * 1024 * 1024,
        ),
    )
    await p.drain()
    return src, cmd.target_node_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "bw"),
    [({}, 1023.9), ({}, 1.0), ({"min_bw_bytes_s": 1e6}, 999_999.0)],
)
async def test_a_result_under_the_floor_is_refused_and_named(
    settings: dict[str, Any], bw: float
) -> None:
    p, manager, store, clock = prober(**settings)
    pair = await probe_once(p, manager, clock, bw)
    assert store.written == []
    assert (p.failed, p.measured) == (1, 0)
    assert p.failures[pair].error == BW_BELOW_FLOOR == "bw_below_floor"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings", "bw"),
    [({}, 1024.0), ({}, 1024.1), ({"min_bw_bytes_s": 1e6}, 1e6)],
)
async def test_a_result_at_the_floor_is_stored(
    settings: dict[str, Any], bw: float
) -> None:
    p, manager, store, clock = prober(**settings)
    src, dst = await probe_once(p, manager, clock, bw)
    assert [(s.src_node_id, s.dst_node_id, s.bw_bytes_s) for s in store.written] == [
        (src, dst, bw)
    ]
    assert (p.failed, p.measured) == (0, 1) and (src, dst) not in p.failures


@pytest.mark.asyncio
async def test_a_good_result_clears_the_floor_failure_of_its_pair() -> None:
    """The pair's failure is the *latest* probe's: once the link measures a
    usable bandwidth again, ``compute network`` no longer shows the refusal."""
    p, manager, store, clock = prober(probe_interval_s=10.0, stale_after_s=60.0)
    first = await probe_once(p, manager, clock, 10.0)
    assert p.failures[first].error == BW_BELOW_FLOOR
    seen: set[tuple[str, str]] = set()
    for _ in range(8):  # both directions come round again after the interval
        clock.t += 11.0
        seen.add(await probe_once(p, manager, clock, 50e6))
        if first in seen:
            break
    assert first in seen and first not in p.failures
    assert all(s.bw_bytes_s == 50e6 for s in store.written)


# ---------------------------------------------- the floor in the estimator


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_slots": 2,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "j",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "k",
        "trees": (),
    }
    base.update(kw)
    return PlanRequest(**base)


def key(i: int) -> str:
    return "sha256:" + f"{i:064x}"


@pytest.mark.parametrize("floor", [0.0, -1.0, math.nan, 5e-324, 0.5])
@pytest.mark.parametrize("bw", [0.0, -3.0, math.nan, 1e-300])
def test_an_unusable_floor_never_divides_by_zero(floor: float, bw: float) -> None:
    """The settings refuse a floor under 1 B/s; a config built by hand with
    less is read as 1 B/s: 500 B over a dead link = 500 s, flagged."""
    cfg = PlannerConfig(min_bw_bytes_s=floor, default_bw_bytes_s=bw)
    target, holder = node("t"), node("h", cached=frozenset({key(1)}))
    stat = LinkStat(bw, 2.0, 1, NOW)
    for snapshot in (
        ClusterSnapshot(nodes=(target, holder), links={("t", "h"): stat}, config=cfg),
        ClusterSnapshot(
            nodes=(target,),
            controller_links={"t": ControllerLinkStat(bw, 1)},
            config=cfg,
        ),
        ClusterSnapshot(nodes=(target,), config=cfg),  # the default bandwidth
    ):
        req = request(
            trees=(TreeNeed(key(1), 500),),
            history=History(output_bytes=500.0, output_samples=1),
        )
        e = estimate(req, target, "cpu", snapshot, local=False, now=NOW)
        assert e.transfer.seconds == pytest.approx(500.0)
        assert e.transfer.detail["bw_clamped"] is True
        assert e.output.seconds == pytest.approx(500.0)
        assert math.isfinite(e.total_s)


# ------------------------------------------------- random hostile fabrics

HOSTILE_BW = (0.0, 1e-300, -5.0, math.nan, math.inf, 1e15)
SOURCE_OF = {
    "measured": TermSource.MEASURED,
    "history": TermSource.HISTORY,
    "assumed": TermSource.ASSUMED,
}


def a_bw(rng: random.Random) -> float:
    if rng.random() < 0.2:
        return rng.choice(HOSTILE_BW)
    return rng.choice((1024.0, 2.0 * MB, 20.0 * MB, 20.0 * MB, 80.0 * MB))


def a_score(rng: random.Random) -> float | None:
    return rng.choice((None, 0.0, -1.0, math.nan, math.inf, 10.0, 100.0, 400.0))


def fabric(seed: int) -> tuple[PlanRequest, ClusterSnapshot]:
    rng = random.Random(seed)  # noqa: S311 - a reproducible test fabric
    trees = tuple(
        TreeNeed(key(i), rng.choice((0, 1, 4096, 10 * MB, 300 * MB, 2**62)))
        for i in range(rng.randint(1, 5))
    )
    ids = [f"n{i}" for i in range(rng.randint(1, 5))]
    nodes = tuple(
        node(
            n,
            cached=frozenset(t.key for t in trees if rng.random() < 0.4),
            cpu_score=a_score(rng),
        )
        for n in ids
    )
    links = {
        (a, b): LinkStat(
            a_bw(rng),
            rng.choice((2.0, math.nan, math.inf)),
            rng.randint(1, 9),
            NOW - dt.timedelta(seconds=rng.choice((5.0, 600.0, 3599.0, 3601.0, 9e4))),
        )
        for a in ids
        for b in ids
        if a != b and rng.random() < 0.6
    }
    ctl = {
        n: ControllerLinkStat(
            a_bw(rng),
            rng.randint(1, 9),
            rng.choice((None, NOW - dt.timedelta(seconds=rng.choice((1.0, 7200.0))))),
        )
        for n in ids
        if rng.random() < 0.5
    }
    history = History(
        by_node_s={
            n: rng.choice((0.5, 10.0, 11.0, -3.0, math.inf, math.nan, 1e308))
            for n in ids
            if rng.random() < 0.5
        },
        output_bytes=rng.choice((None, 0.0, 4096.0, math.inf, math.nan, -1.0)),
        output_samples=1,
    )
    cfg = PlannerConfig(default_bw_bytes_s=a_bw(rng), min_bw_bytes_s=1024.0)
    snapshot = ClusterSnapshot(
        nodes=nodes, links=links, controller_links=ctl, config=cfg
    )
    return request(trees=trees, history=history), snapshot


SEEDS = range(300)


@pytest.mark.parametrize("seed", SEEDS)
def test_the_transfer_term_adds_up_and_blames_the_right_source(seed: int) -> None:
    req, snapshot = fabric(seed)
    sizes = {t.key: t.size for t in req.trees}
    for target in snapshot.nodes:
        e = estimate(req, target, "cpu", snapshot, local=False, now=NOW)
        assert math.isfinite(e.total_s) and e.total_s >= 0.0
        for term in e.terms().values():
            assert 0.0 <= term.seconds <= MAX_TERM_S
        term = e.transfer
        missing = [t for t in req.trees if t.key not in target.cached]
        assert term.detail["missing_bytes"] == sum(t.size for t in missing)
        if len(missing) < 2 or not any(t.size for t in missing):
            continue  # one tree keeps the flat detail; no bytes = nothing to cost
        items = term.detail["items"]
        assert [i["tree"] for i in items] == [t.key for t in missing]
        assert term.seconds == pytest.approx(
            min(MAX_TERM_S, sum(i["seconds"] for i in items)), rel=1e-6, abs=1e-5
        )
        assert term.source is weakest(
            TermSource.MEASURED, *(SOURCE_OF[i["source"]] for i in items)
        )
        for item in items:
            assert item["bytes"] == sizes[item["tree"]]
            assert item["bw_bytes_s"] >= 1024.0  # the floor, always
            assert item["seconds"] == pytest.approx(
                min(MAX_TERM_S, item["bytes"] / item["bw_bytes_s"]), rel=1e-3, abs=1e-5
            )
            if item["source"] == "measured":  # a probe: from a holder of THIS tree
                holder = snapshot.node(item["bw_from"])
                assert holder is not None and item["tree"] in holder.cached
                link = snapshot.links[(target.node_id, holder.node_id)]
                assert (NOW - link.measured_at).total_seconds() <= 3600.0
            elif item["source"] == "history":
                assert item["bw_from"] == "controller"
                assert target.node_id in snapshot.controller_links
            else:
                assert item["bw_source"] == "assumed" and "bw_from" not in item
                assert target.node_id not in snapshot.controller_links


@pytest.mark.parametrize("seed", SEEDS)
def test_decisions_are_strict_json_and_ignore_the_node_order(seed: int) -> None:
    """Checklist 16 + D131: the same fabric with its nodes listed backwards
    gives the same stored decision, and PostgreSQL would accept it."""
    req, snapshot = fabric(seed)
    stored = candidates_json(plan(req, snapshot, "cost_model", NOW))
    text = json.dumps(stored, allow_nan=False, sort_keys=True)
    assert "Infinity" not in text and "NaN" not in text
    backwards = ClusterSnapshot(
        nodes=tuple(reversed(snapshot.nodes)),
        links=dict(reversed(list(snapshot.links.items()))),
        controller_links=snapshot.controller_links,
        config=snapshot.config,
    )
    again = candidates_json(plan(req, backwards, "cost_model", NOW))
    assert json.dumps(again, allow_nan=False, sort_keys=True) == text


# ------------------------------------------- scaled compute: the X / Y case


@pytest.mark.parametrize(
    ("mine", "expected_s"),
    [(100.0, 1.1), (10.0, 11.0), (55.0, 2.0), (1.0, 110.0)],
)
def test_x_ran_10s_at_score_100_and_y_11s_at_score_10(
    mine: float, expected_s: float
) -> None:
    """Work = seconds x score: X did 1000, Y did 110. The least work is the
    basis (Y), whatever the target's score: 110 / score. Before D131 the
    fewest raw seconds (X) won and a score-10 node was told 100 s."""
    target = node("n", cpu_score=mine)
    x, y = node("x", cpu_score=100.0), node("y", cpu_score=10.0)
    req = request(history=History(by_node_s={"x": 10.0, "y": 11.0}))
    for nodes in ((target, x, y), (y, x, target)):
        term = estimate(
            req, target, "cpu", ClusterSnapshot(nodes=nodes), local=False, now=NOW
        ).compute
        assert term.seconds == pytest.approx(expected_s)
        assert term.source is TermSource.SCALED
        assert term.detail["from_node_id"] == "y"
        assert term.detail["score_ratio"] == pytest.approx(10.0 / mine, abs=1e-4)


def test_the_gpu_slot_normalises_by_the_gpu_score() -> None:
    """Same case on the GPU scores (the CPU scores point the other way)."""
    target = node("n", gpu_score=100.0, cpu_score=1.0)
    x = node("x", gpu_score=100.0, cpu_score=1.0)
    y = node("y", gpu_score=10.0, cpu_score=1000.0)
    req = request(history=History(by_node_s={"x": 10.0, "y": 11.0}))
    term = estimate(
        req, target, "gpu", ClusterSnapshot(nodes=(target, x, y)), local=False, now=NOW
    ).compute
    assert term.seconds == pytest.approx(1.1) and term.detail["from_node_id"] == "y"
