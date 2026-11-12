"""V1.0 G11 (D456): per-node fixed chunk cost and first-chunk cost.

Covered: the estimator (clean data, noise, outliers, too few samples, first
chunk extraction, an env build on the first chunk, a negative residual); the
cost model with per-node terms (a slow-start node gets less share / no chunk;
lane time = first + chunks x fixed + items x per item; fallback to the shared
overhead; chunk sizing uses the fastest lane's own cost); the decision JSON
and ``compute explain`` note; the replay round trip and the older-replay
digest; and the lab round 10 case (24 items, 2 per chunk, L1 + L2 + L4: real
51 s, the shared-overhead planner said 28 s).
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

import pytest

from meshforge.common.explain_text import term_note
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitConfig,
    SplitInfo,
    TermSource,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.chunk_cost import (
    MIN_FIXED_SAMPLES,
    ChunkSample,
    chunk_costs,
    node_chunk_cost,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.plan import snapshot_digest
from meshforge.controller.planner.replay import from_replay, to_replay
from meshforge.controller.planner.split import (
    ChunkChoice,
    chunk_overhead,
    estimate_split,
    lane,
    set_chunk,
)
from meshforge.controller.planner.types import PlannerConfig

pytestmark = pytest.mark.unit

EXEC = frozenset({"exec", "exec-subprocess", "exec-split"})
PROJECT = "sha256:" + "a" * 64
ENV = "e" * 64
NOW = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.UTC)


def _node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_threads": 8,
        "cpu_slots": 1,
        "cached": frozenset({PROJECT}),
        "cached_envs": {ENV: 1},
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def _snap(*nodes: NodeSnapshot, split: SplitConfig | None = None) -> ClusterSnapshot:
    config = PlannerConfig(split=split or SplitConfig())
    return ClusterSnapshot(
        nodes=tuple(sorted(nodes, key=lambda n: n.node_id)), config=config
    )


def _history(rates: dict[str, float], **kw: Any) -> History:
    return History(
        item_rate_by_node=rates,
        item_rate_samples_by_node=dict.fromkeys(rates, 20),
        env_hash_by_node=dict.fromkeys(rates, ENV),
        **kw,
    )


def _request(items: int, history: History, **split_kw: Any) -> PlanRequest:
    return PlanRequest(
        job_id="job_01J0000000000000000000000",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key=ENV,
        trees=(TreeNeed(PROJECT, 1000),),
        submitter_node_id="n1",
        seeded=True,
        history=history,
        split=SplitInfo(items=items, **split_kw),
    )


def _steady(n: int, *, items: int, spi: float, fixed: float, **kw: Any) -> list[Any]:
    return [
        ChunkSample(items=items, wall_s=items * spi + fixed, **kw) for _ in range(n)
    ]


# ------------------------------------------------------------------ estimator


def test_clean_data_gives_the_fixed_cost() -> None:
    samples = _steady(6, items=4, spi=1.5, fixed=20.0)
    cost = node_chunk_cost(samples, 1.5)
    assert cost is not None
    assert cost.fixed_s == pytest.approx(20.0)
    assert cost.fixed_samples == 6
    assert cost.first_s is None and cost.first_samples == 0


def test_stage_time_is_not_fixed_cost() -> None:
    """A chunk's staging is its items' fetch (``items_fetch``), not fixed."""
    samples = [ChunkSample(items=4, wall_s=6 + 10 + 3, stage_s=3) for _ in range(4)]
    cost = node_chunk_cost(samples, 1.5)
    assert cost is not None and cost.fixed_s == pytest.approx(10.0)


def test_noise_is_averaged_out() -> None:
    noise = [-2.1, 1.7, 0.4, -0.9, 2.3, -1.2, 0.0, 1.1, -0.3]
    samples = [ChunkSample(items=4, wall_s=6.0 + 20.0 + e) for e in noise]
    cost = node_chunk_cost(samples, 1.5)
    assert cost is not None and abs(cost.fixed_s - 20.0) < 1.0


def test_outliers_do_not_move_it() -> None:
    samples = _steady(7, items=4, spi=1.5, fixed=20.0)
    samples += [
        ChunkSample(items=4, wall_s=900.0),  # a stalled try
        ChunkSample(items=4, wall_s=0.5),  # clock step
    ]
    cost = node_chunk_cost(samples, 1.5)
    assert cost is not None and cost.fixed_s == pytest.approx(20.0)


def test_unusable_samples_are_ignored() -> None:
    samples = _steady(MIN_FIXED_SAMPLES, items=4, spi=1.5, fixed=20.0)
    samples += [
        ChunkSample(items=4, wall_s=math.nan),
        ChunkSample(items=4, wall_s=-5.0),
        ChunkSample(items=0, wall_s=3.0),
        ChunkSample(items=4, wall_s=math.inf),
    ]
    cost = node_chunk_cost(samples, 1.5)
    assert cost is not None and cost.fixed_samples == MIN_FIXED_SAMPLES


def test_too_few_samples_gives_none() -> None:
    few = _steady(MIN_FIXED_SAMPLES - 1, items=4, spi=1.5, fixed=20.0)
    firsts = [ChunkSample(items=4, wall_s=40.0, first=True)] * 5
    assert node_chunk_cost(few + firsts, 1.5) is None
    assert node_chunk_cost(_steady(5, items=4, spi=1.5, fixed=20), 0.0) is None
    assert node_chunk_cost(_steady(5, items=4, spi=1.5, fixed=20), math.nan) is None


def test_first_chunk_extra_is_extracted() -> None:
    samples = _steady(10, items=4, spi=1.5, fixed=20.0, env_s=4.0)
    samples.append(ChunkSample(items=4, wall_s=6 + 20 + 15, env_s=8.0, first=True))
    samples.append(ChunkSample(items=4, wall_s=6 + 20 + 17, env_s=9.0, first=True))
    cost = node_chunk_cost(samples, 1.5)
    assert cost is not None
    assert cost.fixed_s == pytest.approx(20.0)
    assert cost.first_s == pytest.approx(16.0)
    assert cost.first_samples == 2


def test_a_first_chunk_that_built_its_env_is_not_a_sample() -> None:
    """The planner prices an env build with its ``env`` term."""
    samples = _steady(5, items=4, spi=1.5, fixed=20.0, env_s=4.0)
    built = ChunkSample(items=4, wall_s=6 + 20 + 600, env_s=590.0, first=True)
    cost = node_chunk_cost([*samples, built], 1.5)
    assert cost is not None and cost.first_s is None
    cold = ChunkSample(items=4, wall_s=6 + 20 + 12, env_s=12.0, first=True)
    cost = node_chunk_cost([*samples, built, cold], 1.5)
    assert cost is not None and cost.first_s == pytest.approx(12.0)


def test_negative_residuals_clamp_to_zero() -> None:
    """A rate measured on a slower run: never a negative cost."""
    samples = _steady(5, items=4, spi=1.0, fixed=0.0)
    samples.append(ChunkSample(items=4, wall_s=3.0, first=True))
    cost = node_chunk_cost(samples, 1.5)
    assert cost is not None and cost.fixed_s == 0.0 and cost.first_s == 0.0


def test_chunk_costs_needs_a_rate_and_is_ordered() -> None:
    samples = {
        "b": _steady(4, items=2, spi=1.0, fixed=5.0),
        "a": _steady(4, items=2, spi=1.0, fixed=1.0),
        "c": _steady(4, items=2, spi=1.0, fixed=9.0),  # no rate
        "d": _steady(1, items=2, spi=1.0, fixed=9.0),  # too few
    }
    out = chunk_costs(samples, {"a": 1.0, "b": 1.0, "d": 1.0})
    assert list(out) == ["a", "b"]
    assert out["a"].fixed_s == pytest.approx(1.0)
    assert out["b"].fixed_s == pytest.approx(5.0)


# ------------------------------------------------------------------ cost model


def _slow_start(first: float = 15.0, **kw: Any) -> History:
    """n1 and n2 equally fast per item; n2 pays 20 s per chunk + ``first`` once."""
    return _history(
        {"n1": 1.0, "n2": 1.0},
        chunk_overhead_s=0.5,
        chunk_overhead_samples=40,
        chunk_fixed_s_by_node={"n1": 0.2, "n2": 20.0},
        chunk_fixed_samples_by_node={"n1": 30, "n2": 12},
        chunk_first_s_by_node={"n2": first},
        chunk_first_samples_by_node={"n2": 2},
        **kw,
    )


def test_lane_uses_learned_terms_else_the_shared_overhead() -> None:
    snapshot = _snap(_node("n1"), _node("n2"), _node("n3"))
    request = _request(100, _slow_start())
    slot = "cpu"
    n2 = lane(request, snapshot.node("n2"), slot, snapshot, local=False)
    assert n2.fixed is not None and n2.fixed.seconds == 20.0
    assert n2.fixed.source == TermSource.HISTORY
    assert n2.fixed.detail == {"from": "node", "samples": 12}
    assert n2.first is not None and n2.first.seconds == 15.0
    assert n2.ready_s == pytest.approx(n2.start_s + 15.0)
    n3 = lane(request, snapshot.node("n3"), slot, snapshot, local=False)
    assert n3.fixed == chunk_overhead(request, snapshot.config.split)
    assert n3.first is None and n3.ready_s == n3.start_s


def test_lane_time_is_first_plus_chunks_times_fixed_plus_items() -> None:
    snapshot = _snap(_node("n2"))
    request = _request(40, _slow_start())
    n2 = lane(request, snapshot.node("n2"), "cpu", snapshot, local=False)
    chunk = ChunkChoice(4, 10, ("fixed",))
    est = estimate_split(request, [n2], snapshot, chunk)
    lane_time = n2.start_s + 15.0 + 10 * 20.0 + 40 * 1.0
    body = est.total_s - est.estimate.output.seconds
    body -= est.estimate.extra["combine"].seconds
    assert body == pytest.approx(lane_time)
    term = est.estimate.extra["chunk_overhead"]
    assert term.seconds == pytest.approx(15.0 + 10 * 20.0)
    assert term.source == TermSource.HISTORY


def test_slow_start_node_gets_no_chunk_in_a_short_job() -> None:
    """24 items, 2 per chunk: n1 does everything in ~27 s, before n2's lane
    (30 s first-chunk extra) even starts paying off, so n2 is predicted idle
    (the lab's L2: 0 chunks) - unlike with one shared overhead, which gives
    it a share."""
    snapshot = _snap(_node("n1"), _node("n2"))
    lanes = [
        lane(
            _request(24, _slow_start(first=30.0)),
            snapshot.node(n),
            "cpu",
            snapshot,
            local=n == "n1",
        )
        for n in ("n1", "n2")
    ]
    chunk = ChunkChoice(2, 12, ("fixed",))
    learned = estimate_split(
        _request(24, _slow_start(first=30.0)), lanes, snapshot, chunk
    )
    assert learned.active == frozenset({"n1"})
    shared_history = _history(
        {"n1": 1.0, "n2": 1.0}, chunk_overhead_s=0.5, chunk_overhead_samples=40
    )
    shared_req = _request(24, shared_history)
    shared_lanes = [
        lane(shared_req, snapshot.node(n), "cpu", snapshot, local=n == "n1")
        for n in ("n1", "n2")
    ]
    shared = estimate_split(shared_req, shared_lanes, snapshot, chunk)
    assert shared.active == frozenset({"n1", "n2"})
    assert learned.total_s > shared.total_s


def test_slow_start_node_gets_less_share_in_a_long_job() -> None:
    """Both active; n2's items/s is ``c / (c + 20)`` of n1's, so the set is
    slower than with the shared overhead and the plan says why."""
    snapshot = _snap(_node("n1"), _node("n2"))
    request = _request(2000, _slow_start())
    decision = plan(request, snapshot, "cost_model", now=NOW)
    chosen = decision.split
    assert chosen is not None and chosen.split
    assert set(chosen.node_ids) == {"n1", "n2"}
    lanes = [
        lane(request, snapshot.node(n), "cpu", snapshot, local=n == "n1")
        for n in ("n1", "n2")
    ]
    est = estimate_split(
        request, lanes, snapshot, ChunkChoice(chosen.chunk_items, chosen.chunks, ())
    )
    size = chosen.chunk_items
    r1 = size / (size * 1.0 + 0.2)
    r2 = size / (size * 1.0 + 20.0)
    start = {item.node_id: item.start_s for item in lanes}
    m = (2000 + start["n1"] * r1 + (start["n2"] + 15.0) * r2) / (r1 + r2)
    share_n2 = (m - start["n2"] - 15.0) * r2
    assert 0 < share_n2 < 0.5 * 2000 * 0.98  # clearly less than half
    body = est.total_s - est.estimate.output.seconds
    body -= est.estimate.extra["combine"].seconds + est.estimate.extra["tail"].seconds
    assert body == pytest.approx(m, rel=1e-6)


def test_chunk_size_uses_the_fastest_lanes_own_overhead() -> None:
    """``max_overhead_frac``: the fastest lane's own t_n raises the chunk."""
    split = SplitConfig(target_chunk_s=1.0, min_chunks_per_node=1)
    snapshot = _snap(_node("n1"), _node("n2"), split=split)
    base = {"chunk_overhead_s": 0.0, "chunk_overhead_samples": 10}
    fast_costly = _history(
        {"n1": 10.0, "n2": 5.0},
        chunk_fixed_s_by_node={"n1": 5.0},
        chunk_fixed_samples_by_node={"n1": 9},
        **base,
    )
    plain = _history({"n1": 10.0, "n2": 5.0}, **base)
    sizes = []
    for history in (plain, fast_costly):
        req = _request(10_000, history)
        group = [
            lane(req, snapshot.node(n), "cpu", snapshot, local=n == "n1")
            for n in ("n1", "n2")
        ]
        sizes.append(set_chunk(req, group, snapshot).items)
    assert sizes[1] > sizes[0]


def test_no_learned_cost_keeps_the_decision_unchanged() -> None:
    """Empty per-node maps: same JSON, no ``per_node`` detail, same digest as a
    request built without the new fields at all."""
    snapshot = _snap(_node("n1"), _node("n2"), _node("n3"))
    history = _history({"n1": 2.0, "n2": 2.0, "n3": 2.0})
    request = _request(600, history)
    rows = candidates_json(plan(request, snapshot, "cost_model", now=NOW))
    for row in rows:
        terms = (row.get("estimate") or {}).get("terms", {})
        if "chunk_overhead" in terms:
            assert "per_node" not in terms["chunk_overhead"]["detail"]
            assert "first_s" not in terms["chunk_overhead"]["detail"]
    replay = to_replay(request, snapshot, NOW)
    assert "chunk_fixed_s_by_node" not in replay["request"]["history"]
    assert "chunk_first_s_by_node" not in replay["request"]["history"]


# ------------------------------------------------------------------ decision JSON


def test_decision_json_and_explain_note_show_the_per_node_terms() -> None:
    snapshot = _snap(_node("n1"), _node("n2"), _node("n3"))
    history = _history(
        {"n1": 1.0, "n2": 1.0, "n3": 1.0},
        chunk_overhead_s=0.5,
        chunk_overhead_samples=40,
        chunk_fixed_s_by_node={"n1": 0.2, "n2": 20.0},
        chunk_fixed_samples_by_node={"n1": 30, "n2": 12},
        chunk_first_s_by_node={"n2": 15.0},
        chunk_first_samples_by_node={"n2": 2},
    )
    decision = plan(_request(3000, history), snapshot, "cost_model", now=NOW)
    split_rows = [r for r in candidates_json(decision) if r["mode"] == "split"]
    assert split_rows
    term = next(
        r["estimate"]["terms"]["chunk_overhead"]
        for r in split_rows
        if r.get("estimate") and set(r["node_set"]) == {"n1", "n2", "n3"}
    )
    assert term["source"] == "history"
    per_node = term["detail"]["per_node"]
    assert per_node["n2"] == {
        "fixed_s": 20.0,
        "fixed_source": "history",
        "fixed_from": "node",
        "fixed_samples": 12,
        "first_s": 15.0,
        "first_samples": 2,
    }
    assert per_node["n3"]["fixed_from"] == "shared"
    assert per_node["n3"]["fixed_s"] == 0.5
    assert "first_s" not in per_node["n3"]
    note = term_note(term, name="chunk_overhead")
    assert note is not None
    assert "n2 20.0s (history n=12) +15.0s first (n=2)" in note  # G12 label
    assert "n3 0.5s (shared)" in note
    assert term_note(term, name="tail") is None or "per chunk" not in str(
        term_note(term, name="tail")
    )


def test_explain_note_tolerates_garbage() -> None:
    for detail in (
        {"per_node": "x"},
        {"per_node": {"a": "x"}},
        {"per_node": {"a": {"fixed_s": "no"}}},
        {"per_node": {}},
    ):
        assert term_note({"detail": detail}, name="chunk_overhead") is None


# ------------------------------------------------------------------ replay


def test_replay_round_trip_keeps_the_terms_and_the_decision() -> None:
    snapshot = _snap(_node("n1"), _node("n2"), _node("n3"))
    request = _request(3000, _slow_start())
    decision = plan(request, snapshot, "cost_model", now=NOW)
    replay = to_replay(request, snapshot, NOW)
    assert replay["request"]["history"]["chunk_fixed_s_by_node"] == {
        "n1": 0.2,
        "n2": 20.0,
    }
    again_req, again_snap, again_now = from_replay(replay)
    assert again_req.history == request.history
    again = plan(again_req, again_snap, "cost_model", now=again_now)
    assert candidates_json(again) == candidates_json(decision)


def test_an_older_replay_without_the_keys_decodes_and_keeps_its_digest() -> None:
    snapshot = _snap(_node("n1"), _node("n2"))
    request = _request(600, _history({"n1": 2.0, "n2": 2.0}))
    replay = to_replay(request, snapshot, NOW)
    for key in ("chunk_fixed_s_by_node", "chunk_first_s_by_node"):
        replay["request"]["history"].pop(key, None)
    again_req, again_snap, _ = from_replay(replay)
    assert again_req.history.chunk_fixed_s_by_node == {}
    assert snapshot_digest(again_req, again_snap) == snapshot_digest(
        request, again_snap
    )


# ------------------------------------------------------------------ lab case


def _lab_samples() -> tuple[dict[str, list[ChunkSample]], dict[str, float]]:
    """Lab round 10's per-chunk table (job_01M75155RE5YFXJ6FW46DV8HRP, 4 items a
    chunk, controller grant -> commit): L1 6.3 s (first 6.5 s), L4 26.6 s
    (first 42.3 s), L2 31.5 s (first 43.2 s). The agent's run time is the
    chunk process: the training (L1/L4 6.2 s, L2 21.4 s for 4 items) plus,
    on the Windows venv lanes, the interpreter start and torch import (~6 s
    assumed here; not separately measured)."""
    spec = {
        # node: (run_s for 4 items, steady wall, first wall, env check s)
        "L1": (6.2, 6.3, 6.5, 0.0),
        "L4": (6.2 + 6.0, 26.6, 42.3, 4.0),
        "L2": (21.4 + 6.0, 31.5, 43.2, 3.0),
    }
    wobble = (-0.6, 0.4, -0.2, 0.7, -0.4, 0.1, 0.3, -0.3)
    samples: dict[str, list[ChunkSample]] = {}
    spi: dict[str, float] = {}
    for node_id, (run_s, steady, first, env) in spec.items():
        spi[node_id] = run_s / 4
        rows = [
            ChunkSample(items=4, wall_s=steady + w, stage_s=0.2, env_s=env)
            for w in wobble
        ]
        rows.append(
            ChunkSample(items=4, wall_s=first, stage_s=0.2, env_s=env * 2, first=True)
        )
        samples[node_id] = rows
    return samples, spi


def _lab_request(history: History) -> PlanRequest:
    """job_01M74WKXPN8V7XM1N9HEBYNZ6N: 24 items, ``chunk: {items: 2}``, L1 the
    submitter (combine there), L3 excluded."""
    return _request(24, history, chunk_items=2)


def _lab_snapshot() -> ClusterSnapshot:
    return _snap(_node("L1"), _node("L2"), _node("L4"))


def _lab_history(*, per_node: bool) -> History:
    samples, spi = _lab_samples()
    rates = {n: 1.0 / s for n, s in spi.items()}
    # D438's shared overhead: median stage + upload of every chunk (~0.5 s)
    base: dict[str, Any] = {"chunk_overhead_s": 0.5, "chunk_overhead_samples": 27}
    if per_node:
        costs = chunk_costs(samples, spi)
        assert set(costs) == {"L1", "L2", "L4"}
        base |= {
            "chunk_fixed_s_by_node": {n: c.fixed_s for n, c in costs.items()},
            "chunk_fixed_samples_by_node": {
                n: c.fixed_samples for n, c in costs.items()
            },
            "chunk_first_s_by_node": {
                n: c.first_s for n, c in costs.items() if c.first_s is not None
            },
            "chunk_first_samples_by_node": {
                n: c.first_samples for n, c in costs.items() if c.first_s is not None
            },
        }
    return _history(rates, **base)


def _lab_set_s(history: History) -> float:
    snapshot = _lab_snapshot()
    request = _lab_request(history)
    lanes = [
        lane(request, snapshot.node(n), "cpu", snapshot, local=n == "L1")
        for n in ("L1", "L2", "L4")
    ]
    chunk = set_chunk(request, lanes, snapshot)
    assert chunk.items == 2 and chunk.chunks == 12
    return estimate_split(request, lanes, snapshot, chunk).total_s


def test_lab_case_learned_costs_predict_within_20_percent_of_51_s() -> None:
    real = 51.0
    learned = _lab_set_s(_lab_history(per_node=True))
    shared = _lab_set_s(_lab_history(per_node=False))
    assert abs(learned - real) / real <= 0.20, learned
    # the shared overhead under-predicts (lab: 28 s): that is what D456 fixes
    assert shared < 0.8 * real, shared
    assert abs(learned - real) < abs(shared - real)


def test_lab_case_replay_reproduces_the_prediction() -> None:
    snapshot = _lab_snapshot()
    request = _lab_request(_lab_history(per_node=True))
    decision = plan(request, snapshot, "cost_model", now=NOW)
    again = plan(
        *from_replay(to_replay(request, snapshot, NOW))[:2], "cost_model", now=NOW
    )
    assert candidates_json(again) == candidates_json(decision)
    three = next(
        r
        for r in candidates_json(decision)
        if r["mode"] == "split" and set(r["node_set"]) == {"L1", "L2", "L4"}
    )
    total = three["estimate"]["total_s"]
    assert abs(total - 51.0) / 51.0 <= 0.20, total
    per_node = three["estimate"]["terms"]["chunk_overhead"]["detail"]["per_node"]
    assert per_node["L4"]["fixed_from"] == "node"
    assert per_node["L4"]["first_s"] > 10.0
