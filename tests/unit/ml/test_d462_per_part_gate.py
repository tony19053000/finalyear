"""D462 (ADR-0026): the per-part ML activation gate and serving.

* the gate approves only the parts that pass; a set with some approved parts
  is accepted and records ``approved_terms`` / ``rejected_terms``;
* no part passes -> rejected exactly as before (all-or-nothing verdict);
* serving loads only the approved parts (``not_approved`` for the others),
  and a set gated before D462 still serves every learned part;
* the regret check judges the partial set and, when it rejects, empties
  ``approved_terms``;
* the planner uses ML for approved parts, calibration (reason
  ``not_approved``) for the others, and keeps the per-case fallback;
* the CLI / API views name the source of every part.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
from mf_ml_stub import Rule, stub_ml
from mf_ml_synthetic import synthetic_rows
from mf_planner_fabrics import synthetic_fabric

from meshforge.cli.ml_model_commands import ml_set_table, ml_sets_table
from meshforge.common.ml_explain import ml_line, term_ml
from meshforge.controller.ml.folds import loaded_set
from meshforge.controller.ml.regret import RegretOutcome, apply
from meshforge.controller.ml.serve import NOT_APPROVED, approved_terms
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.gate import Reference, run_gate
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import Calibration, Factor, TermFactors
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import project
from meshforge.controller.planner_eval.regret_gate import RegretCheck

pytestmark = pytest.mark.unit

TWO = CompareSettings(
    terms=("compute", "env"), algorithms=("ridge", "lightgbm"), min_rows=100
)


def _rows(noisy: tuple[str, ...]) -> list[dict[str, Any]]:
    """Signal on every term except ``noisy`` (pure noise: ML cannot beat
    the calibration there)."""
    rows = synthetic_rows(
        "signal",
        families=6,
        groups_per_family=5,
        runs_per_group=10,
        terms=("compute", "env"),
    )
    rng = random.Random(5)  # noqa: S311 - reproducible test data
    for row in rows:
        if row["term"] in noisy:
            y = rng.gauss(0.0, 0.3)
            row["y"] = y
            row["actual_s"] = row["raw_s"] * math.exp(y)
    return rows


@pytest.fixture(scope="module")
def partial() -> tuple[Any, Any, Any]:
    table = table_from_rows(_rows(noisy=("env",)))
    shipped = train_set(table, TWO)
    return table, shipped, run_gate(table, TWO, shipped, Reference())


def test_the_gate_approves_only_the_parts_that_pass(partial: Any) -> None:
    _table, _shipped, gate = partial
    assert gate.accepted, gate.reason
    assert gate.doc["per_term"] is True
    assert gate.doc["approved_terms"] == ["compute"]
    assert gate.doc["terms"]["compute"]["approved"] is True
    assert gate.doc["terms"]["env"]["approved"] is False
    # env is learned but rejected (or a baseline pick): never approved
    assert "compute" not in gate.doc["rejected_terms"]
    assert gate.rule is None and gate.reason is None


def test_no_passing_part_is_rejected_as_before() -> None:
    table = table_from_rows(_rows(noisy=("compute", "env")))
    shipped = train_set(table, TWO)
    gate = run_gate(table, TWO, shipped, Reference())
    assert not gate.accepted
    assert gate.rule == "not_better"
    assert gate.doc["approved_terms"] == []
    assert gate.reason is not None and gate.reason.startswith("not_better: ")


def test_all_parts_passing_approves_all() -> None:
    table = table_from_rows(_rows(noisy=()))
    shipped = train_set(table, TWO)
    gate = run_gate(table, TWO, shipped, Reference())
    assert gate.accepted, gate.reason
    learned = sorted(m.term for m in shipped.members if m.learned)
    assert gate.doc["approved_terms"] == learned


def _child_doc(shipped: Any, gate: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
    members = [
        {
            "term": m.term,
            "algorithm": m.algorithm,
            "version": m.version,
            "reason": m.reason,
        }
        for m in shipped.members
    ]
    artefacts = {
        m.version: m.artefact for m in shipped.members if m.artefact is not None
    }
    doc = {
        "set_version": shipped.set_version,
        "feature_version": shipped.feature_version,
        "members": members,
        "gate": gate.doc,
        "accepted": gate.accepted,
    }
    return doc, artefacts


def test_serving_loads_only_the_approved_parts(partial: Any) -> None:
    _table, shipped, gate = partial
    doc, artefacts = _child_doc(shipped, gate)
    loaded = loaded_set(doc, artefacts)
    assert set(loaded.models) == {"compute"}
    if any(m.term == "env" and m.learned for m in shipped.members):
        assert loaded.without_model["env"] == NOT_APPROVED
        assert loaded.not_approved == frozenset({"env"})


def test_a_set_gated_before_d462_serves_every_learned_part(partial: Any) -> None:
    _table, shipped, gate = partial
    old = {k: v for k, v in gate.doc.items() if k != "approved_terms"}
    assert approved_terms(old) is None
    doc, artefacts = _child_doc(shipped, gate)
    doc["gate"] = old
    loaded = loaded_set(doc, artefacts)
    assert set(loaded.models) == {m.term for m in shipped.members if m.learned}
    assert loaded.not_approved == frozenset()


def test_a_regret_rejection_serves_no_part(partial: Any) -> None:
    _table, shipped, gate = partial
    doc, _ = _child_doc(shipped, gate)
    check = RegretCheck("rejected", 12, 2.0, 1.0, "mean regret worse")
    out = apply(doc, RegretOutcome(check, None, None))
    assert out["accepted"] is False
    assert out["gate"]["approved_terms"] == []
    assert out["gate"]["rule"] == "regret_worse"
    passed = apply(
        doc, RegretOutcome(RegretCheck("passed", 12, 1.0, 1.0, None), None, None)
    )
    assert passed["gate"]["approved_terms"] == ["compute"]


# ------------------------------------------------------------------ planner


def _inputs() -> tuple[Any, Any, Any]:
    request, snapshot, now = synthetic_fabric(4, split=False, seed=4)
    return request, project(request, snapshot), now


def _calibration(factor: float = 2.0) -> Calibration:
    one = Factor(factor=factor, n=10, median=factor)
    return Calibration(
        version=7,
        terms={
            name: TermFactors(global_=one)
            for name in ("transfer", "env", "compute", "output")
        },
    )


def _feasible(decision: Any) -> list[Any]:
    return [c for c in decision.candidates if c.feasible and c.estimate is not None]


def test_planner_uses_ml_for_approved_parts_calibration_for_the_rest() -> None:
    request, view, now = _inputs()
    cal = _calibration(2.0)
    base = plan(request, view, "cost_model", now)
    calibrated = plan(request, view, "cost_model", now, calibration=cal)
    ml, stub = stub_ml(
        {("compute", None): Rule(y_hat=math.log(0.5)), ("env", None): Rule()}
    )
    ml = replace(ml, withheld=frozenset({"env"}))
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    assert all(q.term != "env" for q in stub.queries)  # never asked
    for row in _feasible(decision):
        raw = next(
            c
            for c in base.candidates
            if c.node_id == row.node_id and c.feasible and not c.node_set
        ).estimate
        want = next(
            c
            for c in calibrated.candidates
            if c.node_id == row.node_id and c.feasible and not c.node_set
        ).estimate
        compute, env = row.estimate.compute, row.estimate.env
        assert compute.detail["ml"]["used"] is True
        assert compute.seconds == pytest.approx(raw.compute.seconds * 0.5)
        assert env.detail["ml"] == {
            "model_version": "stub-v1",
            "used": False,
            "reason": "not_approved",
        }
        assert env.seconds == want.env.seconds
        assert env.detail["calibration"] == want.env.detail["calibration"]


def test_an_approved_part_without_a_prediction_falls_back_per_case() -> None:
    request, view, now = _inputs()
    cal = _calibration(2.0)
    calibrated = plan(request, view, "cost_model", now, calibration=cal)
    ml, _ = stub_ml({("compute", None): Rule(ood=True, ood_reason="range:x")})
    ml = replace(ml, withheld=frozenset({"env"}))
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    for row in _feasible(decision):
        want = next(
            c
            for c in calibrated.candidates
            if c.node_id == row.node_id and c.feasible and not c.node_set
        ).estimate
        compute = row.estimate.compute
        assert compute.detail["ml"]["reason"] == "ood"
        assert compute.seconds == want.compute.seconds
        assert row.estimate.env.detail["ml"]["reason"] == "not_approved"


def test_nothing_withheld_is_the_old_behaviour() -> None:
    request, view, now = _inputs()
    ml, _ = stub_ml({("compute", None): Rule(y_hat=0.1)})
    a = plan(request, view, "cost_model", now, ml=ml)
    b = plan(request, view, "cost_model", now, ml=replace(ml, withheld=frozenset()))
    assert candidates_json(a) == candidates_json(b)
    for row in _feasible(a):
        assert row.estimate.env.detail["ml"]["reason"] == "no_model"


def test_explain_json_names_the_source_of_every_part() -> None:
    request, view, now = _inputs()
    cal = _calibration(2.0)
    ml, _ = stub_ml({("compute", None): Rule(y_hat=0.1), ("env", None): Rule()})
    ml = replace(ml, withheld=frozenset({"env"}))
    decision = plan(request, view, "cost_model", now, calibration=cal, ml=ml)
    rows = candidates_json(decision)
    terms = next(r for r in rows if r.get("estimate"))["estimate"]["terms"]
    assert terms["compute"]["detail"]["ml"]["used"] is True
    env = terms["env"]
    assert env["detail"]["ml"]["reason"] == "not_approved"
    line = ml_line(term_ml(env))
    assert line is not None and "not approved by the gate" in line


# ------------------------------------------------------------------ views


def _api_set() -> dict[str, Any]:
    return {
        "set_version": "sha256:" + "a" * 64,
        "status": "active",
        "created_at": "2026-11-21T00:00:00Z",
        "reason": None,
        "approved_terms": ["env", "combine"],
        "members": [
            {"term": "env", "algorithm": "knn", "serves": "ml", "metrics": {}},
            {
                "term": "compute",
                "algorithm": "lightgbm",
                "serves": "calibration",
                "gate_reason": "worse_than_reference: compute",
                "metrics": {},
            },
        ],
    }


def _text(table: Any) -> str:
    from rich.console import Console

    console = Console(width=200, record=True)
    console.print(table)
    return console.export_text()


def test_cli_models_shows_ml_parts_and_sources() -> None:
    listing = _text(ml_sets_table({"sets": [_api_set()]}))
    assert "ML PARTS" in listing and "env, combine" in listing
    assert "compute=lightgbm (cal)" in listing
    members = _text(ml_set_table(_api_set()))
    assert "SERVES" in members and "calibration" in members
    assert "worse_than_reference" in members
    old = dict(_api_set(), approved_terms=None)
    assert "all learned" in _text(ml_sets_table({"sets": [old]}))
