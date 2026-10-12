"""V0.6 G2: the pure planner (D102-D107; checklist 16, 18).

Snapshots are built by hand - no database, no event loop. Covered: every
rejection code (static and live), slot choice, every policy on a crafted
cluster where it picks a *different* node, Local vs Mode 1, the estimator's
term sources, determinism (incl. seeded ``random``), and a seeded property test
(thousands of random fabrics): the chosen node is always feasible.
"""

from __future__ import annotations

import ast
import dataclasses
import random
from pathlib import Path
from typing import Any

import pytest

from meshforge.common.manifest import POLICIES
from meshforge.common.owner_policy import OPEN_POLICY, OwnerPolicy
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
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.feasibility import (
    choose_slot,
    live_rejection,
    static_rejection,
)
from meshforge.controller.planner.types import STATIC_CODES

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
        "cpu_slots": 1,
        "gpu_slots": 0,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def gpu_node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "capabilities": EXEC_GPU,
        "gpu_vram_bytes": (4 * GIB,),
        "gpu_slots": 1,
    }
    base.update(kw)
    return node(node_id, **base)


def request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job_01J0000000000000000000000",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e" * 64,
        "trees": (TreeNeed(TREE, 100 * 1024 * 1024),),
    }
    base.update(kw)
    return PlanRequest(**base)


def snap(*nodes: NodeSnapshot, **kw: Any) -> ClusterSnapshot:
    return ClusterSnapshot(nodes=tuple(sorted(nodes, key=lambda n: n.node_id)), **kw)


def chosen(decision: Any) -> str | None:
    return None if decision.chosen is None else decision.chosen.node_id


# ------------------------------------------------------------------ feasibility


def _OPEN_SUB(**kw: Any) -> OwnerPolicy:  # noqa: N802 - reads like a constant
    """An open owner policy with ``kw`` changed (V0.7 G5)."""
    return dataclasses.replace(OPEN_POLICY, **kw)


_GPU_KW: dict[str, Any] = {
    "capabilities": EXEC_GPU,
    "gpu_vram_bytes": (4 * GIB,),
    "gpu_slots": 1,
}


STATIC_CASES: list[tuple[RejectCode, dict[str, Any], dict[str, Any]]] = [
    (RejectCode.REVOKED, {"state": "revoked"}, {}),
    (RejectCode.NOT_EXEC, {"capabilities": frozenset({"docker"})}, {}),
    (RejectCode.NO_GPU, {}, {"gpu": "required"}),
    (
        RejectCode.NO_GPU_RUNTIME,
        {"gpu_vram_bytes": (4 * GIB,), "gpu_slots": 1},
        {"gpu": "required"},
    ),
    (
        RejectCode.NO_GPU_SLOTS,
        {"capabilities": EXEC_GPU, "gpu_vram_bytes": (4 * GIB,), "gpu_slots": 0},
        {"gpu": "required"},
    ),
    (RejectCode.NO_CPU_SLOTS, {"cpu_slots": 0}, {}),
    (RejectCode.NO_CPU_SLOTS, {"cpu_slots": 0}, {"gpu": "preferred"}),
    (
        RejectCode.VRAM_SHORT,
        {"capabilities": EXEC_GPU, "gpu_vram_bytes": (4 * GIB,), "gpu_slots": 1},
        {"gpu": "required", "vram_bytes": 4 * GIB + 1},
    ),
    (RejectCode.RAM_SHORT, {}, {"ram_bytes": 16 * GIB + 1}),
    (RejectCode.CPUS_SHORT, {}, {"cpus": 8.5}),
    (
        RejectCode.NO_RUNTIME_FOR_BASE,
        {"capabilities": frozenset({"exec", "exec-venv"})},
        {"base": "cuda-pytorch"},
    ),
    # V0.7 G5 (D142): the owner's policy (each code also in test_v07_g5_*)
    (RejectCode.POLICY_NOT_ACCEPTING, {"policy": OwnerPolicy()}, {}),
    (
        RejectCode.POLICY_USER_NOT_ALLOWED,
        {"policy": _OPEN_SUB(allowed_users=("alice",))},
        {"owner_name": "bob"},
    ),
    (RejectCode.POLICY_UNSANDBOXED, {"policy": _OPEN_SUB(allow_unsandboxed=False)}, {}),
    (
        RejectCode.POLICY_NETWORK,
        {"policy": _OPEN_SUB(allow_network=False)},
        {"network": True},
    ),
    (
        RejectCode.POLICY_VRAM,
        {"policy": _OPEN_SUB(max_vram_bytes=GIB), **_GPU_KW},
        {"gpu": "required", "vram_bytes": GIB + 1},
    ),
    (
        RejectCode.POLICY_RAM,
        {"policy": _OPEN_SUB(max_ram_bytes=GIB)},
        {"ram_bytes": GIB + 1},
    ),
    (RejectCode.POLICY_CPU, {"policy": _OPEN_SUB(max_cpu_percent=50)}, {"cpus": 4.5}),
]


