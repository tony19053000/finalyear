"""V0.9 G3 lane B tester r1: adversarial campaign-spec / expansion tests derived
# ruff: noqa: E501, PERF402
from D216 (deterministic, stratified, interleaved, budgeted, validated)."""

from __future__ import annotations

import hashlib
import itertools
import os
import subprocess
import sys
import textwrap
import time
from typing import Any

import pytest

from meshforge.bench.campaign import (
    MAX_SPEC_BYTES,
    CampaignSpecError,
    NodeRef,
    expand,
    load_spec,
    parse_spec,
    runs_json,
    spec_digest,
)

pytestmark = pytest.mark.unit

NODES = [NodeRef("node_c", "c"), NodeRef("node_a", "a"), NodeRef("node_b", "b")]

BASE: dict[str, Any] = {
    "name": "t",
    "seed": 11,
    "families": [
        {"family": "cpu.matmul", "params": {"n": [2048, 2560], "repeats": 30}},
        {"family": "cpu.sort"},
    ],
    "strategies": ["local", "mode1"],
    "repetitions": 3,
    "budget": {"max_runs": 5000},
}


def digest(spec: dict[str, Any], nodes: list[NodeRef]) -> str:
    return hashlib.sha256(
        runs_json(expand(parse_spec(spec), nodes).runs).encode()
    ).hexdigest()


# ---------------------------------------------------------- determinism


def test_every_node_order_gives_the_same_bytes() -> None:
    seen = {digest(BASE, list(p)) for p in itertools.permutations(NODES)}
    assert len(seen) == 1


_SCRIPT = textwrap.dedent(
    """
    import hashlib, json, sys
    from meshforge.bench.campaign import NodeRef, expand, parse_spec, runs_json
    spec = json.loads(sys.argv[1])
    nodes = [NodeRef("node_c", "c"), NodeRef("node_a", "a"), NodeRef("node_b", "b")]
    print(hashlib.sha256(runs_json(expand(parse_spec(spec), nodes).runs).encode()).hexdigest())
    """
)


@pytest.mark.parametrize("sampler", ["grid", "random", "lhs"])
def test_identical_bytes_across_processes_and_hash_seeds(sampler: str) -> None:
    import json

    spec = json.loads(json.dumps(BASE))
    spec["origin"] = "node_a"
    if sampler != "grid":
        spec["sampler"] = sampler
        spec["samples"] = 6
        spec["families"] = [
            {
                "family": "cpu.matmul",
                "params": {
                    "n": {"min": 2048, "max": 2600, "int": True, "log": True},
                    "repeats": {"min": 30, "max": 45, "int": True},
                },
            }
        ]
    outs = set()
    for hashseed in ("0", "1", "12345", "random"):
        env = {**os.environ, "PYTHONHASHSEED": hashseed}
        res = subprocess.run(
            [sys.executable, "-c", _SCRIPT, json.dumps(spec)],
            capture_output=True,
            text=True,
            env=env,
            check=True,
            timeout=120,
        )
        outs.add(res.stdout.strip())
    assert len(outs) == 1, outs


# ---------------------------------------------------- stratification


def test_lhs_stratification_holds_for_many_seeds_and_sizes() -> None:
    for seed in range(25):
        for samples in (2, 5, 17):
            spec = {
                **BASE,
                "seed": seed,
                "sampler": "lhs",
                "samples": samples,
                "on_invalid": "drop",
                "families": [
                    {
                        "family": "cpu.sort",
                        "params": {
                            "n": {"min": 10_000_000, "max": 50_000_000, "int": True},
                            "repeats": 18,
                        },
                    }
                ],
                "strategies": ["local"],
                "origin": "node_a",
            }
            exp = expand(parse_spec(spec), NODES)
            ns = sorted({r.params["n"] for r in exp.runs})
            assert len(ns) + exp.duplicates == samples
            span = 40_000_000
            strata = {
                min(int((n - 10_000_000) / span * samples), samples - 1) for n in ns
            }
            assert len(strata) == len(ns), (seed, samples, ns)


# --------------------------------------------------- interleaving / budget


def test_every_round_has_each_config_exactly_once_and_rounds_differ() -> None:
    exp = expand(parse_spec(BASE), NODES)
    per_round = exp.configs
    rounds = [exp.runs[i * per_round : (i + 1) * per_round] for i in range(3)]
    orders = []
    for rnd in rounds:
        assert len({r.config_id for r in rnd}) == per_round
        assert {r.repetition for r in rnd} == {rnd[0].repetition}
        orders.append([r.config_id for r in rnd])
    assert orders[0] != orders[1] != orders[2]
    assert [r.seq for r in exp.runs] == list(range(1, len(exp.runs) + 1))


