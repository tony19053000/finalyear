"""V0.6 G2 tester round 1: the pure planner, attacked from D105-D108, checklist 16/18.

Written from the decisions, not from the implementation: determinism (input
order, node-id tie-breaks, seeded random), each policy's D107 corner cases
(missing score ranks last, cache_affinity ties -> least_loaded, wrap of the
round-robin cursor), slot choice incl. ``gpu: preferred`` fallback and agents
that never said their slots, the decision JSON bound, the honesty of every
estimate source, and a simulation: placing hundreds of jobs one after another
never takes a slot that is not free.
"""

from __future__ import annotations

# ruff: noqa: S311
import dataclasses
import json
import random
from typing import Any

import pytest

from meshforge.common.manifest import POLICIES
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    RejectCode,
    TermSource,
    TreeNeed,
    plan,
    snapshot_digest,
)
from meshforge.controller.planner.decision_json import (
    MAX_DECISION_CANDIDATES,
    candidates_json,
)
from meshforge.controller.planner.inputs import (
    effective_slots,
    node_snapshot,
    scores_of,
)

pytestmark = pytest.mark.unit

GIB = 1024**3
EXEC = frozenset({"exec", "exec-subprocess"})
EXEC_GPU = frozenset({"exec", "exec-docker", "exec-gpu"})
TREE = "sha256:" + "a" * 64
INPUT = "sha256:" + "b" * 64


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "ram_bytes": 16 * GIB,
        "cpu_threads": 8,
        "cpu_slots": 2,
        "gpu_slots": 0,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def gpu_node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "capabilities": EXEC_GPU,
        "gpu_vram_bytes": (8 * GIB,),
        "gpu_slots": 1,
    }
    base.update(kw)
    return node(node_id, **base)


def req(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job_01J0000000000000000000000",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e" * 64,
        "trees": (TreeNeed(TREE, 10_000_000),),
    }
    base.update(kw)
    return PlanRequest(**base)


def snap(*nodes: NodeSnapshot, **kw: Any) -> ClusterSnapshot:
    return ClusterSnapshot(nodes=tuple(nodes), **kw)


def chosen_id(
    request: PlanRequest, snapshot: ClusterSnapshot, policy: str
) -> str | None:
    decision = plan(request, snapshot, policy)
    return None if decision.chosen is None else decision.chosen.node_id


# ----------------------------------------------------------------- determinism


@pytest.mark.parametrize("policy", POLICIES)
def test_identical_nodes_every_policy_is_deterministic_under_input_order(
    policy: str,
) -> None:
    nodes = [node(f"node_{c}") for c in "abcdefgh"]
    request = req()
    baseline = plan(request, snap(*nodes), policy)
    for seed in range(12):
        shuffled = list(nodes)
        random.Random(seed).shuffle(shuffled)
        again = plan(request, snap(*shuffled), policy)
        assert [(c.node_id, c.rank) for c in again.candidates] == [
            (c.node_id, c.rank) for c in baseline.candidates
        ]
        assert again.reason_key == baseline.reason_key


@pytest.mark.parametrize(
    "policy",
    [p for p in POLICIES if p not in ("random", "round_robin")],
)
def test_identical_nodes_tie_breaks_on_the_lowest_node_id(policy: str) -> None:
    nodes = [node(n) for n in ("node_z", "node_b", "node_m", "node_a")]
    assert chosen_id(req(), snap(*nodes), policy) == "node_a"


def test_same_inputs_same_digest_and_a_changed_slot_changes_it() -> None:
    a, b = node("node_a"), node("node_b")
    request = req()
    assert snapshot_digest(request, snap(a, b)) == snapshot_digest(request, snap(a, b))
    busy = dataclasses.replace(b, used_cpu_slots=1, live_attempts=1)
    assert snapshot_digest(request, snap(a, b)) != snapshot_digest(
        request, snap(a, busy)
    )


def test_random_is_reproducible_but_a_retry_draws_again() -> None:
    nodes = [node(f"node_{i:02d}") for i in range(10)]
    snapshot = snap(*nodes)
    first = [chosen_id(req(attempt_no=1), snapshot, "random") for _ in range(5)]
    assert len(set(first)) == 1  # reproducible
    drawn = {chosen_id(req(attempt_no=n), snapshot, "random") for n in range(1, 40)}
    assert len(drawn) >= 4  # not a constant: each attempt number draws again