@pytest.mark.parametrize(("code", "node_kw", "req_kw"), STATIC_CASES)
def test_every_static_rejection(
    code: RejectCode, node_kw: dict[str, Any], req_kw: dict[str, Any]
) -> None:
    refused = static_rejection(request(**req_kw), node("n1", **node_kw))
    assert refused is not None and refused.code is code
    assert refused.static and refused.text
    decision = plan(request(**req_kw), snap(node("n1", **node_kw)), "cost_model")
    assert decision.chosen is None and decision.all_static_rejections


def test_static_bounds_accept_exactly_enough() -> None:
    accepted = request(gpu="required", vram_bytes=4 * GIB, ram_bytes=16 * GIB, cpus=8.0)
    assert static_rejection(accepted, gpu_node("n1")) is None
    # a value the node did not report is not held against it
    unknown = node("n2", ram_bytes=None, cpu_threads=None)
    assert static_rejection(request(ram_bytes=999 * GIB, cpus=99.0), unknown) is None
    unknown_vram = gpu_node("n3", gpu_vram_bytes=(0,))
    assert (
        static_rejection(request(gpu="required", vram_bytes=99 * GIB), unknown_vram)
        is None
    )
    # cuda-pytorch is fine with docker or subprocess next to venv
    both = node("n4", capabilities=frozenset({"exec", "exec-venv", "exec-docker"}))
    assert static_rejection(request(base="cuda-pytorch"), both) is None


LIVE_CASES: list[tuple[RejectCode, dict[str, Any], dict[str, Any]]] = [
    (RejectCode.NOT_ONLINE, {"state": "suspect"}, {}),
    (RejectCode.NOT_ONLINE, {"state": "offline"}, {}),
    (RejectCode.NOT_ONLINE, {"state": "draining"}, {}),
    (RejectCode.NO_SESSION, {"connected": False}, {}),
    (RejectCode.ENV_KNOWN_BAD, {"known_bad_envs": frozenset({"e" * 64})}, {}),
    (RejectCode.NODE_CAP, {"live_attempts": 8, "cpu_slots": 9}, {}),
    (RejectCode.SLOTS_FULL, {"used_cpu_slots": 1}, {}),
    (RejectCode.DISK_SHORT, {"disk_free_bytes": 100 * 1024 * 1024 - 1}, {}),
]


@pytest.mark.parametrize(("code", "node_kw", "req_kw"), LIVE_CASES)
def test_every_live_rejection(
    code: RejectCode, node_kw: dict[str, Any], req_kw: dict[str, Any]
) -> None:
    n = node("n1", **node_kw)
    assert static_rejection(request(**req_kw), n) is None
    refused = live_rejection(request(**req_kw), n, max_attempts_per_node=8, local=False)
    assert refused is not None and refused.code is code and not refused.static
    decision = plan(request(**req_kw), snap(n), "cost_model")
    assert decision.chosen is None and not decision.all_static_rejections


def test_every_reject_code_has_a_case() -> None:
    covered = {c for c, _, _ in STATIC_CASES} | {c for c, _, _ in LIVE_CASES}
    # V0.6 G3: retry_excluded is soft (lifted on a one-node fabric), so its
    # cases need two nodes - tests/unit/controller/test_v06_g3_retry_planner.py
    covered |= {RejectCode.RETRY_EXCLUDED}
    assert covered == set(RejectCode)
    assert {c for c, _, _ in STATIC_CASES} == STATIC_CODES


