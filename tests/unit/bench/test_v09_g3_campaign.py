"""V0.9 G3 lane B item 1: campaign spec + deterministic expansion (0.9.3, D216)."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Any

import pytest

from meshforge.bench.campaign import (
    HARD_MAX_RUNS,
    MAX_SPEC_BYTES,
    CampaignSpec,
    CampaignSpecError,
    NodeRef,
    Range,
    Sampler,
    Stream,
    expand,
    load_spec,
    parse_spec,
    runs_json,
    spec_digest,
)
from meshforge.bench.campaign import _points as points_of

pytestmark = pytest.mark.unit

NODES = [NodeRef("node_b", "laptop-b"), NodeRef("node_a", "laptop-a")]

BASE: dict[str, Any] = {
    "name": "unit-campaign",
    "seed": 7,
    "families": [
        {
            "family": "cpu.matmul",
            "params": {
                "n": 2048,
                "repeats": {"min": 22, "max": 60, "int": True, "steps": 3},
            },
        },
        {"family": "cpu.sort"},
    ],
    "strategies": ["local", "mode1"],
    "origin": "laptop-a",
    "repetitions": 3,
    "budget": {"max_runs": 1000, "max_hours": 10},
}


def spec(**changes: Any) -> CampaignSpec:
    return parse_spec({**BASE, **changes})


def problems(**changes: Any) -> list[str]:
    with pytest.raises(CampaignSpecError) as caught:
        expand(spec(**changes), NODES)
    return caught.value.problems


# ------------------------------------------------------------ determinism


def test_same_spec_same_nodes_gives_identical_bytes() -> None:
    first = runs_json(expand(spec(), NODES).runs)
    second = runs_json(expand(spec(), list(reversed(NODES))).runs)
    assert first == second
    assert len(first) > 100


def test_golden_expansion_digest_is_pinned() -> None:
    """A change to the expansion algorithm (order, ids, seeds) changes every
    campaign's run list: it must be deliberate (update this digest and say so
    in the dataset version notes)."""
    text = runs_json(expand(spec(), NODES).runs)
    digest = hashlib.sha256(text.encode()).hexdigest()
    assert digest == GOLDEN, digest


GOLDEN = "7c931409cf3a900e112d6f2a7d91d3d7f7ccf7159724b36c0d0174d4d5ad2337"


def test_stream_is_pinned_across_python_versions() -> None:
    stream = Stream("pin", 1)
    assert [stream.below(1000) for _ in range(5)] == PINNED_DRAWS
    assert 0.0 <= Stream("x").uniform() < 1.0


PINNED_DRAWS: list[int] = [988, 872, 392, 362, 98]


def test_seed_change_changes_order_not_the_set() -> None:
    one = expand(spec(seed=1), NODES).runs
    two = expand(spec(seed=2), NODES).runs
    assert [r.config_id for r in one] != [r.config_id for r in two]
    assert Counter((r.config_id, r.repetition) for r in one) == Counter(
        (r.config_id, r.repetition) for r in two
    )


def test_spec_digest_ignores_comments_and_key_order() -> None:
    a = load_spec(
        "name: d\nseed: 1\nfamilies: [{family: cpu.sort}]\nbudget: {max_runs: 9}\n"
    )
    b = load_spec(
        "# a comment\nbudget: {max_runs: 9}\nfamilies:\n  - family: cpu.sort\n"
        "seed: 1\nname: d\n"
    )
    assert spec_digest(a) == spec_digest(b)
    assert spec_digest(a) != spec_digest(a.model_copy(update={"seed": 2}))


# ------------------------------------------------------------ structure


def test_interleaving_each_round_visits_every_config_once() -> None:
    expansion = expand(spec(), NODES)
    configs = expansion.configs
    assert len(expansion.runs) == configs * 3
    for round_no in range(3):
        chunk = expansion.runs[round_no * configs : (round_no + 1) * configs]
        assert {r.repetition for r in chunk} == {round_no + 1}
        assert len({r.config_id for r in chunk}) == configs
    assert [r.seq for r in expansion.runs] == list(range(1, len(expansion.runs) + 1))


def test_ids_and_seeds() -> None:
    runs = expand(spec(), NODES).runs
    by_group: dict[str, set[int]] = {}
    for run in runs:
        by_group.setdefault(run.group_id, set()).add(run.seed)
        assert run.group_id.startswith("grp_") and len(run.group_id) == 28
        assert run.config_id.startswith("cfg_") and len(run.config_id) == 28
    # one workload seed per group: every strategy runs on the same data
    assert all(len(seeds) == 1 for seeds in by_group.values())
    assert len(by_group) == 4  # 3 matmul points + 1 sort


def test_strategies_with_origin() -> None:
    runs = expand(spec(), NODES).runs
    assert {(r.strategy, r.node_id, r.origin_node_id) for r in runs} == {
        ("local", "node_a", "node_a"),
        ("mode1", "node_b", "node_a"),
    }


def test_without_origin_every_node_is_the_origin_in_turn() -> None:
    nodes = [*NODES, NodeRef("node_c")]
    runs = expand(spec(origin=None), nodes).runs
    triples = {(r.strategy, r.node_id, r.origin_node_id) for r in runs}
    assert len([t for t in triples if t[0] == "local"]) == 3
    assert len([t for t in triples if t[0] == "mode1"]) == 6  # ordered pairs
    assert all(t[1] == t[2] for t in triples if t[0] == "local")
    assert all(t[1] != t[2] for t in triples if t[0] == "mode1")


def test_params_are_complete_and_valid() -> None:
    runs = expand(spec(), NODES).runs
    matmul = {tuple(sorted(r.params.items())) for r in runs if r.family == "cpu.matmul"}
    assert {dict(p)["repeats"] for p in matmul} == {22, 41, 60}
    assert all(dict(p)["dtype"] == "float64" for p in matmul)  # default filled


# ------------------------------------------------------------ samplers


def test_lhs_uses_every_stratum_once_per_dimension() -> None:
    samples = 10
    lhs = spec(
        sampler="lhs",
        samples=samples,
        families=[
            {
                "family": "cpu.matmul",
                "params": {
                    "n": {"min": 0, "max": 10},
                    "repeats": {"min": 0, "max": 10},
                },
            }
        ],
    )
    pts = points_of(lhs, 0, lhs.families[0])
    assert len(pts) == samples
    for name in ("n", "repeats"):
        strata = sorted(min(int(p[name]), samples - 1) for p in pts)
        assert strata == list(range(samples)), name


def test_lhs_log_and_int_ranges_stay_in_bounds() -> None:
    lhs = spec(
        sampler="lhs",
        samples=8,
        on_invalid="drop",
        families=[
            {
                "family": "cpu.matmul",
                "params": {
                    "n": {"min": 1024, "max": 4096, "log": True, "int": True},
                    "repeats": [22, 40],
                },
            }
        ],
    )
    pts = points_of(lhs, 0, lhs.families[0])
    assert all(isinstance(p["n"], int) and 1024 <= p["n"] <= 4096 for p in pts)
    assert {p["repeats"] for p in pts} <= {22, 40}


def test_random_sampler_is_deterministic_and_counts_drops() -> None:
    random_spec = spec(
        sampler="random",
        samples=12,
        on_invalid="drop",
        families=[
            {
                "family": "cpu.matmul",
                "params": {"n": {"min": 256, "max": 4096, "int": True}},
            }
        ],
    )
    one = expand(random_spec, NODES)
    two = expand(random_spec, NODES)
    assert runs_json(one.runs) == runs_json(two.runs)
    assert one.dropped + one.duplicates + one.groups == 12
    assert one.dropped > 0  # n=256 x 22 repeats is under 5 s: dropped, counted
    assert len(one.problems) == one.dropped


def test_invalid_point_is_an_error_naming_it_by_default() -> None:
    found = problems(
        families=[{"family": "cpu.matmul", "params": {"n": [256, 2048]}}],
    )
    assert any('"n":256' in p and "5-180 s" in p for p in found)


def test_duplicate_points_merge() -> None:
    expansion = expand(
        spec(
            families=[
                {"family": "cpu.sort"},
                {"family": "cpu.sort", "params": {"n": 10_000_000}},
            ]
        ),
        NODES,
    )
    assert expansion.groups == 1 and expansion.duplicates == 1


# ------------------------------------------------------------ budget


def test_budget_max_runs_is_an_error_with_the_count() -> None:
    with pytest.raises(CampaignSpecError) as caught:
        expand(spec(budget={"max_runs": 5}), NODES)
    assert caught.value.details["runs"] == 24
    assert "24 runs" in caught.value.message


def test_budget_max_hours() -> None:
    with pytest.raises(CampaignSpecError) as caught:
        expand(spec(budget={"max_runs": 1000, "max_hours": 0.01}), NODES)
    assert caught.value.details["hours"] > 0.01
    assert "max_hours" in caught.value.message


def test_estimated_hours_is_positive_and_counted() -> None:
    expansion = expand(spec(), NODES)
    assert 0 < expansion.estimated_hours < 10


def test_max_runs_is_capped() -> None:
    with pytest.raises(CampaignSpecError):
        spec(budget={"max_runs": HARD_MAX_RUNS + 1})


# ------------------------------------------------------------ validation


@pytest.mark.parametrize(
    ("changes", "needle"),
    [
        (
            {"conditions": ["cpu_burn"]},
            "unknown condition 'cpu_burn'",
        ),  # G4: cpu_load exists
        ({"families": [{"family": "cpu.nope"}]}, "unknown workload family"),
        (
            {"families": [{"family": "cpu.sort", "params": {"m": 1}}]},
            "no such parameter",
        ),
        (
            {
                "families": [
                    {"family": "cpu.sort", "params": {"n": {"min": 1, "max": 2}}}
                ]
            },
            "needs steps under grid",
        ),
        ({"sampler": "lhs"}, "samples: required"),
        ({"samples": 4}, "only for the random and lhs"),
        ({"repetitions": 2}, "repetitions"),
        ({"strategies": ["mode2"]}, "strategies"),
        ({"name": "Bad Name"}, "name"),
        ({"strategies": ["local", "local"]}, "listed twice"),
        ({"families": [{"family": "cpu.sort", "params": {"n": []}}]}, "empty list"),
        ({"unknown_key": 1}, "unknown_key"),
        ({"seed": "7"}, "seed"),
    ],
)
def test_spec_errors_name_the_problem(changes: dict[str, Any], needle: str) -> None:
    with pytest.raises(CampaignSpecError) as caught:
        spec(**changes)
    assert any(needle in p for p in caught.value.problems), caught.value.problems


def test_range_rules() -> None:
    for bad in (
        {"min": 2, "max": 1},
        {"min": 0, "max": 1, "log": True},
        {"min": 0.5, "max": 2, "int": True},
        {"min": float("nan"), "max": 1},
    ):
        with pytest.raises(ValueError):
            Range.model_validate(bad)
    assert Range.model_validate({"min": 1, "max": 100, "log": True}).at(0.5) == 10.0


def test_node_resolution() -> None:
    assert problems(nodes=["laptop-z"], origin=None) == ["nodes[0]: no node 'laptop-z'"]
    found = problems(nodes=["laptop-b"])  # origin laptop-a not selected
    assert any("origin" in p for p in found)
    twins = [NodeRef("node_a", "same"), NodeRef("node_b", "same")]
    with pytest.raises(CampaignSpecError) as caught:
        expand(spec(nodes=["same"], origin=None), twins)
    assert "matches 2 nodes" in caught.value.problems[0]
    runs = expand(spec(nodes=["node_a", "laptop-b"]), NODES).runs
    assert {r.node_id for r in runs} == {"node_a", "node_b"}


def test_zero_runs_is_an_error() -> None:
    with pytest.raises(CampaignSpecError) as caught:
        expand(spec(strategies=["mode1"]), [NodeRef("node_a", "laptop-a")])
    assert "0 runs" in caught.value.problems[0]


def test_load_spec_refusals() -> None:
    with pytest.raises(CampaignSpecError, match="not valid YAML"):
        load_spec("name: [unclosed")
    with pytest.raises(CampaignSpecError, match="mapping"):
        load_spec("- a\n- b\n")
    with pytest.raises(CampaignSpecError, match="larger than"):
        load_spec("#" * (MAX_SPEC_BYTES + 1))


def test_sampler_enum_values() -> None:
    assert {s.value for s in Sampler} == {"grid", "random", "lhs"}
    assert math.isclose(Range(min=0, max=1).at(1.0), 1.0)


@pytest.mark.parametrize("on_invalid", ["error", "drop"])
@pytest.mark.parametrize(
    "params",
    [{"n": float("nan")}, {"n": [float("inf")]}, {"n": [10_000_000, float("-inf")]}],
)
def test_fix1_d2_non_finite_params_are_spec_errors(
    params: dict[str, Any], on_invalid: str
) -> None:
    with pytest.raises(CampaignSpecError) as caught:
        expand(
            spec(
                families=[{"family": "cpu.sort", "params": params}],
                on_invalid=on_invalid,
            ),
            NODES,
        )
    assert any("not a finite number" in p for p in caught.value.problems)


def test_fix1_d2_nan_hours_and_yaml_nan() -> None:
    with pytest.raises(CampaignSpecError):
        spec(budget={"max_runs": 10, "max_hours": float("nan")})
    with pytest.raises(CampaignSpecError):
        load_spec(
            "name: t\nseed: 1\nbudget: {max_runs: 5}\n"
            "families: [{family: cpu.sort, params: {n: .nan}}]\n"
        )


@pytest.mark.security
def test_a_spec_names_registered_families_only_never_code() -> None:
    """THREAT_MODEL RE-E3: a campaign is job submission at scale, so its spec can
    only name registered families with bounded, validated parameters - no
    entrypoint, file, image or code field exists, and the run count is capped."""
    for smuggled in (
        {"entrypoint": ["sh", "-c", "id"]},
        {"code": "import os"},
        {"image": "evil:latest"},
    ):
        with pytest.raises(CampaignSpecError):
            spec(**smuggled)
        with pytest.raises(CampaignSpecError):
            spec(families=[{"family": "cpu.sort", **smuggled}])
    with pytest.raises(CampaignSpecError):
        spec(families=[{"family": "../../etc/passwd"}])
    with pytest.raises(CampaignSpecError):  # outside the family's own model
        expand(spec(families=[{"family": "cpu.sort", "params": {"n": 10**12}}]), NODES)
    with pytest.raises(CampaignSpecError):
        spec(budget={"max_runs": HARD_MAX_RUNS + 1})
