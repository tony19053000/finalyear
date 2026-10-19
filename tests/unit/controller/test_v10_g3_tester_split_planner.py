"""V0.10 G3 tester r1: adversarial checks of the Mode 3 planner (criterion 5).

Derived from the criteria (brief 5c, D265, D275, D277-D279), not from the code:
margin boundary, degenerate inputs, monotonicity, determinism, pinned/retry,
byte-identity of whole-job rows, honest explain, D278 budgets.
"""

from __future__ import annotations

import datetime as dt
import itertools
import json
import math
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from meshforge.common.manifest import ManifestError, SplitSpec
from meshforge.common.split_items import SplitLimits, resolve
from meshforge.common.tree import build_tree
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    SplitConfig,
    SplitInfo,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.decision_json import candidates_json, split_json
from meshforge.controller.planner.split import choose_chunk_items
from meshforge.controller.planner.types import ControllerLinkStat

pytestmark = pytest.mark.unit

EXEC = frozenset({"exec", "exec-subprocess"})
PROJECT = "sha256:" + "a" * 64
MB = 1_000_000
NOW = dt.datetime(2026, 10, 20, 12, 0, tzinfo=dt.UTC)


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_threads": 8,
        "cpu_slots": 1,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def request(
    items: int = 3000, history: History | None = None, **kw: Any
) -> PlanRequest:
    split_kw = {
        k: kw.pop(k)
        for k in ("item_bytes", "items_tree", "chunk_items", "min_nodes", "max_nodes")
        if k in kw
    }
    base: dict[str, Any] = {
        "job_id": "job_01J0000000000000000000000",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e" * 64,
        "trees": (TreeNeed(PROJECT, 10 * MB),),
        "submitter_node_id": "n1",
        "seeded": True,
        "history": history or History(),
        "split": SplitInfo(items=items, **split_kw),
    }
    base.update(kw)
    return PlanRequest(**base)


def snap(*nodes: NodeSnapshot, **kw: Any) -> ClusterSnapshot:
    return ClusterSnapshot(nodes=tuple(sorted(nodes, key=lambda n: n.node_id)), **kw)


def fleet(n: int, **kw: Any) -> ClusterSnapshot:
    return snap(*(node(f"n{i}") for i in range(1, n + 1)), **kw)


def rates(**per_node: float) -> History:
    return History(item_rate_by_node=dict(per_node))


def makespan(decision: Any) -> float:
    assert decision.chosen is not None and decision.chosen.estimate is not None
    return float(decision.chosen.estimate.total_s)


# ---------------------------------------------------------- margin boundary


def _margin_snapshot(margin: float) -> ClusterSnapshot:
    return fleet(3, config=PlannerConfig(split=SplitConfig(split_margin=margin)))


def test_margin_is_exactly_the_threshold_boundary() -> None:
    """Set the margin to the measured ratio: passes at ==, fails just above."""
    req = request(history=rates(n1=10, n2=10, n3=10))
    base = plan(req, _margin_snapshot(0.10), "cost_model", NOW)
    assert base.split is not None and base.split.split
    test = base.split.margin
    assert test.set_s is not None and test.best_single_s is not None
    ratio = 1.0 - test.set_s / test.best_single_s
    # a margin a hair below the achieved gain splits, a hair above does not
    below = plan(req, _margin_snapshot(ratio - 1e-4), "cost_model", NOW)
    above = plan(req, _margin_snapshot(ratio + 1e-3), "cost_model", NOW)
    assert below.split is not None and below.split.split
    assert above.split is not None and not above.split.split
    assert above.chosen is not None and above.chosen.mode in ("local", "offload")
    assert len(above.split.lanes) == 1
    # the verdict reported agrees with the arithmetic
    assert above.split.margin.passed is False
    assert below.split.margin.passed is True


@pytest.mark.parametrize("margin", [-1.0, 0.0, 0.95, 1.0, 5.0, math.nan])
def test_absurd_margins_do_not_crash_or_forbid_everything(margin: float) -> None:
    req = request(history=rates(n1=10, n2=10, n3=10))
    decision = plan(req, _margin_snapshot(margin), "cost_model", NOW)
    assert decision.chosen is not None and decision.split is not None
    assert decision.split.lanes