def test_random_spreads_over_jobs_and_never_picks_an_infeasible_node() -> None:
    nodes = [
        node("node_a"),
        node("node_b"),
        node("node_c"),
        node("node_d", state="offline"),
    ]
    counts: dict[str | None, int] = {}
    for i in range(600):
        pick = chosen_id(req(job_id=f"job_{i:026d}"), snap(*nodes), "random")
        counts[pick] = counts.get(pick, 0) + 1
    assert "node_d" not in counts
    assert set(counts) == {"node_a", "node_b", "node_c"}
    assert min(counts.values()) > 120  # roughly uniform (expected 200)


# ---------------------------------------------------------------- round robin


def test_round_robin_wraps_and_skips_an_infeasible_next_node() -> None:
    nodes = [
        node("node_a"),
        node("node_b"),
        node("node_c", state="suspect"),
        node("node_d"),
    ]
    cursor_to_pick = {
        None: "node_a",
        "node_a": "node_b",
        "node_b": "node_d",
        "node_c": "node_d",
        "node_d": "node_a",
        "node_zzz": "node_a",
    }
    for cursor, expected in cursor_to_pick.items():
        got = chosen_id(req(), snap(*nodes, round_robin_after=cursor), "round_robin")
        assert got == expected, (cursor, got)


def test_round_robin_with_one_feasible_node_always_picks_it() -> None:
    nodes = [node("node_a", state="offline"), node("node_b")]
    for cursor in (None, "node_a", "node_b", "node_c"):
        assert (
            chosen_id(req(), snap(*nodes, round_robin_after=cursor), "round_robin")
            == "node_b"
        )


# --------------------------------------------------------------- D107 policies


def test_strongest_suitable_missing_score_ranks_last_even_against_a_tiny_score() -> (
    None
):
    nodes = [
        node("node_a"),  # unmeasured
        node("node_b", cpu_score=0.001),
        node("node_c", cpu_score=5.0),
        node("node_d"),  # unmeasured
    ]
    decision = plan(req(), snap(*nodes), "strongest_suitable")
    assert [c.node_id for c in decision.candidates] == [
        "node_c",
        "node_b",
        "node_a",
        "node_d",
    ]


def test_strongest_suitable_uses_the_gpu_score_for_a_gpu_job_and_cpu_for_cpu() -> None:
    nodes = [
        gpu_node("node_a", cpu_score=900.0, gpu_score=2.0),
        gpu_node("node_b", cpu_score=10.0, gpu_score=9.0),
    ]
    assert (
        chosen_id(req(gpu="required"), snap(*nodes), "strongest_suitable") == "node_b"
    )
    assert chosen_id(req(gpu="none"), snap(*nodes), "strongest_suitable") == "node_a"


def test_strongest_suitable_preferred_on_a_cpu_slot_uses_the_cpu_score() -> None:
    # node_a's GPU slot is taken, so a `preferred` job lands on its CPU slot:
    # the CPU score, not the GPU score, is what the job will run on.
    a = gpu_node(
        "node_a", cpu_score=800.0, gpu_score=1.0, used_gpu_slots=1, live_attempts=1
    )
    b = gpu_node("node_b", cpu_score=10.0, gpu_score=50.0)
    decision = plan(req(gpu="preferred"), snap(a, b), "strongest_suitable")
    slots = {c.node_id: c.slot for c in decision.candidates}
    assert slots == {"node_a": "cpu", "node_b": "gpu"}
    # b's slot is a GPU (score 50), a's is a CPU (score 800): a is the stronger one
    assert decision.chosen is not None and decision.chosen.node_id == "node_a"


def test_resource_score_with_all_missing_numbers_still_ranks_every_node() -> None:
    nodes = [node(n, cpu_slots=1) for n in ("node_c", "node_a", "node_b")]
    decision = plan(req(), snap(*nodes), "resource_score")
    assert [c.node_id for c in decision.candidates] == ["node_a", "node_b", "node_c"]
    assert all(c.score is not None for c in decision.candidates)