def test_disk_enough_and_cached_tree_needs_no_disk() -> None:
    exact = node("n1", disk_free_bytes=100 * 1024 * 1024)
    assert (
        live_rejection(request(), exact, max_attempts_per_node=8, local=False) is None
    )
    cached = node("n2", disk_free_bytes=0, cached=frozenset({TREE}))
    assert (
        live_rejection(request(), cached, max_attempts_per_node=8, local=False) is None
    )
    seeded_local = node("n3", disk_free_bytes=0)
    assert (
        live_rejection(
            request(seeded=True), seeded_local, max_attempts_per_node=8, local=True
        )
        is None
    )


def test_slot_choice() -> None:
    g = gpu_node("g", cpu_slots=1)
    assert choose_slot(request(gpu="required"), g) == "gpu"
    assert choose_slot(request(gpu="preferred"), g) == "gpu"
    assert choose_slot(request(gpu="none"), g) == "cpu"
    busy_gpu = dataclasses.replace(g, used_gpu_slots=1)
    assert choose_slot(request(gpu="required"), busy_gpu) is None
    assert choose_slot(request(gpu="preferred"), busy_gpu) == "cpu"
    all_busy = dataclasses.replace(busy_gpu, used_cpu_slots=1)
    assert choose_slot(request(gpu="preferred"), all_busy) is None
    # a GPU the node cannot pass to a job is never a GPU slot
    no_runtime = node("x", gpu_vram_bytes=(4 * GIB,), gpu_slots=1)
    assert choose_slot(request(gpu="preferred"), no_runtime) == "cpu"


def test_gpu_job_takes_a_gpu_slot_and_cpu_job_a_cpu_slot() -> None:
    g = gpu_node("g", cpu_slots=1)
    d_gpu = plan(request(gpu="required"), snap(g), "cost_model")
    d_cpu = plan(request(), snap(g), "cost_model")
    assert d_gpu.chosen is not None and d_gpu.chosen.slot == "gpu"
    assert d_cpu.chosen is not None and d_cpu.chosen.slot == "cpu"


# ------------------------------------------------------------------ candidates


def test_local_and_offload_candidates_and_pinned() -> None:
    fabric = snap(node("a"), node("b"), node("c"))
    decision = plan(request(submitter_node_id="b"), fabric, "least_loaded")
    modes = {c.node_id: c.mode for c in decision.candidates}
    assert modes == {"a": "offload", "b": "local", "c": "offload"}
    pinned = plan(request(pinned_node_id="c"), fabric, "least_loaded")
    assert [c.node_id for c in pinned.candidates] == ["c"]
    assert chosen(pinned) == "c"
    absent = plan(request(pinned_node_id="zz"), fabric, "least_loaded")
    assert absent.candidates == () and absent.chosen is None


def test_candidates_ordered_ranked_first_then_rejected_by_node_id() -> None:
    fabric = snap(
        node("d", state="offline"),
        node("c"),
        node("b", capabilities=frozenset()),
        node("a", used_cpu_slots=1, cpu_slots=2),
    )
    decision = plan(request(), fabric, "least_loaded")
    assert [c.node_id for c in decision.candidates] == ["c", "a", "b", "d"]
    assert [c.rank for c in decision.candidates] == [1, 2, None, None]


# ------------------------------------------------------------------ policies


def _scores(cpu: float | None = None, gpu: float | None = None) -> dict[str, Any]:
    return {"cpu_score": cpu, "gpu_score": gpu}


def crafted() -> ClusterSnapshot:
    """Four feasible nodes; each policy prefers a different one (criterion 5)."""
    return snap(
        # strongest CPU, but busy and holding nothing
        node(
            "n_strong",
            cpu_slots=4,
            used_cpu_slots=3,
            cpu_percent=90.0,
            ram_free_bytes=1 * GIB,
            **_scores(cpu=500.0),
        ),
        # idle and lightly loaded
        node(
            "n_idle",
            cpu_slots=1,
            cpu_percent=5.0,
            ram_free_bytes=2 * GIB,
            **_scores(cpu=50.0),
        ),
        # lots of free RAM and slots
        node(
            "n_roomy",
            cpu_slots=8,
            used_cpu_slots=1,
            cpu_percent=40.0,
            ram_free_bytes=60 * GIB,
            **_scores(cpu=100.0),
        ),
        # holds the project tree and the input
        node(
            "n_cache",
            cpu_slots=2,
            used_cpu_slots=1,
            cpu_percent=30.0,
            ram_free_bytes=1 * GIB,
            cached=frozenset({TREE, INPUT}),
            **_scores(cpu=60.0),
        ),
    )