def test_budget_boundary_is_exact() -> None:
    exp = expand(parse_spec(BASE), NODES)
    n = len(exp.runs)
    expand(parse_spec({**BASE, "budget": {"max_runs": n}}), NODES)
    with pytest.raises(CampaignSpecError):
        expand(parse_spec({**BASE, "budget": {"max_runs": n - 1}}), NODES)


def test_origin_makes_local_run_on_the_origin_only() -> None:
    exp = expand(parse_spec({**BASE, "origin": "a"}), NODES)
    for r in exp.runs:
        assert r.origin_node_id == "node_a"
        if r.strategy == "local":
            assert r.node_id == "node_a"
        else:
            assert r.node_id != "node_a"


# ------------------------------------------------------ validation


@pytest.mark.parametrize(
    "mutation",
    [
        {"families": [{"family": "cpu.nope"}]},
        {"families": [{"family": "cpu.sort", "params": {"bogus": 1}}]},
        {"families": [{"family": "cpu.sort", "params": {"n": 1}}]},  # below bound
        {"families": [{"family": "cpu.sort", "params": {"n": 10**12}}]},
        {"families": [{"family": "cpu.sort", "params": {"n": True}}]},
        {"families": [{"family": "cpu.sort", "params": {"n": "5000000"}}]},
        {"families": [{"family": "cpu.sort", "params": {"n": 2e6}}]},
        {"families": [{"family": "cpu.sort", "params": {"n": float("nan")}}]},
        {"families": [{"family": "cpu.sort", "params": {"n": float("inf")}}]},
        {"families": [{"family": "cpu.sort", "params": {"n": [float("nan")]}}]},
        {
            "families": [
                {"family": "cpu.sort", "params": {"n": {"min": 1, "max": float("inf")}}}
            ]
        },
        {"conditions": ["thermal_throttle"]},
        {"nodes": ["ghost"]},
        {"origin": "ghost"},
        {"strategies": ["mode9"]},
        {"strategies": ["mode1"], "nodes": ["a"]},  # mode1 needs a second node
        {"repetitions": 2},
        {"repetitions": True},
        {"seed": -1},
        {"seed": 2**63},
        {"seed": 1.5},
        {"name": "Bad Name"},
        {"name": ""},
        {"name": "x" * 65},
        {"extra_key": 1},
        {"version": 2},
        {"budget": {"max_runs": 0}},
        {"budget": {"max_runs": 10**9}},
        {"budget": {"max_runs": 10, "max_hours": 0}},
        {"budget": {"max_runs": 10, "max_hours": float("nan")}},
    ],
)
def test_bad_specs_raise_spec_error_not_crash(mutation: dict[str, Any]) -> None:
    """Any hostile spec must end as CampaignSpecError (HTTP 400), never a bare
    ValueError/TypeError/ZeroDivisionError (HTTP 500)."""
    raw = {**BASE, **mutation}
    with pytest.raises(CampaignSpecError):
        spec = parse_spec(raw)
        exp = expand(spec, NODES)
        spec_digest(spec)
        runs_json(exp.runs)


def test_nan_in_range_or_list_never_reaches_digest_as_a_bare_valueerror() -> None:
    for params in ({"n": [float("nan")]}, {"n": float("inf")}):
        raw = {**BASE, "on_invalid": "drop"}
        raw["families"] = [{"family": "cpu.sort", "params": params}]
        try:
            spec = parse_spec(raw)
            expand(spec, NODES)
            spec_digest(spec)
        except CampaignSpecError:
            pass


def test_on_invalid_drop_counts_each_dropped_point() -> None:
    raw = {
        **BASE,
        "on_invalid": "drop",
        "families": [
            {
                "family": "cpu.sort",
                "params": {"n": [1, 15_000_000, 20_000_000, 10**12], "repeats": 20},
            }
        ],
    }
    exp = expand(parse_spec(raw), NODES)
    assert exp.dropped == 2
    assert exp.groups == 2


def test_unicode_and_odd_node_names() -> None:
    nodes = [NodeRef("node_1", "laptop-ä"), NodeRef("node_2", "laptop-ä")]
    with pytest.raises(CampaignSpecError):
        expand(parse_spec({**BASE, "nodes": ["laptop-ä"]}), nodes)
    exp = expand(parse_spec({**BASE, "nodes": ["node_1", "node_2"]}), nodes)
    assert exp.runs