def test_resource_score_weights_steer_the_choice() -> None:
    ram_rich = node("node_a", ram_free_bytes=64 * GIB, cpu_slots=1, cpu_score=1.0)
    slot_rich = node("node_b", ram_free_bytes=1 * GIB, cpu_slots=16, cpu_score=1.0)
    ram_cfg = PlannerConfig(resource_weights=(1.0, 0.0, 0.0))
    slot_cfg = PlannerConfig(resource_weights=(0.0, 1.0, 0.0))
    assert (
        chosen_id(req(), snap(ram_rich, slot_rich, config=ram_cfg), "resource_score")
        == "node_a"
    )
    assert (
        chosen_id(req(), snap(ram_rich, slot_rich, config=slot_cfg), "resource_score")
        == "node_b"
    )
    zero = PlannerConfig(resource_weights=(0.0, 0.0, 0.0))
    assert (
        chosen_id(req(), snap(ram_rich, slot_rich, config=zero), "resource_score")
        == "node_a"
    )


def test_least_loaded_counts_both_slot_kinds_and_unknown_cpu_is_last() -> None:
    nodes = [
        gpu_node("node_a", used_gpu_slots=1, live_attempts=1, cpu_percent=1.0),
        node("node_b", used_cpu_slots=1, live_attempts=1, cpu_percent=1.0),
        node("node_c", cpu_percent=90.0),
        node("node_d"),  # idle, cpu unknown
    ]
    decision = plan(req(), snap(*nodes), "least_loaded")
    # idle first (known CPU 90 % beats unknown), then the two with one slot used
    assert [c.node_id for c in decision.candidates] == [
        "node_c",
        "node_d",
        "node_a",
        "node_b",
    ]


def test_cache_affinity_counts_bytes_and_ties_fall_back_to_least_loaded() -> None:
    big, small = TreeNeed(TREE, 900), TreeNeed(INPUT, 100)
    request = req(trees=(big, small))
    a = node("node_a", cached=frozenset({INPUT}), used_cpu_slots=0)
    b = node("node_b", cached=frozenset({TREE}), used_cpu_slots=1, live_attempts=1)
    c = node("node_c", cached=frozenset({TREE}), used_cpu_slots=0)
    decision = plan(request, snap(a, b, c), "cache_affinity")
    # 900 cached on b and c (tie) -> the less loaded c; a holds only 100
    assert [x.node_id for x in decision.candidates] == ["node_c", "node_b", "node_a"]
    assert [x.score for x in decision.candidates] == [900.0, 900.0, 100.0]
    # nothing cached anywhere: purely least_loaded, then node id
    cold = plan(
        request,
        snap(node("node_x", used_cpu_slots=1, live_attempts=1), node("node_y")),
        "cache_affinity",
    )
    assert cold.chosen is not None and cold.chosen.node_id == "node_y"


def test_cache_affinity_seeded_local_holds_only_the_project_tree() -> None:
    request = req(
        trees=(TreeNeed(TREE, 500), TreeNeed(INPUT, 400)),
        submitter_node_id="node_b",
        seeded=True,
    )
    remote = node("node_a", cached=frozenset({INPUT}))
    local = node("node_b")
    decision = plan(request, snap(remote, local), "cache_affinity")
    scores = {c.node_id: c.score for c in decision.candidates}
    assert scores == {"node_a": 400.0, "node_b": 500.0}
    # a seeded flag from some OTHER submitter never helps a node that is not Local
    other = dataclasses.replace(request, submitter_node_id="node_zzz")
    scores = {
        c.node_id: c.score
        for c in plan(other, snap(remote, local), "cache_affinity").candidates
    }
    assert scores == {"node_a": 400.0, "node_b": 0.0}


def test_unseeded_local_is_not_credited() -> None:
    request = req(
        submitter_node_id="node_b", seeded=False, trees=(TreeNeed(TREE, 10**9),)
    )
    decision = plan(request, snap(node("node_a"), node("node_b")), "cost_model")
    for cand in decision.candidates:
        assert cand.estimate is not None
        assert cand.estimate.transfer.detail["missing_bytes"] == 10**9


