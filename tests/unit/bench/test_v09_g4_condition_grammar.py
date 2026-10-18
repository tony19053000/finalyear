"""V0.9 G4 lane B item 1: condition grammar + campaign sweep axis (0.9.5, D217/D241)."""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from meshforge.bench.campaign import (
    CampaignSpecError,
    NodeRef,
    expand,
    parse_spec,
    run_dict,
    runs_json,
)
from meshforge.bench.conditions import (
    ConditionError,
    ConditionKind,
    CpuLoad,
    Netem,
    advertised_kinds,
    capability_tag,
    extra_seconds,
    parse_condition,
    unsupported_reason,
)

pytestmark = pytest.mark.unit

ALL = frozenset(ConditionKind) - {ConditionKind.NONE}
A = NodeRef("node_a", "laptop-a", conditions=frozenset(ALL), threads=8)
B = NodeRef(
    "node_b", "laptop-b", conditions=frozenset({ConditionKind.CACHE}), threads=4
)
B_ALL = NodeRef("node_b", "laptop-b", conditions=frozenset(ALL), threads=4)

BASE: dict[str, Any] = {
    "name": "g4-unit",
    "seed": 5,
    "families": [{"family": "cpu.sort"}],
    "strategies": ["local", "mode1"],
    "origin": "laptop-a",
    "repetitions": 3,
    "budget": {"max_runs": 1000, "max_hours": 24},
}


def spec(**changes: Any) -> Any:
    return parse_spec({**BASE, **changes})


# ------------------------------------------------------------ grammar


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("none", "none"),
        ("cpu_load:2", "cpu_load:2"),
        ("gpu_load:50", "gpu_load:50"),
        ("gpu_load:50%", "gpu_load:50"),
        ("queue:4", "queue:4"),
        ("cache:cold", "cache:cold"),
        ("cache:warm", "cache:warm"),
        ("netem:delay=50ms,loss=1%", "netem:delay=50ms,loss=1%"),
        ("netem:loss=1.0%,delay=0.05s", "netem:delay=50ms,loss=1%"),
        ("netem:rate=10mbit", "netem:rate=10mbit"),
        ("netem:rate=2000kbit,delay=1ms", "netem:delay=1ms,rate=2mbit"),
        ("netem:rate=1gbit", "netem:rate=1000mbit"),
        ("unavailable:30s", "unavailable:30s"),
        ("unavailable:30", "unavailable:30s"),
        ("unavailable:2m", "unavailable:120s"),
    ],
)
def test_canonical_form(text: str, canonical: str) -> None:
    condition = parse_condition(text)
    assert condition.canonical() == canonical
    # the canonical form parses to the same condition (a fixed point)
    assert parse_condition(canonical) == condition
    assert parse_condition(canonical).canonical() == canonical


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("", "1-64 characters"),
        ("x" * 65, "1-64 characters"),
        ("thermal", "unknown condition 'thermal'"),
        ("CPU_LOAD:1", "unknown condition"),
        ("none:1", "no argument"),
        ("cpu_load", "needs an argument"),
        ("cpu_load:", "needs an argument"),
        ("cpu_load:0", "greater than or equal to 1"),
        ("cpu_load:257", "less than or equal to 256"),
        ("cpu_load:-1", "not a whole number"),
        ("cpu_load:1.5", "not a whole number"),
        ("cpu_load: 1", "not a whole number"),
        ("gpu_load:9", "greater than or equal to 10"),
        ("gpu_load:101", "less than or equal to 100"),
        ("queue:0", "greater than or equal to 1"),
        ("queue:33", "less than or equal to 32"),
        ("cache:lukewarm", "cold or warm"),
        ("netem:delay=501ms", "less than or equal to 500"),
        ("netem:delay=1s", "less than or equal to 500"),
        ("netem:loss=21%", "less than or equal to 20"),
        ("netem:rate=999kbit", "greater than or equal to 1"),
        ("netem:delay=0ms", "at least one"),
        ("netem:delay=50", "<n>ms"),
        ("netem:jitter=5ms", "not delay="),
        ("netem:delay=5ms,delay=6ms", "given twice"),
        ("netem:delay=5ms;rm -rf /", "<n>ms"),
        ("netem:delay=5ms dev lo", "<n>ms"),
        ("unavailable:0s", "greater than or equal to 1"),
        ("unavailable:301s", "less than or equal to 300"),
        ("unavailable:6m", "less than or equal to 300"),
        ("unavailable:1h", "not <n>s"),
    ],
)
@pytest.mark.security
def test_bad_conditions_name_the_problem(text: str, needle: str) -> None:
    with pytest.raises(ConditionError) as caught:
        parse_condition(text)
    assert needle in str(caught.value), str(caught.value)


