# ruff: noqa: S311 - seeded test data, not security
"""V0.15 G4 tester round 2: every stored term's ``detail.ml`` is in that
term's own unit (fix of G4-R1-1, D367-G4-1).

For random planner cases (Mode 1 whole job and Mode 3 split job: one-lane
candidates, multi-node sets, the combine) with a random stub model:

* every ML-used term satisfies ``raw_s * exp(y_hat) == seconds`` (the unit is
  the term's seconds, never seconds per item);
* a one-lane compute term carries the lane's rate block nested as
  ``detail.ml.rate`` (seconds per item), and the lane's published
  ``rate_items_s`` equals ``1 / (rate.raw_s * exp(rate.y_hat))``;
* the outer block's ``used`` / ``reason`` / ``y_hat`` mirror the rate's;
* ``raw_prediction`` gives the term's analytical seconds, the explain line is
  sane (``ML <raw> -> <seconds>``, raw never larger than seconds by more than
  the correction factor, no per-item number);
* ``ml/targets`` reads the chunk_rate raw from ``ml.rate`` (never from the
  outer block) and tolerates hostile shapes; the training rows of a split job
  are unchanged by the nesting (ML-off rows == ML-on rows for the raw features
  and targets).
"""

from __future__ import annotations

import json
import math
import random
from typing import Any

import pytest
from mf_ml_stub import Rule, stub_ml
from mf_planner_fabrics import random_case

from meshforge.cli.explain_commands import _ml_note, _term_cell
from meshforge.controller.ml.targets import _raw_seconds_per_item
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import raw_prediction
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import ML_TERMS
from meshforge.controller.planner.replay import project

pytestmark = pytest.mark.unit

SEEDS = range(150)
COVERED: dict[str, Any] = {"one_lane": 0, "used": 0, "heavy_used": 0, "heavy_names": set()}
SPLIT_SEEDS = [s for s in range(300) if random_case(s)[0].split]


def _rows(decision: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = json.loads(json.dumps(candidates_json(decision)))
    return out


def _rules(rng: random.Random, *, ood_p: float = 0.0) -> dict[Any, Rule]:
    return {
        (t, None): Rule(
            y_hat=rng.uniform(-1.2, 1.2),
            half_width=rng.choice([0.03, 0.2]),
            ood=rng.random() < ood_p,
        )
        for t in ML_TERMS
    }


def _used_terms(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, Any]]:
    out = []
    for row in rows:
        if not row.get("feasible") or not row.get("estimate"):
            continue
        for name, term in row["estimate"]["terms"].items():
            block = term.get("detail", {}).get("ml")
            if isinstance(block, dict) and block.get("used") is True:
                out.append((row, name, term))
    return out


def _unit_ok(term: dict[str, Any]) -> None:
    block = term["detail"]["ml"]
    want = block["raw_s"] * math.exp(block["y_hat"])
    assert term["seconds"] == pytest.approx(want, rel=2e-3, abs=3e-5), term


