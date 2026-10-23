"""V0.15 G6a (D375): the classifier experiment's examples (the controller
side, pure).

* the label is the oracle configuration's class; per class the outcome is
  **the planner's best-ranked candidate of that class**, mapped to its
  configuration and scored with the evaluator's regret - so a right class can
  still cost regret (the planner's ``mode1`` node is not the oracle's);
* the cost-model outcome is exactly the evaluator's criterion row pick;
* Mode 3: ``mode3_k`` classes from the sets the planner ranked; a label of a
  size it never enumerated maps to the nearest one;
* features: fixed width, job sizes + min / max / mean over the feasible
  nodes + the node count; missing stays NaN;
* groups without a label or a usable replay are counted, not used;
* the examples document parses in the ``ml`` child.
"""

from __future__ import annotations

import json
import math

import pytest
from mf_eval_world import config_id, fabric, group, records

from meshforge.controller.planner_eval import classifier_examples as ce
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.model import (
    CRITERION_POLICY,
    EvalConfig,
    default_policies,
)

pytestmark = pytest.mark.unit

PREDICTED = {"node_a": 300.0, "node_b": 100.0, "node_c": 120.0, "node_d": 200.0}
"""The planner's compute seconds per node: it ranks node_b first."""


def _whole_case() -> tuple[list, list]:  # type: ignore[type-arg]
    request, snapshot = fabric(PREDICTED)
    # measured: node_c is the oracle (a mode1 node the planner ranks second)
    g = group(
        "g1",
        {"node_a": 400.0, "node_b": 150.0, "node_c": 100.0, "node_d": 300.0},
    )
    return [g], records(g, request, snapshot, reps=1)


def test_a_right_class_still_costs_the_planners_candidate_regret() -> None:
    groups, recs = _whole_case()
    (example,), counts = ce.build_examples(groups, recs, {})
    assert counts["examples"] == 1 and counts["groups"] == 1
    assert example.label == "mode1" and example.oracle_class == "mode1"
    mode1 = example.classes["mode1"]
    # the planner's best mode1 candidate is node_b (150 ms), not the oracle
    assert mode1["config_id"] == config_id("g1", "mode1", "node_b")
    assert mode1["status"] == "counted"
    assert mode1["regret_pct"] == pytest.approx(50.0)
    local = example.classes["local"]
    assert local["config_id"] == config_id("g1", "local", "node_a")
    assert local["regret_pct"] == pytest.approx(300.0)
    assert set(example.classes) == {"local", "mode1"}


def test_the_cost_model_outcome_is_the_evaluators_criterion_pick() -> None:
    request, snapshot = fabric(PREDICTED)
    groups = [
        group(f"g{i}", {"node_a": 400.0 - i, "node_b": 150.0 + i, "node_c": 100.0})
        for i in range(4)
    ]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=2)]
    examples, _ = ce.build_examples(groups, recs, {})
    report = evaluate(
        groups,
        recs,
        {},
        default_policies(),
        EvalConfig(),
        campaign_ids=["camp_1"],
    )
    for example, row in zip(examples, report["groups"], strict=True):
        (pick,) = [p for p in row["picks"] if p["key"] == CRITERION_POLICY.key]
        assert example.cost_model["config_id"] == pick["config_id"]
        assert example.cost_model["regret_pct"] == pick["regret_pct"]
        assert example.cost_model["votes"] == pick["votes"]
        assert example.records_used == row["records_used"]


def test_mode3_classes_and_the_nearest_enumerated_size() -> None:
    request, snapshot = fabric(
        {"node_a": 40.0, "node_b": 40.0, "node_c": 40.0, "node_d": 40.0},
        split_items=4000,
    )
    sets = {("node_a", "node_b", "node_c", "node_d"): 10.0}
    g = group("g3", {"node_a": 40.0, "node_b": 40.0}, sets=sets)
    (example,), _ = ce.build_examples([g], records(g, request, snapshot, reps=1), {})
    mode3 = [c for c in example.classes if c.startswith("mode3_")]
    assert mode3, example.classes
    assert example.oracle_class == "mode3_4"
    assert example.label == ce.nearest_class("mode3_4", sorted(example.classes))
    assert ce.nearest_class("mode3_4", ["local", "mode3_2", "mode3_3"]) == "mode3_3"
    assert ce.nearest_class("mode3_3", ["mode3_2", "mode3_4"]) == "mode3_2"
    assert ce.nearest_class("mode3_3", ["local", "mode1"]) == "mode3_3"
    assert ce.nearest_class("mode1", ["local"]) == "mode1"


def test_features_are_fixed_width_and_missing_stays_nan() -> None:
    groups, recs = _whole_case()
    (example,), _ = ce.build_examples(groups, recs, {})
    names = list(ce.FEATURE_NAMES)
    assert len(example.features) == len(names)
    value = dict(zip(names, example.features, strict=True))
    assert value["node_count"] == 4.0
    assert value["job_input_bytes"] == 10 * 10**6
    assert math.isnan(value["job_items"])  # a whole job has no items
    assert math.isnan(value["mean_hw_gpu_tflops"])  # no node reported a GPU
    assert value["min_hw_cpu_gflops"] == value["max_hw_cpu_gflops"] == 100.0
    assert value["max_net_local"] == 1.0 and value["min_net_local"] == 0.0
    assert "net_term_bw_bytes_s" not in ce.NODE_FEATURES


def test_unusable_groups_are_counted_not_used() -> None:
    request, snapshot = fabric(PREDICTED)
    good = group("g1", {"node_a": 400.0, "node_b": 150.0})
    unlabelled = group("g2", {"node_a": 1.0}, label="insufficient", reason="n<3")
    silent = group("g3", {"node_a": 400.0, "node_b": 150.0})
    recs = [
        *records(good, request, snapshot, reps=1),
        *records(silent, request, snapshot, reps=1, replay=None),
    ]
    examples, counts = ce.build_examples([good, unlabelled, silent], recs, {})
    assert [e.group_id for e in examples] == ["g1"]
    assert counts["not_labelled"] == 1
    assert counts["no_replay"] == 1
    assert counts["record:no_replay"] == 2
    assert counts["groups"] == 3


def test_the_document_round_trips_into_the_ml_child() -> None:
    pytest.importorskip("lightgbm", reason="needs the ml extra")
    from meshforge.controller.ml.train.classify import parse_examples

    groups, recs = _whole_case()
    examples, counts = ce.build_examples(groups, recs, {})
    doc = ce.examples_document(
        examples, counts, campaign_ids=["camp_1"], calibrations={}
    )
    text = json.dumps(doc, allow_nan=False)  # NaN written as null
    names, parsed = parse_examples(json.loads(text))
    assert names == list(ce.FEATURE_NAMES)
    assert parsed[0].label == "mode1"
    assert math.isnan(parsed[0].features[names.index("job_items")])