@pytest.mark.parametrize("value", [None, 1, ["cpu_load:1"], {"cpu_load": 1}])
def test_non_string_is_refused(value: object) -> None:
    with pytest.raises(ConditionError):
        parse_condition(value)


def test_netem_tc_args_come_from_numbers_only() -> None:
    netem = parse_condition("netem:delay=50ms,loss=1.5%,rate=10mbit")
    assert isinstance(netem, Netem)
    assert netem.tc_args() == ["delay", "50ms", "loss", "1.5%", "rate", "10mbit"]


def test_capability_tag_round_trip_and_unknown_names() -> None:
    tag = capability_tag(["queue", "cpu_load", "cache", "netem"])
    assert tag == "bench-conditions:cache,cpu_load,netem"  # agent kinds only, sorted
    assert advertised_kinds(["exec", tag]) == {
        ConditionKind.CACHE,
        ConditionKind.CPU_LOAD,
        ConditionKind.NETEM,
    }
    # an unknown or controller-side name is ignored, never a crash
    assert advertised_kinds(["bench-conditions:warp,queue,,cache"]) == {
        ConditionKind.CACHE
    }
    assert advertised_kinds([]) == frozenset()
    assert capability_tag(["queue"]) is None


def test_support_rules() -> None:
    cpu2 = parse_condition("cpu_load:2")
    assert (
        unsupported_reason(cpu2, strategy="local", node_kinds=None, node_threads=1)
        is None
    )
    assert (
        unsupported_reason(
            cpu2, strategy="local", node_kinds=frozenset(), node_threads=8
        )
        == "the node does not offer cpu_load (bench-conditions capability)"
    )
    assert "threads" in str(
        unsupported_reason(
            CpuLoad(cores=9), strategy="local", node_kinds=ALL, node_threads=8
        )
    )
    away = parse_condition("unavailable:10s")
    assert "mode1" in str(
        unsupported_reason(away, strategy="local", node_kinds=ALL, node_threads=8)
    )
    assert (
        unsupported_reason(
            away, strategy="mode1", node_kinds=frozenset(), node_threads=1
        )
        is None
    )
    queue = parse_condition("queue:3")
    assert (
        unsupported_reason(
            queue, strategy="local", node_kinds=frozenset(), node_threads=1
        )
        is None
    )


def test_extra_seconds() -> None:
    assert extra_seconds(parse_condition("queue:3"), 10.0) == 30.0
    assert extra_seconds(parse_condition("cache:warm"), 10.0) == 10.0
    assert extra_seconds(parse_condition("cache:cold"), 10.0) == 0.0
    assert extra_seconds(parse_condition("unavailable:20s"), 10.0) == 20.0
    assert extra_seconds(parse_condition("cpu_load:1"), 10.0) == 0.0


# ------------------------------------------------------------ campaign axis


def test_none_only_spec_is_unchanged_by_g4() -> None:
    """``conditions`` omitted == ``[none]``; group ids and seeds use the
    pre-G4 identity, and no run JSON carries a ``skip_reason``."""
    implicit = expand(spec(), [A, B])
    explicit = expand(spec(conditions=["none"]), [A, B])
    assert runs_json(implicit.runs) == runs_json(explicit.runs)
    assert all("skip_reason" not in run_dict(r) for r in implicit.runs)
    assert {r.conditions for r in implicit.runs} == {("none",)}


def test_each_condition_is_a_sweep_axis_with_one_condition_per_run() -> None:
    expansion = expand(spec(conditions=["none", "cpu_load:1", "queue:2"]), [A, B_ALL])
    assert {r.conditions for r in expansion.runs} == {
        ("none",),
        ("cpu_load:1",),
        ("queue:2",),
    }
    assert all(
        len(r.conditions) == 1 and r.condition == r.conditions[0]
        for r in expansion.runs
    )
    assert expansion.groups == 3  # one point x three conditions
    # 3 groups x (local on A + mode1 on B) x 3 repetitions
    assert len(expansion.runs) == 3 * 2 * 3
    # same point -> same workload seed under every condition (same data)
    assert len({r.seed for r in expansion.runs}) == 1
    # a different condition -> a different group
    groups = {r.condition: r.group_id for r in expansion.runs}
    assert len(set(groups.values())) == 3