def policy_request(**kw: Any) -> PlanRequest:
    return request(
        trees=(TreeNeed(TREE, 2 * GIB), TreeNeed(INPUT, 1 * GIB)),
        history=History(by_node_s={"n_idle": 10.0}, samples_by_node={"n_idle": 3}),
        **kw,
    )


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ("least_loaded", "n_idle"),
        ("strongest_suitable", "n_strong"),
        ("resource_score", "n_roomy"),
        ("cache_affinity", "n_cache"),
        ("cost_model", "n_cache"),
        ("round_robin", "n_idle"),
    ],
)
def test_each_policy_picks_its_node(policy: str, expected: str) -> None:
    # round_robin: the last pick was n_cache, so the next in id order is n_idle
    fabric = dataclasses.replace(crafted(), round_robin_after="n_cache")
    decision = plan(policy_request(), fabric, policy)
    assert chosen(decision) == expected, [
        (c.node_id, c.score, c.rank) for c in decision.candidates
    ]
    assert decision.policy == policy


def test_cost_model_prefers_the_measured_fast_node_when_nothing_is_cached() -> None:
    fabric = snap(
        *[dataclasses.replace(n, cached=frozenset()) for n in crafted().nodes]
    )
    decision = plan(policy_request(), fabric, "cost_model")
    # history says n_idle runs it in 10 s; scaled estimates elsewhere are
    # 10 s x (50 / score) -> n_strong is 1 s; transfers are equal.
    assert chosen(decision) == "n_strong"
    by_node = {c.node_id: c for c in decision.candidates}
    assert by_node["n_idle"].estimate is not None
    assert by_node["n_idle"].estimate.compute.source is TermSource.HISTORY
    assert by_node["n_strong"].estimate is not None
    assert by_node["n_strong"].estimate.compute.source is TermSource.SCALED
    assert by_node["n_strong"].estimate.compute.seconds == pytest.approx(1.0)


def test_round_robin_walks_the_cursor_and_wraps() -> None:
    fabric = snap(node("a"), node("b"), node("c"))
    picks = []
    cursor = None
    for _ in range(5):
        decision = plan(
            request(),
            dataclasses.replace(fabric, round_robin_after=cursor),
            "round_robin",
        )
        cursor = chosen(decision)
        picks.append(cursor)
    assert picks == ["a", "b", "c", "a", "b"]


def test_random_is_reproducible_per_job_and_attempt() -> None:
    fabric = snap(*[node(f"n{i}") for i in range(8)])
    first = plan(request(), fabric, "random")
    assert plan(request(), fabric, "random") == first
    picks = {
        chosen(plan(request(attempt_no=k), fabric, "random")) for k in range(1, 30)
    }
    assert len(picks) > 1  # another attempt draws again
    jobs = {
        chosen(plan(request(job_id=f"job_{k:026d}"), fabric, "random"))
        for k in range(30)
    }
    assert len(jobs) > 1


def test_strongest_ranks_unmeasured_last_and_uses_gpu_score_for_gpu_slots() -> None:
    fabric = snap(
        gpu_node("g_fast", **_scores(cpu=10.0, gpu=20.0)),
        gpu_node("g_slow", **_scores(cpu=900.0, gpu=5.0)),
        gpu_node("g_none"),
    )
    gpu_pick = plan(request(gpu="required"), fabric, "strongest_suitable")
    assert [c.node_id for c in gpu_pick.candidates] == ["g_fast", "g_slow", "g_none"]
    cpu_pick = plan(request(), fabric, "strongest_suitable")
    assert chosen(cpu_pick) == "g_slow"


def test_ties_break_on_node_id_for_every_policy() -> None:
    fabric = snap(node("b"), node("a"), node("c"))
    for policy in POLICIES:
        if policy == "random":
            continue
        assert chosen(plan(request(), fabric, policy)) == "a", policy


def test_unknown_policy_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown placement policy"):
        plan(request(), snap(node("a")), "fastest")


# ------------------------------------------------------------------ Local / estimate