# ---------------------------------------------------- hostile YAML


def _timed(text: str) -> None:
    start = time.monotonic()
    with pytest.raises(CampaignSpecError):
        load_spec(text)
    assert time.monotonic() - start < 5.0


def test_billion_laughs_anchor_bomb_is_refused_fast() -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 10):
        refs = ", ".join(f"*a{i - 1}" for _ in range(9))
        lines.append(f"a{i}: &a{i} [{refs}]")
    _timed("\n".join(lines))
    body = "\n".join(lines)
    _timed(
        "name: t\nseed: 1\nbudget: {max_runs: 5}\nfamilies:\n  - family: cpu.sort\n"
        "    params:\n      n: *a9\n"
        + "x-bomb:\n"
        + "\n".join("  " + ln for ln in lines)
    )
    assert body


def test_anchor_bomb_in_a_param_list_is_refused_fast() -> None:
    lines = ["x0: &a0 [1, 1, 1, 1, 1, 1, 1, 1, 1]"]
    for i in range(1, 9):
        lines.append(f"x{i}: &a{i} [{', '.join(f'*a{i - 1}' for _ in range(9))}]")
    text = (
        "name: t\nseed: 1\nbudget: {max_runs: 5}\n"
        + "\n".join(lines)
        + "\nfamilies:\n  - family: cpu.sort\n    params:\n      n: *a8\n"
    )
    _timed(text)


def test_huge_param_list_is_bounded() -> None:
    items = ", ".join(str(2_000_000 + i) for i in range(MAX_SPEC_BYTES // 10))
    text = (
        "name: t\nseed: 1\nbudget: {max_runs: 5}\nfamilies:\n"
        f"  - family: cpu.sort\n    params:\n      n: [{items}]\n"
    )
    assert len(text) < MAX_SPEC_BYTES
    start = time.monotonic()
    with pytest.raises(CampaignSpecError):
        expand(load_spec(text), NODES)
    assert time.monotonic() - start < 10.0


def test_cross_product_of_huge_axes_does_not_materialise() -> None:
    items = ", ".join(str(1 + i) for i in range(3000))
    text = (
        "name: t\nseed: 1\nbudget: {max_runs: 5}\nfamilies:\n"
        "  - family: gpu.conv2d\n    params:\n"
        f"      batch: [{items[:0] or ', '.join(str(i % 250 + 1) for i in range(3000))}]\n"
        f"      repeats: [{', '.join(str(i + 1) for i in range(3000))}]\n"
        f"      size: [{', '.join(str(i + 16) for i in range(230))}]\n"
    )
    start = time.monotonic()
    with pytest.raises(CampaignSpecError):
        expand(load_spec(text), NODES)
    assert time.monotonic() - start < 10.0


@pytest.mark.parametrize(
    "text",
    [
        "",
        "- a\n- b\n",
        "just a string",
        "1: 2\n",
        "? [a, b]\n: c\n",
        "!!python/object/apply:os.system ['true']\n",
        "name: t\nseed: 1\nbudget: {max_runs: 5}\nfamilies: [{family: cpu.sort, params: {1: 2}}]\n",
        "name: t\nseed: 1\nbudget: {max_runs: 5}\nfamilies: [{family: cpu.sort, params: {n: 2020-01-01}}]\n",
        "name: !!binary aGk=\nseed: 1\nbudget: {max_runs: 5}\nfamilies: [{family: cpu.sort}]\n",
        "\x00\x01",
        "name: t\n\ttab: bad\n",
        "a: &x\n  b: *x\n",
    ],
)
def test_hostile_yaml_is_a_spec_error(text: str) -> None:
    with pytest.raises(CampaignSpecError):
        load_spec(text)


def test_oversize_and_non_utf8_are_refused() -> None:
    with pytest.raises(CampaignSpecError):
        load_spec(b"#" * (MAX_SPEC_BYTES + 1))
    with pytest.raises(CampaignSpecError):
        load_spec(b"name: \xff\xfe\n")


def test_secrets_never_enter_expansion_or_digest() -> None:
    raw = {**BASE}
    exp = expand(parse_spec(raw), NODES)
    blob = runs_json(exp.runs) + spec_digest(parse_spec(raw))
    for needle in ("token", "password", "secret", "Bearer", "authorization"):
        assert needle.lower() not in blob.lower()