def test_margin_zero_splits_on_any_gain_and_is_never_worse_than_single() -> None:
    req = request(history=rates(n1=10, n2=10, n3=10))
    decision = plan(req, _margin_snapshot(0.0), "cost_model", NOW)
    test = decision.split.margin  # type: ignore[union-attr]
    assert test.set_s is not None and test.best_single_s is not None
    assert test.passed == (test.set_s <= test.best_single_s)


# ------------------------------------------------------- degenerate inputs


@pytest.mark.parametrize("items", [0, 1, 2, 3])
def test_items_below_two_times_min_nodes_never_split(items: int) -> None:
    decision = plan(
        request(items=items, history=rates(n1=10, n2=10)), fleet(3), "cost_model"
    )
    assert decision.chosen is not None and decision.chosen.mode != "split"
    assert decision.split is not None and not decision.split.split
    assert len(decision.split.lanes) == 1
    assert decision.split.chunks >= 1 and decision.split.chunk_items >= 1


def test_exactly_two_times_min_nodes_may_split() -> None:
    decision = plan(
        request(items=4, history=rates(n1=0.001, n2=0.001), min_nodes=2),
        fleet(2),
        "cost_model",
    )
    assert decision.split is not None
    assert decision.split.margin.forced is None


def test_zero_eligible_nodes() -> None:
    decision = plan(request(), snap(), "cost_model")
    assert decision.chosen is None and decision.split is None


def test_one_eligible_node_runs_single_lane() -> None:
    decision = plan(request(), fleet(1), "cost_model")
    assert decision.split is not None and not decision.split.split
    assert [lane.node_id for lane in decision.split.lanes] == ["n1"]


def test_all_rates_unknown_is_assumed_never_measured() -> None:
    decision = plan(request(), fleet(3), "cost_model", NOW)
    assert decision.chosen is not None and decision.split is not None
    text = json.dumps(candidates_json(decision))
    for lane in decision.split.lanes:
        assert lane.rate_source in (
            "assumed",
            "default",
            "scaled",
            "history",
            "hardware",
        )
    rows = candidates_json(decision)
    for row in rows:
        est = row.get("estimate") or {}
        compute = est.get("terms", {}).get("compute")
        if compute:
            assert compute["source"] != "measured", text


@pytest.mark.parametrize(
    "bad", [0.0, -1.0, math.nan, math.inf, -math.inf, 1e-300, 1e300]
)
def test_pathological_rates_never_crash_or_emit_nonfinite(bad: float) -> None:
    req = request(history=rates(n1=bad, n2=10, n3=10))
    decision = plan(req, fleet(3), "cost_model", NOW)
    assert decision.chosen is not None and decision.split is not None
    plan_json = split_json(decision.split)
    # strict JSON: no NaN / Infinity tokens
    json.dumps(plan_json, allow_nan=False)
    json.dumps(candidates_json(decision), allow_nan=False)
    assert decision.split.chunks >= 1 and decision.split.chunk_items >= 1
    assert math.isfinite(decision.split.predicted_makespan_s)


@pytest.mark.parametrize("bad", [0.0, math.nan, math.inf, 1e-300])
def test_all_rates_pathological(bad: float) -> None:
    req = request(history=rates(n1=bad, n2=bad, n3=bad))
    decision = plan(req, fleet(3), "cost_model", NOW)
    assert decision.chosen is not None and decision.split is not None
    json.dumps(split_json(decision.split), allow_nan=False)


@pytest.mark.parametrize("items", [10_000, 10_001, 1_000_000, 10**9, 10**12])
def test_huge_item_counts_cap_chunks(items: int) -> None:
    req = request(items=items, history=rates(n1=1e6, n2=1e6, n3=1e6))
    decision = plan(req, fleet(3), "cost_model", NOW)
    assert decision.split is not None
    assert 1 <= decision.split.chunks <= 10_000
    assert decision.split.chunk_items * decision.split.chunks >= items
    for c in decision.candidates:
        if c.chunks is not None and c.mode == "split":
            assert c.chunks <= 10_000