def test_seeded_local_wins_a_transfer_dominated_job_under_cost_model() -> None:
    fabric = snap(
        node("local", **_scores(cpu=10.0)),
        node("remote", **_scores(cpu=1000.0)),
    )
    big_tree = request(
        trees=(TreeNeed(TREE, 5 * GIB),), submitter_node_id="local", seeded=True
    )
    decision = plan(big_tree, fabric, "cost_model")
    assert chosen(decision) == "local"
    assert decision.chosen is not None and decision.chosen.mode == "local"
    local = decision.chosen.estimate
    assert local is not None
    assert local.transfer.seconds == 0 and local.transfer.source is TermSource.MEASURED
    remote = next(c for c in decision.candidates if c.node_id == "remote")
    assert remote.estimate is not None
    assert remote.estimate.transfer.source is TermSource.ASSUMED
    assert remote.estimate.transfer.detail["missing_bytes"] == 5 * GIB
    # not seeded: Local has to fetch it like everyone -> no free advantage
    unseeded = plan(dataclasses.replace(big_tree, seeded=False), fabric, "cost_model")
    assert all(
        c.estimate is not None and c.estimate.transfer.seconds > 0
        for c in unseeded.candidates
    )


def test_estimate_terms_all_carry_a_source_and_assumed_defaults() -> None:
    config = PlannerConfig(default_bw_bytes_s=1e6, default_compute_s=42.0)
    decision = plan(
        request(trees=(TreeNeed(TREE, 3_000_000),)),
        snap(node("a"), config=config),
        "cost_model",
    )
    assert decision.chosen is not None and decision.chosen.estimate is not None
    est = decision.chosen.estimate
    assert est.compute.seconds == 42.0 and est.compute.source is TermSource.ASSUMED
    assert est.transfer.seconds == pytest.approx(3.0)
    assert est.total_s == pytest.approx(45.0)
    for term in est.terms().values():
        assert isinstance(term.source, TermSource)
    rows = candidates_json(decision)
    assert rows[0]["estimate"]["terms"]["compute"]["source"] == "assumed"
    assert rows[0]["estimate"]["total_s"] == pytest.approx(45.0)


def test_unscaled_history_from_another_node_says_so() -> None:
    fabric = snap(node("a"), node("b"))
    decision = plan(
        request(history=History(by_node_s={"b": 7.0}, samples_by_node={"b": 2})),
        fabric,
        "least_loaded",
    )
    a = next(c for c in decision.candidates if c.node_id == "a")
    assert a.estimate is not None
    assert a.estimate.compute.source is TermSource.HISTORY
    assert a.estimate.compute.detail["scaled"] is False
    assert a.estimate.compute.detail["from_node_id"] == "b"


# ------------------------------------------------------------------ determinism


def test_same_inputs_same_decision_and_digest() -> None:
    for policy in POLICIES:
        one = plan(policy_request(), crafted(), policy)
        two = plan(policy_request(), crafted(), policy)
        assert one == two
    a = plan(policy_request(), crafted(), "cost_model")
    other = dataclasses.replace(crafted(), round_robin_after="n_idle")
    b = plan(policy_request(), other, "cost_model")
    assert a.snapshot_digest != b.snapshot_digest
    assert a.reason_key == b.reason_key  # same feasibility, same reasons


def test_reason_key_changes_only_with_the_reason_set() -> None:
    busy = snap(node("a", used_cpu_slots=1), node("b", state="suspect"))
    k1 = plan(request(), busy, "cost_model").reason_key
    k2 = plan(
        request(), dataclasses.replace(busy, round_robin_after="a"), "cost_model"
    ).reason_key
    k3 = plan(request(), snap(node("a", used_cpu_slots=1), node("b")), "cost_model")
    assert k1 == k2
    assert k3.reason_key != k1


# ------------------------------------------------------------------ property


def _random_node(rng: random.Random, i: int) -> NodeSnapshot:
    caps = set(
        rng.sample(["exec", "exec-docker", "exec-gpu", "exec-venv"], rng.randint(0, 4))
    )
    gpus = tuple(rng.choice([0, 2, 4, 8]) * GIB for _ in range(rng.randint(0, 2)))
    cpu_slots, gpu_slots = rng.randint(0, 3), rng.randint(0, 2)
    return NodeSnapshot(
        node_id=f"n{i:02d}",
        state=rng.choice(
            ["online", "online", "online", "suspect", "offline", "revoked"]
        ),
        connected=rng.random() < 0.85,
        capabilities=frozenset(caps),
        ram_bytes=rng.choice([None, 4 * GIB, 16 * GIB]),
        cpu_threads=rng.choice([None, 2, 8]),
        gpu_vram_bytes=gpus,
        cpu_slots=cpu_slots,
        gpu_slots=gpu_slots,
        used_cpu_slots=rng.randint(0, cpu_slots),
        used_gpu_slots=rng.randint(0, gpu_slots),
        live_attempts=rng.randint(0, 5),
        cpu_percent=rng.choice([None, 1.0, 50.0, 99.0]),
        ram_free_bytes=rng.choice([None, GIB, 8 * GIB]),
        disk_free_bytes=rng.choice([None, 0, 10 * GIB]),
        cpu_score=rng.choice([None, 10.0, 100.0]),
        gpu_score=rng.choice([None, 5.0, 20.0]),
        cached=frozenset(rng.sample([TREE, INPUT], rng.randint(0, 2))),
        known_bad_envs=frozenset({"e" * 64} if rng.random() < 0.1 else ()),
    )