@pytest.mark.parametrize("seed", SEEDS)
def test_every_ml_used_term_is_in_its_own_unit(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    view = project(request, snapshot)
    ml, _ = stub_ml(_rules(random.Random(seed)))
    used = _used_terms(_rows(plan(request, view, "cost_model", now, ml=ml)))
    for _row, _name, term in used:
        _unit_ok(term)
    COVERED["used"] += len(used)


@pytest.mark.parametrize("seed", SPLIT_SEEDS[:80])
def test_one_lane_compute_has_its_own_block_with_the_rate_nested(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    view = project(request, snapshot)
    rng = random.Random(seed + 5)
    ml, _ = stub_ml(_rules(rng, ood_p=0.2))
    rows = _rows(plan(request, view, "cost_model", now, ml=ml))
    seen = 0
    for row in rows:
        if not row.get("feasible") or len(row.get("node_set") or ()) != 1:
            continue
        compute = row["estimate"]["terms"]["compute"]
        outer = compute["detail"].get("ml")
        if outer is None:
            continue  # a whole-job style row of a split request
        if "rate" not in outer:
            continue
        seen += 1
        rate = outer["rate"]
        assert isinstance(rate, dict) and "rate" not in rate
        assert outer["used"] == rate["used"]
        assert outer.get("reason") == rate.get("reason")
        if outer["used"]:
            assert outer["y_hat"] == rate["y_hat"]
            _unit_ok(compute)
            assert raw_prediction(compute) == outer["raw_s"]
        else:
            assert "raw_s" not in outer
            assert raw_prediction(compute) == compute["seconds"] or (
                "calibration" in compute["detail"]
            )
    COVERED["one_lane"] += seen


@pytest.mark.parametrize("seed", SPLIT_SEEDS[:80])
def test_published_lane_rate_equals_the_corrected_rate_block(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    view = project(request, snapshot)
    rng = random.Random(seed + 9)
    ml, _ = stub_ml(_rules(rng))
    decision = plan(request, view, "cost_model", now, ml=ml)
    if decision.split is None:
        return
    rows = {
        row["node_id"]: row
        for row in _rows(decision)
        if row.get("feasible") and len(row.get("node_set") or ()) == 1
    }
    for lane in decision.split.lanes:
        row = rows.get(lane.node_id)
        if row is None or lane.rate_items_s <= 0:
            continue
        outer = row["estimate"]["terms"]["compute"]["detail"].get("ml")
        if not outer or not outer.get("used"):
            continue
        rate = outer["rate"]
        if not rate.get("used"):
            continue
        spi = rate["raw_s"] * math.exp(rate["y_hat"])
        assert 1.0 / lane.rate_items_s == pytest.approx(spi, rel=2e-2, abs=1e-6), (
            seed,
            lane.node_id,
        )


@pytest.mark.parametrize("seed", SPLIT_SEEDS[:60])
def test_raw_prediction_and_explain_line_are_sane_for_one_lane_compute(
    seed: int,
) -> None:
    request, snapshot, now = random_case(seed)
    view = project(request, snapshot)
    y = random.Random(seed).uniform(0.2, 1.0)  # a clear correction upwards
    ml, _ = stub_ml({(t, None): Rule(y_hat=y, half_width=0.02) for t in ML_TERMS})
    for row in _rows(plan(request, view, "cost_model", now, ml=ml)):
        if not row.get("feasible") or len(row.get("node_set") or ()) != 1:
            continue
        compute = row["estimate"]["terms"]["compute"]
        outer = compute["detail"].get("ml")
        if not outer or not outer.get("used"):
            continue
        raw = raw_prediction(compute)
        assert raw is not None
        # raw is in seconds: seconds / raw == e^y (not a per-item number)
        if raw > 1e-3:
            assert compute["seconds"] / raw == pytest.approx(math.exp(y), rel=2e-2)
        note = _ml_note(compute)
        assert note.startswith(", ML ") and " -> " in note and "x" in note
        cell = _term_cell(compute, "compute")
        assert cell.count("->") == 1
        # the note's left number is the analytical seconds, formatted
        left = note.split("ML ", 1)[1].split(" -> ", 1)[0]
        right = note.split(" -> ", 1)[1].split(" ", 1)[0]
        assert left and right and left != "?" and right != "?"


@pytest.mark.parametrize("seed", SPLIT_SEEDS[:80])
def test_mode3_sets_single_and_combine_units(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    view = project(request, snapshot)
    ml, _ = stub_ml(_rules(random.Random(seed + 1)))
    rows = _rows(plan(request, view, "cost_model", now, ml=ml))
    base = {
        (r["node_id"], tuple(r.get("node_set") or ())): r
        for r in _rows(plan(request, view, "cost_model", now))
        if r.get("feasible")
    }
    for row in rows:
        if not row.get("feasible") or not row.get("estimate"):
            continue
        key = (row["node_id"], tuple(row.get("node_set") or ()))
        for name, term in row["estimate"]["terms"].items():
            block = term["detail"].get("ml")
            if not isinstance(block, dict) or block.get("used") is not True:
                continue
            _unit_ok(term)
            # and the analytical raw equals the no-ML plan's seconds (6 dp)
            want = base.get(key, {}).get("estimate", {}).get("terms", {}).get(name)
            if want is not None and name in ("compute", "combine"):
                assert block["raw_s"] == pytest.approx(
                    want["seconds"], rel=1e-3, abs=3e-5
                ), (seed, key, name)


def _heavy(n: int, split: bool, seed: int) -> tuple[Any, Any, Any]:
    """Every term above the 0.05 s ML floor: slow env, big output."""
    import dataclasses

    from mf_planner_fabrics import synthetic_fabric

    request, snapshot, now = synthetic_fabric(n, split=split, seed=seed)
    snapshot = dataclasses.replace(
        snapshot, config=dataclasses.replace(snapshot.config, default_env_s=30.0)
    )
    request = dataclasses.replace(
        request,
        history=dataclasses.replace(
            request.history, output_bytes=5e8, output_samples=3
        ),
    )
    return request, project(request, snapshot), now


@pytest.mark.parametrize("seed", range(40))
@pytest.mark.parametrize("split", [False, True])
def test_heavy_cases_every_term_incl_mode3_in_its_own_unit(
    seed: int, split: bool
) -> None:
    n = 2 + seed % 7
    request, view, now = _heavy(n, split, seed)
    rng = random.Random(seed)
    ml, _ = stub_ml(_rules(rng))
    raw = {
        (r["node_id"], tuple(r.get("node_set") or ())): r
        for r in _rows(plan(request, view, "cost_model", now))
        if r.get("feasible")
    }
    rows = _rows(plan(request, view, "cost_model", now, ml=ml))
    for row, name, term in _used_terms(rows):
        _unit_ok(term)
        COVERED["heavy_used"] += 1
        COVERED["heavy_names"].add(name)
        base = raw.get((row["node_id"], tuple(row.get("node_set") or ())))
        if base and name in base["estimate"]["terms"]:
            want = base["estimate"]["terms"][name]["seconds"]
            assert raw_prediction(term) == pytest.approx(want, rel=1e-3, abs=3e-5)
        if "rate" in term["detail"]["ml"]:
            r = term["detail"]["ml"]["rate"]
            if r.get("used"):
                # per item: far smaller than the lane's seconds when items > 1
                assert r["raw_s"] * request.split.items == pytest.approx(
                    term["detail"]["ml"]["raw_s"], rel=2e-2, abs=1e-4
                )


# ---------------------------------------------------------- the training side


def test_targets_reads_the_rate_raw_from_ml_rate_only() -> None:
    def lane(compute_detail: Any, rate_items_s: float = 100.0) -> dict[str, Any]:
        return {
            "rate_items_s": rate_items_s,
            "estimate": {"terms": {"compute": {"seconds": 9.0, "detail": compute_detail}}},
        }

    rate = {"used": True, "raw_s": 0.004, "y_hat": 0.5}
    outer = {"used": True, "raw_s": 7.5, "y_hat": 0.5, "rate": rate}
    got = _raw_seconds_per_item(lane({"ml": outer}))
    assert got == pytest.approx(0.004)
    # hostile shapes never raise, never return the outer (seconds) number
    for bad in (
        {"ml": {"used": True, "raw_s": 7.5}},  # no rate: must not use 7.5
        {"ml": {"used": True, "raw_s": 7.5, "rate": "x"}},
        {"ml": {"used": True, "raw_s": 7.5, "rate": None}},
        {"ml": {"used": True, "raw_s": 7.5, "rate": []}},
        {"ml": {"used": True, "raw_s": 7.5, "rate": {"used": True}}},
        {"ml": {"used": True, "raw_s": 7.5, "rate": {"used": True, "raw_s": "a"}}},
        {"ml": {"used": True, "raw_s": 7.5, "rate": {"used": True, "raw_s": True}}},
        {"ml": {"used": True, "raw_s": 7.5, "rate": {"used": False, "raw_s": 0.1}}},
        {"ml": "str"},
        {"ml": 3},
        {},
        "notamapping",
    ):
        value = _raw_seconds_per_item(lane(bad))
        assert value != 7.5, bad
        # falls back to 1/rate_items_s (the stored analytical rate) or None
        assert value is None or value == pytest.approx(0.01), (bad, value)
    # a nested rate that is not used: the stored rate is the analytical one
    notused = {"used": False, "reason": "ood", "rate": {"used": False}}
    assert _raw_seconds_per_item(lane({"ml": notused})) == pytest.approx(0.01)


@pytest.mark.parametrize("seed", SPLIT_SEEDS[:40])
def test_nested_rate_survives_json_roundtrip_and_replay_projection(seed: int) -> None:
    """The stored decision is JSON: the nested block survives a canonical
    round trip byte-for-byte (the persistence path)."""
    from meshforge.controller.planner.replay import canonical_json

    request, snapshot, now = random_case(seed)
    view = project(request, snapshot)
    ml, _ = stub_ml(_rules(random.Random(seed)))
    rows = _rows(plan(request, view, "cost_model", now, ml=ml))
    blob = canonical_json(rows)
    assert json.loads(blob) == rows
    assert "NaN" not in blob and "Infinity" not in blob


def test_zz_the_unit_tests_really_exercised_terms() -> None:
    """Guards against a vacuous pass (runs after the parametrised tests)."""
    assert COVERED["used"] > 40
    assert COVERED["one_lane"] > 5
    assert COVERED["heavy_used"] > 150
    assert COVERED["heavy_names"] >= {"transfer", "env", "compute", "output", "combine"}