@pytest.mark.parametrize("fixed", [1, 7, 3000, 3001, 10**9])
def test_fixed_chunk_larger_than_items(fixed: int) -> None:
    req = request(items=3000, history=rates(n1=10, n2=10, n3=10), chunk_items=fixed)
    decision = plan(req, fleet(3), "cost_model", NOW)
    assert decision.split is not None
    assert decision.split.chunk_items == min(fixed, 3000)
    assert decision.split.chunks == math.ceil(3000 / min(fixed, 3000))


def test_fixed_chunk_one_with_huge_items_is_user_choice() -> None:
    """Fixed chunks are the user's: documented that max_chunks only bounds auto.
    The planner must still terminate quickly and report truthfully."""
    req = request(items=50_000, history=rates(n1=1e4, n2=1e4), chunk_items=1)
    decision = plan(req, fleet(2), "cost_model", NOW)
    assert decision.split is not None
    assert decision.split.chunk_items == 1 and decision.split.chunks == 50_000


@pytest.mark.parametrize("lanes", [1, 2, 7, 64])
@pytest.mark.parametrize("per_item", [1e-12, 1e-3, 1.0, 1e6, math.inf, math.nan])
@pytest.mark.parametrize("items", [1, 2, 99, 12_345, 10**8])
def test_choose_chunk_items_invariants(lanes: int, per_item: float, items: int) -> None:
    c = choose_chunk_items(
        items,
        fastest_per_item_s=per_item,
        lanes=lanes,
        overhead_s=1.5,
        config=SplitConfig(),
    )
    assert 1 <= c.items <= items
    assert c.chunks == math.ceil(items / c.items)
    assert c.chunks <= 10_000


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT T1: balance rule sawtooth (minor)",
)
def test_more_items_never_reduces_chunks() -> None:
    prev = 0
    for items in [8, 16, 100, 1000, 5_000, 20_000, 100_000, 10**6]:
        c = choose_chunk_items(
            items, fastest_per_item_s=0.1, lanes=3, overhead_s=1.5, config=SplitConfig()
        )
        assert c.chunks >= prev, (items, c)
        prev = c.chunks


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT T1: balance rule sawtooth (minor)",
)
def test_chunk_count_monotone_dense() -> None:
    prev = 0
    for items in range(1, 3000):
        c = choose_chunk_items(
            items, fastest_per_item_s=0.1, lanes=3, overhead_s=1.5, config=SplitConfig()
        )
        assert c.chunks >= prev - 0, (items, c, prev)
        prev = c.chunks


# ------------------------------------------------------------- monotonicity


def test_adding_a_strictly_faster_node_never_worsens_makespan() -> None:
    rng = random.Random(1905)  # noqa: S311
    for _ in range(60):
        n = rng.randint(1, 4)
        base = {f"n{i}": rng.choice([2, 5, 10, 20, 50]) for i in range(2, 2 + n)}
        items = rng.choice([40, 300, 3000, 30_000])
        before = plan(
            request(items=items, history=rates(**base)),
            snap(*(node(k) for k in base)),
            "cost_model",
            NOW,
        )
        faster = max(base.values()) * rng.choice([1.5, 2, 10])
        extra = {**base, "n1": faster}
        after = plan(
            request(items=items, history=rates(**extra)),
            snap(*(node(k) for k in extra)),
            "cost_model",
            NOW,
        )
        assert makespan(after) <= makespan(before) + 1e-6, (base, faster, items)


def test_slower_link_never_makes_splitting_more_attractive() -> None:
    def gain(bw: float) -> float:
        req = request(
            history=rates(n1=20, n2=20, n3=20),
            trees=(TreeNeed(PROJECT, 3_010 * MB),),
            item_bytes=3_000 * MB,
            items_tree=PROJECT,
        )
        links = {n: ControllerLinkStat(bw_bytes_s=bw, samples=5) for n in ("n2", "n3")}
        d = plan(req, fleet(3, controller_links=links), "cost_model", NOW)
        t = d.split.margin  # type: ignore[union-attr]
        assert t.set_s is not None and t.best_single_s is not None
        return 1.0 - t.set_s / t.best_single_s

    gains = [gain(bw) for bw in (1000 * MB, 100 * MB, 10 * MB, 1 * MB, 0.1 * MB)]
    for fast, slow in itertools.pairwise(gains):
        assert slow <= fast + 1e-9, gains