def test_equal_conditions_digest_equal() -> None:
    first = expand(spec(conditions=["netem:loss=1.0%,delay=50ms"]), [A, B_ALL])
    second = expand(spec(conditions=["netem:delay=50ms,loss=1%"]), [A, B_ALL])
    assert runs_json(first.runs) == runs_json(second.runs)
    assert {r.condition for r in first.runs} == {"netem:delay=50ms,loss=1%"}


def test_same_condition_twice_is_refused_even_spelled_differently() -> None:
    with pytest.raises(CampaignSpecError) as caught:
        spec(conditions=["unavailable:60s", "unavailable:1m"])
    assert any("listed twice" in p for p in caught.value.problems)


def test_bad_condition_is_a_spec_error_naming_it() -> None:
    with pytest.raises(CampaignSpecError) as caught:
        spec(conditions=["none", "cpu_load:x"])
    assert caught.value.problems == [
        "conditions[1]: cpu_load: 'x' is not a whole number"
    ]


def test_unsupported_is_a_create_error_listing_node_x_condition() -> None:
    with pytest.raises(CampaignSpecError) as caught:
        expand(spec(conditions=["none", "cpu_load:2", "netem:delay=10ms"]), [A, B])
    problems = caught.value.problems
    assert problems[:2] == [
        "node node_b x condition cpu_load:2: the node does not offer cpu_load "
        "(bench-conditions capability)",
        "node node_b x condition netem:delay=10ms: the node does not offer netem "
        "(bench-conditions capability)",
    ]
    assert caught.value.details["unsupported"] == [
        {"node_id": "node_b", "condition": "cpu_load:2"},
        {"node_id": "node_b", "condition": "netem:delay=10ms"},
    ]


def test_on_unsupported_skip_marks_runs_and_counts_them() -> None:
    expansion = expand(
        spec(conditions=["none", "cpu_load:2"], on_unsupported="skip"), [A, B]
    )
    skipped = [r for r in expansion.runs if r.skip_reason is not None]
    assert {(r.node_id, r.condition) for r in skipped} == {("node_b", "cpu_load:2")}
    assert len(skipped) == 3 == expansion.unsupported
    assert all("skip_reason" in run_dict(r) for r in skipped)
    # the hour estimate leaves the skipped runs out
    ran = expand(spec(conditions=["none", "cpu_load:2"], on_unsupported="skip"), [A, B])
    assert ran.estimated_hours == expansion.estimated_hours


def test_unavailable_needs_mode1() -> None:
    with pytest.raises(CampaignSpecError) as caught:
        expand(spec(conditions=["unavailable:10s"]), [A, B])
    assert "unavailable needs the mode1 strategy" in caught.value.problems[0]
    only_mode1 = expand(
        spec(conditions=["unavailable:10s"], strategies=["mode1"]), [A, B]
    )
    assert {r.strategy for r in only_mode1.runs} == {"mode1"}


def test_cpu_load_is_bounded_by_the_node_threads() -> None:
    with pytest.raises(CampaignSpecError) as caught:
        expand(
            spec(conditions=["cpu_load:6"], strategies=["local"], origin=None),
            [A, NodeRef("node_c", conditions=ALL, threads=4)],
        )
    assert any("node_c" in p and "threads" in p for p in caught.value.problems)


def test_unknown_capabilities_are_not_checked() -> None:
    """An offline ``bench plan --node`` has no tags: no support error there
    (the controller checks again at create)."""
    nodes = [NodeRef("node_a", "laptop-a"), NodeRef("node_b")]
    expansion = expand(spec(conditions=["netem:delay=5ms", "cpu_load:64"]), nodes)
    assert all(r.skip_reason is None for r in expansion.runs)


def test_conditions_add_to_the_hour_estimate() -> None:
    base = expand(spec(conditions=["none"]), [A, B]).estimated_hours
    queued = expand(spec(conditions=["queue:4"]), [A, B]).estimated_hours
    assert queued == pytest.approx(base * 5, rel=2e-2)  # (hours rounded to 3)


def test_session_label() -> None:
    assert spec(session_label="wifi").session_label == "wifi"
    for bad in ["", "-wifi", "wi fi", "x" * 33, "wifi\n"]:
        with pytest.raises(CampaignSpecError) as caught:
            spec(session_label=bad)
        assert any("session_label" in p for p in caught.value.problems)


def test_condition_order_in_the_spec_does_not_change_the_runs() -> None:
    first = expand(spec(conditions=["none", "queue:2", "cache:cold"]), [A, B])
    second = expand(spec(conditions=["cache:cold", "none", "queue:2"]), [A, B])
    assert (
        hashlib.sha256(runs_json(first.runs).encode()).digest()
        == hashlib.sha256(runs_json(second.runs).encode()).digest()
    )