def test_cost_model_chooses_the_lowest_total_and_a_cached_node_beats_a_faster_cold_one() -> (
    None
):
    cfg = PlannerConfig(default_bw_bytes_s=1e6, default_compute_s=10.0)
    request = req(
        trees=(TreeNeed(TREE, 100_000_000),),
        fingerprint="f" * 64,
        history=History(
            by_node_s={"node_a": 5.0, "node_b": 5.0},
            samples_by_node={"node_a": 3, "node_b": 3},
        ),
    )
    warm = node("node_b", cached=frozenset({TREE}))
    cold = node("node_a")
    decision = plan(request, snap(cold, warm, config=cfg), "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "node_b"
    assert decision.candidates[0].score == pytest.approx(5.0)
    assert decision.candidates[1].score == pytest.approx(105.0)


# ------------------------------------------------------------------ slots


def test_gpu_required_never_takes_a_cpu_slot_and_preferred_falls_back() -> None:
    full_gpu = gpu_node("node_a", used_gpu_slots=1, live_attempts=1)
    only = snap(full_gpu)
    d = plan(req(gpu="required"), only, "cost_model")
    assert d.chosen is None
    assert d.candidates[0].rejection is not None
    assert d.candidates[0].rejection.code is RejectCode.SLOTS_FULL
    p = plan(req(gpu="preferred"), only, "cost_model")
    assert p.chosen is not None and p.chosen.slot == "cpu"
    free = snap(gpu_node("node_a"))
    assert plan(req(gpu="preferred"), free, "cost_model").chosen.slot == "gpu"  # type: ignore[union-attr]
    assert plan(req(gpu="none"), free, "cost_model").chosen.slot == "cpu"  # type: ignore[union-attr]


def test_cpu_job_waits_when_only_the_gpu_slot_is_free() -> None:
    n = gpu_node("node_a", cpu_slots=1, used_cpu_slots=1, live_attempts=1)
    d = plan(req(gpu="none"), snap(n), "cost_model")
    assert d.chosen is None
    assert d.candidates[0].rejection.code is RejectCode.SLOTS_FULL  # type: ignore[union-attr]
    assert not d.all_static_rejections


def test_owner_with_zero_cpu_slots_and_no_gpu_is_a_static_rejection_for_a_cpu_job() -> (
    None
):
    d = plan(req(), snap(node("node_a", cpu_slots=0)), "cost_model")
    assert d.candidates[0].rejection.code is RejectCode.NO_CPU_SLOTS  # type: ignore[union-attr]
    assert d.all_static_rejections


def test_node_cap_wins_over_advertised_slots() -> None:
    n = node("node_a", cpu_slots=64, used_cpu_slots=3, live_attempts=3)
    d = plan(
        req(), snap(n, config=PlannerConfig(max_attempts_per_node=3)), "cost_model"
    )
    assert d.candidates[0].rejection.code is RejectCode.NODE_CAP  # type: ignore[union-attr]


def test_simulation_never_takes_a_slot_that_is_not_free() -> None:
    """Place 600 random jobs one after another, patching the snapshot the way the
    store's ``took`` does: per node used <= slots and live <= cap, always."""
    rng = random.Random(1905)
    cap = 5
    for _ in range(30):
        nodes: dict[str, NodeSnapshot] = {}
        for i in range(rng.randint(1, 6)):
            maker = gpu_node if rng.random() < 0.5 else node
            nodes[f"node_{i}"] = maker(
                f"node_{i}",
                cpu_slots=rng.randint(0, 4),
                gpu_slots=rng.randint(0, 2) if maker is gpu_node else 0,
                cpu_score=rng.choice([None, 1.0, 5.0]),
            )
        policy = rng.choice(POLICIES)
        for j in range(60):
            snapshot = snap(
                *nodes.values(), config=PlannerConfig(max_attempts_per_node=cap)
            )
            request = req(
                job_id=f"job_{j:026d}",
                gpu=rng.choice(["none", "preferred", "required"]),
            )
            decision = plan(request, snapshot, policy)
            if decision.chosen is None:
                continue
            n = nodes[decision.chosen.node_id]
            slot = decision.chosen.slot
            assert slot is not None
            nodes[n.node_id] = dataclasses.replace(
                n,
                used_cpu_slots=n.used_cpu_slots + (slot == "cpu"),
                used_gpu_slots=n.used_gpu_slots + (slot == "gpu"),
                live_attempts=n.live_attempts + 1,
            )
            now = nodes[n.node_id]
            assert now.used_cpu_slots <= now.cpu_slots
            assert now.used_gpu_slots <= now.gpu_slots
            assert now.live_attempts <= cap
            if request.gpu == "required":
                assert slot == "gpu"


# -------------------------------------------------- old agent / Hello (D105)


def test_old_agent_without_slot_fields_is_one_cpu_slot_and_no_gpu_slot() -> None:
    assert effective_slots(None, None, capabilities=EXEC, gpu_count=0) == (1, 0)
    assert effective_slots(None, None, capabilities=EXEC, gpu_count=2) == (
        1,
        0,
    )  # no exec-gpu
    assert effective_slots(None, None, capabilities=EXEC_GPU, gpu_count=0) == (1, 0)
    assert effective_slots(None, None, capabilities=EXEC_GPU, gpu_count=2) == (1, 1)


def test_advertised_zero_is_respected_and_a_gpu_slot_needs_a_usable_gpu() -> None:
    assert effective_slots(0, 0, capabilities=EXEC_GPU, gpu_count=1) == (0, 0)
    assert effective_slots(3, 7, capabilities=EXEC, gpu_count=1) == (3, 0)
    assert effective_slots(3, 7, capabilities=EXEC_GPU, gpu_count=0) == (3, 0)
    assert effective_slots(-5, -5, capabilities=EXEC_GPU, gpu_count=1) == (0, 0)


def test_node_snapshot_old_agent_hardware_json_roundtrip() -> None:
    hardware = {
        "capabilities": ["exec", "exec-gpu", "exec-docker"],
        "gpus": [{"index": 0, "vram_bytes": str(8 * GIB)}],
        "ram_bytes": str(16 * GIB),
        "cpu_threads": 8,
    }
    snapshot = node_snapshot(
        "node_a", state="online", connected=True, hardware=hardware
    )
    assert (snapshot.cpu_slots, snapshot.gpu_slots) == (1, 1)
    assert snapshot.gpu_vram_bytes == (8 * GIB,)
    # junk hardware never raises and never makes a node look capable
    for junk in (
        {},
        {"capabilities": "exec"},
        {"capabilities": [1, None], "gpus": "x"},
        {"gpus": [None, 3, {"vram_bytes": "nope"}]},
    ):
        s = node_snapshot("node_a", state="online", connected=True, hardware=junk)
        assert "exec" not in s.capabilities


@pytest.mark.parametrize(
    "scores",
    [
        None,
        {},
        {"metrics": "x"},
        {"metrics": [None, 3, {"name": "cpu_fp32_gflops_all"}]},
        {"metrics": [{"name": "cpu_fp32_gflops_all", "status": "failed", "mean": 9}]},
        {"metrics": [{"name": "cpu_fp32_gflops_all", "status": "ok", "mean": 0}]},
        {"metrics": [{"name": "cpu_fp32_gflops_all", "status": "ok", "mean": -4}]},
        {"metrics": [{"name": "cpu_fp32_gflops_all", "status": "ok", "mean": True}]},
        {"metrics": [{"name": "cpu_fp32_gflops_all", "status": "ok", "mean": "9"}]},
    ],
)
def test_unmeasured_or_bad_scores_are_none_never_zero(scores: Any) -> None:
    assert scores_of(scores) == (None, None)


# ----------------------------------------------------- decisions / honesty


def test_decision_json_is_bounded_and_keeps_the_chosen_and_feasible_first() -> None:
    nodes = [
        node(f"node_{i:03d}", state="offline" if i % 2 else "online")
        for i in range(200)
    ]
    decision = plan(req(), snap(*nodes), "cost_model")
    rows = candidates_json(decision)
    assert len(rows) == MAX_DECISION_CANDIDATES == 64
    assert rows[0]["rank"] == 1 and rows[0]["node_id"] == decision.chosen.node_id  # type: ignore[union-attr]
    assert (
        sum(1 for r in rows if r["feasible"]) == 64
    )  # 100 feasible > 64: rejections dropped
    json.dumps(rows)  # serialisable for JSONB


def test_every_estimate_term_has_a_source_and_defaults_are_never_measured() -> None:
    request = req(trees=(TreeNeed(TREE, 5_000_000),))
    decision = plan(request, snap(node("node_a"), node("node_b")), "cost_model")
    for row in candidates_json(decision):
        terms = row["estimate"]["terms"]
        assert set(terms) == {"queue", "transfer", "env", "compute", "output"}
        for name, term in terms.items():
            assert term["source"] in {s.value for s in TermSource}, name
        # nothing recorded for this workload: compute and output are assumed
        assert terms["compute"]["source"] == "assumed"
        assert terms["output"]["source"] == "assumed"
        assert terms["env"]["source"] == "assumed"
        # bytes to fetch over a default bandwidth: an assumption, not a measurement
        assert terms["transfer"]["source"] == "assumed"
        assert terms["transfer"]["detail"]["bw_source"] == "assumed"
        assert terms["queue"]["source"] == "measured"


def test_compute_history_scaled_and_unscaled_are_labelled_truthfully() -> None:
    history = History(by_node_s={"node_a": 100.0}, samples_by_node={"node_a": 4})
    request = req(fingerprint="f" * 64, history=history, trees=())
    a = node("node_a", cpu_score=100.0)
    slower = node("node_b", cpu_score=50.0)
    unknown = node("node_c")
    decision = plan(request, snap(a, slower, unknown), "cost_model")
    terms = {c.node_id: c.estimate.compute for c in decision.candidates if c.estimate}
    assert terms["node_a"].source is TermSource.HISTORY
    assert terms["node_a"].seconds == 100.0
    # half the score -> twice the time, flagged as scaled from node_a
    assert terms["node_b"].source is TermSource.SCALED
    assert terms["node_b"].seconds == pytest.approx(200.0)
    assert terms["node_b"].detail["from_node_id"] == "node_a"
    # no score to scale by: the other node's number is shown as history of ANOTHER node,
    # never as this node's own and never as measured
    assert terms["node_c"].source is not TermSource.MEASURED
    assert terms["node_c"].detail.get("from_node_id") == "node_a"
    assert terms["node_c"].detail.get("scaled") is False


def test_transfer_is_measured_zero_only_when_nothing_is_missing() -> None:
    request = req(trees=(TreeNeed(TREE, 7_000),))
    held = node("node_a", cached=frozenset({TREE}))
    cold = node("node_b")
    decision = plan(request, snap(held, cold), "cost_model")
    by_id = {c.node_id: c.estimate.transfer for c in decision.candidates if c.estimate}
    assert (by_id["node_a"].seconds, by_id["node_a"].source) == (
        0.0,
        TermSource.MEASURED,
    )
    assert by_id["node_b"].seconds > 0 and by_id["node_b"].source is TermSource.ASSUMED


def test_rejected_candidates_carry_a_reason_and_no_estimate() -> None:
    nodes = [
        node("node_a", capabilities=frozenset()),
        node("node_b", state="revoked"),
        node("node_c", connected=False),
        node("node_d"),
    ]
    decision = plan(req(), snap(*nodes), "cost_model")
    by_id = {c.node_id: c for c in decision.candidates}
    assert by_id["node_a"].rejection.code is RejectCode.NOT_EXEC  # type: ignore[union-attr]
    assert by_id["node_b"].rejection.code is RejectCode.REVOKED  # type: ignore[union-attr]
    assert by_id["node_c"].rejection.code is RejectCode.NO_SESSION  # type: ignore[union-attr]
    for rejected in ("node_a", "node_b", "node_c"):
        assert by_id[rejected].estimate is None and by_id[rejected].rank is None
        assert by_id[rejected].rejection.text  # type: ignore[union-attr]
    assert decision.chosen is not None and decision.chosen.node_id == "node_d"


def test_reason_key_ignores_text_and_order_but_follows_the_code_set() -> None:
    def waiting(state_a: str, state_b: str) -> str:
        nodes = [node("node_a", state=state_a), node("node_b", state=state_b)]
        return plan(req(), snap(*nodes), "cost_model").reason_key

    assert waiting("suspect", "offline") == waiting("suspect", "offline")
    # same code (not_online) with a different state text -> same key
    assert waiting("suspect", "suspect") == waiting("offline", "offline")
    # a different node becomes the offender -> different key
    only_a = plan(
        req(),
        snap(
            node("node_a", state="offline"),
            node("node_b", used_cpu_slots=2, live_attempts=2),
        ),
        "cost_model",
    )
    only_b = plan(
        req(),
        snap(
            node("node_a", used_cpu_slots=2, live_attempts=2),
            node("node_b", state="offline"),
        ),
        "cost_model",
    )
    assert only_a.reason_key != only_b.reason_key


def test_unknown_policy_is_refused_not_silently_defaulted() -> None:
    with pytest.raises(ValueError):
        plan(req(), snap(node("node_a")), "fastest")
    with pytest.raises(ValueError):
        plan(req(), snap(), "nonsense")


def test_empty_cluster_is_all_static_rejection_and_pinned_missing_node_has_no_candidates() -> (
    None
):
    d = plan(req(), snap(), "cost_model")
    assert d.chosen is None and d.candidates == () and d.all_static_rejections
    pinned = plan(req(pinned_node_id="node_zzz"), snap(node("node_a")), "cost_model")
    assert pinned.candidates == () and pinned.chosen is None


def test_pinned_job_has_exactly_one_candidate_and_ignores_policy() -> None:
    nodes = [node("node_a"), node("node_b", cpu_score=99.0)]
    for policy in POLICIES:
        d = plan(req(pinned_node_id="node_a"), snap(*nodes), policy)
        assert [c.node_id for c in d.candidates] == ["node_a"]
        assert d.chosen is not None and d.chosen.node_id == "node_a"


def test_local_candidate_is_marked_local_and_others_offload() -> None:
    d = plan(
        req(submitter_node_id="node_b"),
        snap(node("node_a"), node("node_b")),
        "cost_model",
    )
    assert {c.node_id: c.mode for c in d.candidates} == {
        "node_a": "offload",
        "node_b": "local",
    }


def test_known_bad_env_and_disk_are_live_rejections_not_static() -> None:
    bad = node("node_a", known_bad_envs=frozenset({"e" * 64}))
    small = node("node_b", disk_free_bytes=1000)
    d = plan(req(trees=(TreeNeed(TREE, 10_000),)), snap(bad, small), "cost_model")
    codes = {c.node_id: c.rejection.code for c in d.candidates if c.rejection}
    assert codes == {
        "node_a": RejectCode.ENV_KNOWN_BAD,
        "node_b": RejectCode.DISK_SHORT,
    }
    assert not d.all_static_rejections


def test_revoked_only_feasible_node_is_an_all_static_decision() -> None:
    gpu_job = req(gpu="required")
    nodes = [gpu_node("node_a", state="revoked"), node("node_b")]
    d = plan(gpu_job, snap(*nodes), "cost_model")
    assert d.chosen is None and d.all_static_rejections
    # the same job with the GPU node merely offline is NOT static: it waits
    d2 = plan(
        gpu_job, snap(gpu_node("node_a", state="offline"), node("node_b")), "cost_model"
    )
    assert d2.chosen is None and not d2.all_static_rejections


def test_unknown_resources_are_never_held_against_a_node() -> None:
    n = node("node_a", ram_bytes=None, cpu_threads=None)
    big = req(ram_bytes=1024 * GIB, cpus=512.0)
    assert plan(big, snap(n), "cost_model").chosen is not None
    small_ram = node("node_b", ram_bytes=1 * GIB)
    d = plan(big, snap(small_ram), "cost_model")
    assert d.candidates[0].rejection.code is RejectCode.RAM_SHORT  # type: ignore[union-attr]


def test_vram_boundaries_exactly_enough_and_one_byte_short() -> None:
    need = 8 * GIB
    ok = gpu_node("node_a", gpu_vram_bytes=(need,))
    short = gpu_node("node_b", gpu_vram_bytes=(need - 1,))
    d = plan(req(gpu="required", vram_bytes=need), snap(ok, short), "cost_model")
    codes = {
        c.node_id: (c.rejection.code if c.rejection else None) for c in d.candidates
    }
    assert codes == {"node_a": None, "node_b": RejectCode.VRAM_SHORT}