def test_slower_link_decision_flips_only_towards_unsplit() -> None:
    flipped_back = False
    seen_unsplit = False
    for bw in (1000, 300, 100, 30, 10, 3, 1, 0.3):
        req = request(
            history=rates(n1=20, n2=20, n3=20),
            trees=(TreeNeed(PROJECT, 3_010 * MB),),
            item_bytes=3_000 * MB,
            items_tree=PROJECT,
        )
        links = {
            n: ControllerLinkStat(bw_bytes_s=bw * MB, samples=5) for n in ("n2", "n3")
        }
        d = plan(req, fleet(3, controller_links=links), "cost_model", NOW)
        split = d.split is not None and d.split.split
        if not split:
            seen_unsplit = True
        elif seen_unsplit:
            flipped_back = True
    assert seen_unsplit and not flipped_back


# --------------------------------------------------------------- determinism


def test_node_order_and_ties_are_deterministic() -> None:
    nodes = [node(f"n{i}") for i in range(1, 6)]
    hist = rates(**{f"n{i}": 10.0 for i in range(1, 6)})  # all tied
    outs = set()
    rng = random.Random(7)  # noqa: S311
    for _ in range(12):
        shuffled = nodes[:]
        rng.shuffle(shuffled)
        s = ClusterSnapshot(nodes=tuple(shuffled))
        d = plan(request(history=hist), s, "cost_model", NOW)
        assert d.split is not None
        outs.add(json.dumps([candidates_json(d), split_json(d.split)], sort_keys=True))
    assert len(outs) == 1


def test_repeated_decisions_are_byte_identical() -> None:
    req = request(history=rates(n1=13, n2=7, n3=3))
    first = None
    for _ in range(5):
        d = plan(req, fleet(3), "cost_model", NOW)
        blob = json.dumps(candidates_json(d), sort_keys=False)
        first = first or blob
        assert blob == first


# ----------------------------------------------------- pinned / retry exclusion


def test_pinned_never_splits_even_with_huge_gain() -> None:
    d = plan(
        request(history=rates(n1=10, n2=10, n3=10), pinned_node_id="n3"),
        fleet(3),
        "cost_model",
    )
    assert d.split is not None and not d.split.split
    assert [lane.node_id for lane in d.split.lanes] == ["n3"]
    assert all(c.mode != "split" or not c.feasible for c in d.candidates)


def test_pinned_ignores_exclusion_but_never_adds_a_set() -> None:
    d = plan(
        request(
            history=rates(n1=10, n2=10),
            pinned_node_id="n2",
            excluded={"n2": "node_lost"},
        ),
        fleet(3),
        "cost_model",
    )
    assert d.split is not None and not d.split.split


def test_excluded_nodes_never_in_any_row_or_lane() -> None:
    req = request(
        history=rates(n1=10, n2=10, n3=10, n4=10),
        excluded={"n2": "node_lost", "n4": "oom"},
    )
    d = plan(req, fleet(4), "cost_model", NOW)
    for c in d.candidates:
        if c.feasible:
            assert not ({"n2", "n4"} & set(c.node_set or (c.node_id,)))
    assert d.split is not None
    assert not {"n2", "n4"} & {lane.node_id for lane in d.split.lanes}


def test_everything_excluded_but_one_runs_unsplit() -> None:
    req = request(history=rates(n1=10, n2=10, n3=10), excluded={"n2": "x", "n3": "x"})
    d = plan(req, fleet(3), "cost_model", NOW)
    assert d.split is not None and not d.split.split
    assert d.split.margin.forced == "split_too_few_nodes"


# ------------------------------------- whole job rows unchanged / explain honesty


def test_whole_job_has_no_split_artifacts() -> None:
    req = replace(request(), split=None)
    d = plan(req, fleet(3), "cost_model", NOW)
    assert d.split is None
    blob = json.dumps(candidates_json(d))
    for key in (
        "node_set",
        "chunk_items",
        "margin_test",
        "rate_source",
        "start_s",
        "inactive",
    ):
        assert key not in blob
    assert all(c["mode"] != "split" for c in candidates_json(d))