def test_property_chosen_node_is_always_feasible() -> None:
    rng = random.Random(20261005)  # noqa: S311 - a seeded test generator
    chose = 0
    for trial in range(3000):
        nodes = [_random_node(rng, i) for i in range(rng.randint(0, 7))]
        ids = [n.node_id for n in nodes]
        req = request(
            attempt_no=rng.randint(1, 5),
            gpu=rng.choice(["none", "preferred", "required"]),
            vram_bytes=rng.choice([None, 3 * GIB, 6 * GIB]),
            ram_bytes=rng.choice([None, 8 * GIB]),
            cpus=rng.choice([None, 4.0]),
            base=rng.choice(["python-cpu", "cuda-pytorch"]),
            trees=(TreeNeed(TREE, rng.randint(0, 3 * GIB)), TreeNeed(INPUT, GIB)),
            submitter_node_id=rng.choice([None, *ids]) if ids else None,
            seeded=rng.random() < 0.5,
            pinned_node_id=rng.choice([None, None, None, *ids]) if ids else None,
            history=History(
                by_node_s={i: rng.uniform(1, 100) for i in ids if rng.random() < 0.3}
            ),
        )
        if req.gpu == "none":
            req = dataclasses.replace(req, vram_bytes=None)
        policy = rng.choice(POLICIES)
        cap = rng.randint(1, 6)
        fabric = snap(
            *nodes,
            round_robin_after=rng.choice([None, *ids]) if ids else None,
            config=PlannerConfig(max_attempts_per_node=cap),
        )
        decision = plan(req, fabric, policy)
        assert decision == plan(req, fabric, policy), trial  # deterministic
        expected = 1 if req.pinned_node_id is not None else len(nodes)
        assert len(decision.candidates) == expected, trial
        feasible = [c for c in decision.candidates if c.feasible]
        assert sorted(c.rank for c in feasible) == list(range(1, len(feasible) + 1))
        for c in decision.candidates:
            assert c.feasible == (c.rejection is None), trial
            assert c.feasible == (c.estimate is not None) == (c.slot is not None)
        if decision.chosen is None:
            assert not feasible
            continue
        chose += 1
        pick = fabric.node(decision.chosen.node_id)
        assert pick is not None
        local = pick.node_id == req.submitter_node_id
        assert static_rejection(req, pick) is None, trial
        assert (
            live_rejection(req, pick, max_attempts_per_node=cap, local=local) is None
        ), trial
        assert decision.chosen.rank == 1
        assert decision.chosen.slot == choose_slot(req, pick)
        if decision.chosen.slot == "gpu":
            assert pick.free_gpu_slots > 0 and req.gpu != "none"
        else:
            assert pick.free_cpu_slots > 0 and req.gpu != "required"
        assert pick.live_attempts < cap
        assert req.env_key not in pick.known_bad_envs
    assert chose > 100  # the generator exercised real choices


# ------------------------------------------------------------------ purity


def test_planner_imports_nothing_but_stdlib_common_and_itself() -> None:
    import meshforge.controller.planner as package

    root = Path(package.__file__).parent
    allowed_external = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "json",
        "math",
        "random",
        "typing",
    }
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for item in ast.walk(tree):
            names: list[str] = []
            if isinstance(item, ast.Import):
                names = [a.name for a in item.names]
            elif isinstance(item, ast.ImportFrom) and item.module:
                names = [item.module]
            for name in names:
                top = name.split(".")[0]
                if top == "meshforge":
                    assert name.startswith(
                        ("meshforge.common", "meshforge.controller.planner")
                    ), f"{path.name} imports {name}"
                else:
                    assert top in allowed_external, f"{path.name} imports {name}"