def test_split_flag_does_not_change_whole_job_decision() -> None:
    a = plan(replace(request(), split=None), fleet(3), "cost_model", NOW)
    b = plan(replace(request(), split=None, attempt_no=1), fleet(3), "cost_model", NOW)
    assert json.dumps(candidates_json(a)) == json.dumps(candidates_json(b))


def test_assumed_rates_flagged_in_rows() -> None:
    d = plan(request(), fleet(3), "cost_model", NOW)
    rows = candidates_json(d)
    singles = [r for r in rows if r["mode"] in ("local", "offload")]
    assert singles
    for r in singles:
        assert r.get("rate_source") in (None, "assumed", "scaled", "history", "default")
        comp = r["estimate"]["terms"]["compute"]
        assert comp["source"] in ("assumed", "default")


def test_measured_rate_not_flagged_assumed() -> None:
    d = plan(request(history=rates(n1=10, n2=10, n3=10)), fleet(3), "cost_model", NOW)
    singles = [r for r in candidates_json(d) if r["mode"] in ("local", "offload")]
    for r in singles:
        assert r["estimate"]["terms"]["compute"]["source"] == "history"


# --------------------------------------------------------------- D278 budget


def _tree(root: Path, files: dict[str, bytes]) -> Any:
    for name, data in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return build_tree(root)


def _res(built: Any, over: Any, limits: SplitLimits) -> Any:
    return resolve(
        SplitSpec.model_validate({"over": over}),
        built.tree,
        {},
        lambda sha: built.sources[sha].open("rb"),
        limits,
    )


def test_list_budget_never_undercounts_for_nasty_unicode(tmp_path: Path) -> None:
    nasty = ["é", "😀" * 5, '"', "\\", "a\nb", "日本語" * 3, "\U0010ffff", "x\u2028y"]
    built = _tree(tmp_path / "p", {"x.txt": b"x"})
    for items in (
        nasty,
        [f"{t}{i}" for i in range(40) for t in nasty],
        [str(i) for i in range(1000)],
        ["é"] * 1,
    ):
        size = len(_res(built, {"list": items}, SplitLimits()).blob())
        with pytest.raises(ManifestError):
            _res(built, {"list": items}, SplitLimits(max_list_bytes=size - 1))
        assert _res(
            built, {"list": items}, SplitLimits(max_list_bytes=size + 400)
        ).count == len(items)


@pytest.mark.parametrize(
    "rng",
    [
        {"start": 0, "stop": 1000, "step": 1},
        {"start": 10**15, "stop": 10**15 + 500, "step": 3},
        {"start": 1000, "stop": -1000, "step": -7},
        {"start": -(10**12), "stop": 10**12, "step": 10**9},
    ],
)
def test_list_budget_never_undercounts_for_ranges(
    tmp_path: Path, rng: dict[str, int]
) -> None:
    built = _tree(tmp_path / "p", {"x.txt": b"x"})
    size = len(_res(built, {"range": rng}, SplitLimits()).blob())
    with pytest.raises(ManifestError):
        _res(built, {"range": rng}, SplitLimits(max_list_bytes=size - 1))


def test_lines_cap_boundary_is_exact(tmp_path: Path) -> None:
    data = b"h\n" + b"1\n" * 99
    built = _tree(tmp_path / "p", {"t.csv": data})
    ok = SplitLimits(max_lines_file_bytes=len(data))
    assert _res(built, {"lines": "t.csv"}, ok).count == 100
    with pytest.raises(ManifestError) as info:
        _res(built, {"lines": "t.csv"}, SplitLimits(max_lines_file_bytes=len(data) - 1))
    assert "larger than" in info.value.problems[0][1]


def test_default_lines_cap_is_256_mib() -> None:
    assert SplitLimits().max_lines_file_bytes == 256 * 1024**2


def test_chunks_floor_never_below_four_per_node_when_items_allow() -> None:
    """The weaker invariant that does hold: at least min(items, 4 x lanes) chunks."""
    for items in range(1, 3000, 7):
        c = choose_chunk_items(
            items, fastest_per_item_s=0.1, lanes=3, overhead_s=1.5, config=SplitConfig()
        )
        assert c.chunks >= min(items, 12), (items, c)
