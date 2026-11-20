"""D462 tester: adversarial tests for the per-part ML gate and serving,
derived from the task criteria (not from the implementation)."""

from __future__ import annotations

import asyncio
import datetime as dt
import math
import random
from dataclasses import replace
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
from mf_ml_synthetic import synthetic_rows

from meshforge.cli.ml_model_commands import ml_set_table, ml_sets_table
from meshforge.controller.api.ml_model_routes import _members, _set
from meshforge.controller.ml.folds import loaded_set, planner_input
from meshforge.controller.ml.registry import ModelRegistry, SetMember, SetRow
from meshforge.controller.ml.regret import RegretOutcome, apply
from meshforge.controller.ml.serve import (
    NOT_APPROVED,
    LoadedSet,
    approved_terms,
    load_model,
)
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.gate import (
    GATE_RULES,
    GateSettings,
    Reference,
    run_gate,
)
from meshforge.controller.planner_eval.regret_gate import RegretCheck

pytestmark = pytest.mark.unit

TWO = CompareSettings(
    terms=("compute", "env"), algorithms=("ridge", "lightgbm"), min_rows=100
)


def _rows(noisy: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    rows = synthetic_rows(
        "signal",
        families=6,
        groups_per_family=5,
        runs_per_group=10,
        terms=("compute", "env"),
    )
    rng = random.Random(5)  # noqa: S311
    for row in rows:
        if row["term"] in noisy:
            y = rng.gauss(0.0, 0.3)
            row["y"] = y
            row["actual_s"] = row["raw_s"] * math.exp(y)
    return rows


@pytest.fixture(scope="module")
def signal() -> tuple[Any, Any]:
    table = table_from_rows(_rows())
    return table, train_set(table, TWO)


@pytest.fixture(scope="module")
def partial() -> tuple[Any, Any, Any]:
    table = table_from_rows(_rows(noisy=("env",)))
    shipped = train_set(table, TWO)
    return table, shipped, run_gate(table, TWO, shipped, Reference())


def _none_member(member: Any) -> Any:
    return replace(
        member,
        algorithm="none",
        artefact=None,
        config=None,
        reason="no_learned_model: test",
    )


def _env_none(shipped: Any) -> Any:
    members = tuple(_none_member(m) if m.term == "env" else m for m in shipped.members)
    return replace(shipped, members=members)


# ---------------------------------------------------------------- criterion 1


def test_c1_approved_terms_each_beat_calibration_and_fail_no_rule(
    partial: Any,
) -> None:
    _t, shipped, gate = partial
    assert gate.accepted
    learned = {m.term for m in shipped.members if m.learned}
    approved = set(gate.doc["approved_terms"])
    rejected = set(gate.doc["rejected_terms"])
    assert approved, "need >=1 approved"
    assert approved | rejected == learned
    assert not approved & rejected
    for term in approved:
        t = gate.doc["terms"][term]
        assert t["male_calibration"] - t["male_new"] > 0.02
        assert t["failed"] == []
        assert t["approved"] is True
    for term, why in gate.doc["rejected_terms"].items():
        assert gate.doc["terms"][term]["approved"] is False
        assert why.split(":")[0] in (*GATE_RULES,), why


def test_c1_rejected_term_reason_is_first_failed_rule_in_rule_order(
    partial: Any,
) -> None:
    _t, _s, gate = partial
    for term, why in gate.doc["rejected_terms"].items():
        failed = gate.doc["terms"][term]["failed"]
        if failed:
            first = min(failed, key=GATE_RULES.index)
            assert why.startswith(first + ":"), (term, failed, why)


def test_c1_gate_is_deterministic(partial: Any) -> None:
    table, shipped, gate = partial
    again = run_gate(table, TWO, shipped, Reference())
    assert again.doc == gate.doc and again.accepted == gate.accepted


@pytest.mark.parametrize(
    ("gate_settings", "compare", "rule"),
    [
        (GateSettings(min_held_rows=10**6), TWO, "too_few_held_out"),
        (GateSettings(), replace(TWO, coverage_reject=1.01), "coverage"),
        (GateSettings(), replace(TWO, family_tolerance_log=-5.0), "family_tolerance"),
    ],
)
def test_c1_every_term_failing_a_rule_rejects_as_before(
    signal: Any, gate_settings: Any, compare: Any, rule: str
) -> None:
    table, shipped = signal
    gate = run_gate(table, compare, shipped, Reference(), gate_settings)
    assert not gate.accepted
    assert gate.rule == rule
    assert gate.doc["approved_terms"] == []
    assert gate.reason is not None and gate.reason.startswith(rule + ": ")
    assert rule in gate.doc["rules_failed"]
    for term in (m.term for m in shipped.members if m.learned):
        assert gate.doc["terms"][term]["approved"] is False


def test_c1_set_without_any_learned_member_is_rejected() -> None:
    table = table_from_rows(_rows())
    cfg = replace(TWO, min_rows=10**6)
    shipped = train_set(table, cfg)
    assert not shipped.learned
    gate = run_gate(table, cfg, shipped, Reference())
    assert not gate.accepted
    assert gate.rule == "not_better"
    assert gate.doc["approved_terms"] == []


def test_c1_single_term_set() -> None:
    cfg = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
    table = table_from_rows(_rows())
    shipped = train_set(table, cfg)
    gate = run_gate(table, cfg, shipped, Reference())
    assert gate.accepted, gate.reason
    assert gate.doc["approved_terms"] == ["compute"]


# ---------------------------------------------------------------- criterion 2


def _ref_with_env(shipped: Any) -> Reference:
    env = shipped.member("env")
    assert env is not None and env.artefact is not None
    return Reference(
        kind="active",
        set_version="sha256:ref",
        models={"env": load_model(env.artefact, expected_version=env.version)},
    )


def test_c2_non_approved_term_worse_than_reference_rejects_the_set(
    signal: Any,
) -> None:
    table, shipped = signal
    gate = run_gate(table, TWO, _env_none(shipped), _ref_with_env(shipped))
    assert not gate.accepted
    assert gate.rule == "worse_than_reference"
    assert gate.reason is not None and "env" in gate.reason
    assert gate.doc["approved_terms"] == []


def test_c2_same_set_without_reference_model_is_accepted(signal: Any) -> None:
    table, shipped = signal
    gate = run_gate(table, TWO, _env_none(shipped), Reference())
    assert gate.accepted, gate.reason
    assert gate.doc["approved_terms"] == ["compute"]


def test_c2_within_tolerance_does_not_block(signal: Any) -> None:
    table, shipped = signal
    wide = GateSettings(tolerance=10.0)
    gate = run_gate(table, TWO, _env_none(shipped), _ref_with_env(shipped), wide)
    assert gate.accepted, gate.reason


def test_c2_learned_not_approved_term_never_approved_with_foreign_reference(
    signal: Any,
) -> None:
    _table, shipped = signal
    ref = _ref_with_env(shipped)
    noisy_table = table_from_rows(_rows(noisy=("env",)))
    gate = run_gate(noisy_table, TWO, train_set(noisy_table, TWO), ref)
    assert "env" not in gate.doc["approved_terms"]


# ---------------------------------------------------------------- criterion 4/5


def _doc(shipped: Any, gate_doc: Any) -> tuple[dict[str, Any], dict[str, bytes]]:
    members = [
        {
            "term": m.term,
            "algorithm": m.algorithm,
            "version": m.version,
            "reason": m.reason,
        }
        for m in shipped.members
    ]
    arts = {m.version: m.artefact for m in shipped.members if m.artefact}
    return (
        {
            "set_version": shipped.set_version,
            "feature_version": shipped.feature_version,
            "members": members,
            "gate": gate_doc,
        },
        arts,
    )


@pytest.mark.parametrize(
    "gate_doc", [None, {}, {"approved_terms": None}, {"approved_terms": "env"}]
)
def test_c5_malformed_or_missing_approved_terms_serve_every_learned(
    signal: Any, gate_doc: Any
) -> None:
    _t, shipped = signal
    assert approved_terms(gate_doc) is None
    doc, arts = _doc(shipped, gate_doc)
    loaded = loaded_set(doc, arts)
    assert set(loaded.models) == {m.term for m in shipped.members if m.learned}
    assert loaded.not_approved == frozenset()


def test_c4_empty_approved_terms_serves_nothing(signal: Any) -> None:
    _t, shipped = signal
    doc, arts = _doc(shipped, {"approved_terms": []})
    loaded = loaded_set(doc, arts)
    assert not loaded.models
    assert planner_input(loaded, 1.0) is None


def test_c3_none_member_keeps_its_own_reason_not_not_approved(signal: Any) -> None:
    _t, shipped = signal
    doc, arts = _doc(_env_none(shipped), {"approved_terms": ["compute"]})
    loaded = loaded_set(doc, arts)
    assert loaded.without_model["env"].startswith("no_learned_model")
    assert "env" not in loaded.not_approved


def test_c3_planner_input_carries_withheld(signal: Any) -> None:
    _t, shipped = signal
    doc, arts = _doc(shipped, {"approved_terms": ["compute"]})
    ml = planner_input(loaded_set(doc, arts), 1.0)
    assert ml is not None
    assert ml.withheld == frozenset({"env"})
    assert ml.predictor.has_model("compute")
    assert not ml.predictor.has_model("env")


def test_c4_regret_rejection_empties_approved(partial: Any) -> None:
    _t, shipped, gate = partial
    doc, _ = _doc(shipped, gate.doc)
    doc["accepted"] = True
    bad = apply(
        doc,
        RegretOutcome(RegretCheck("rejected", 12, 2.0, 1.0, "worse"), None, None),
    )
    assert bad["accepted"] is False
    assert bad["gate"]["approved_terms"] == []
    assert bad["gate"]["rule"] == "regret_worse"
    assert doc["gate"]["approved_terms"] == gate.doc["approved_terms"]  # not mutated
    d2, arts = _doc(shipped, bad["gate"])
    assert not loaded_set(d2, arts).models
    out = apply(
        doc, RegretOutcome(RegretCheck("skipped", 0, None, None, "x"), None, None)
    )
    assert out["gate"]["approved_terms"] == gate.doc["approved_terms"]


# ------------------------------------------------- registry load (fake rows)


def _row(shipped: Any, gate: dict[str, Any], status: str = "active") -> SetRow:
    now = dt.datetime(2026, 11, 21, tzinfo=dt.UTC)
    members = tuple(
        SetMember(
            term=m.term,
            version=m.version,
            algorithm=m.algorithm,
            feature_version=shipped.feature_version,
            params={},
            metrics={},
            reason=m.reason,
            artefact_digest=None if m.artefact is None else "d-" + m.version,
            artefact_bytes=None if m.artefact is None else len(m.artefact),
        )
        for m in shipped.members
    )
    return SetRow(
        set_version=shipped.set_version,
        created_at=now,
        status_changed_at=now,
        status=status,
        reason=None,
        gate=gate,
        dataset_digest=shipped.dataset_digest,
        feature_version=shipped.feature_version,
        inputs={},
        members=members,
    )


def _load(row: SetRow, shipped: Any) -> tuple[LoadedSet, list[str]]:
    reg = object.__new__(ModelRegistry)
    fetched: list[str] = []
    by_digest = {"d-" + m.version: m.artefact for m in shipped.members if m.artefact}

    async def artefact(member: SetMember) -> bytes:
        fetched.append(member.term)
        return by_digest[member.artefact_digest or ""]

    reg.artefact = artefact  # type: ignore[method-assign]
    return asyncio.run(reg.load(row)), fetched


def test_registry_load_only_fetches_and_loads_approved(signal: Any) -> None:
    _t, shipped = signal
    loaded, fetched = _load(_row(shipped, {"approved_terms": ["compute"]}), shipped)
    assert set(loaded.models) == {"compute"}
    assert fetched == ["compute"]
    assert loaded.without_model["env"] == NOT_APPROVED
    assert loaded.not_approved == frozenset({"env"})


def test_registry_load_pre_d462_gate_loads_all(signal: Any) -> None:
    _t, shipped = signal
    loaded, fetched = _load(_row(shipped, {"accepted": True}), shipped)
    assert set(loaded.models) == {m.term for m in shipped.members if m.learned}
    assert sorted(fetched) == sorted(loaded.models)
    assert loaded.not_approved == frozenset()


def test_registry_load_empty_approved_loads_none(signal: Any) -> None:
    _t, shipped = signal
    loaded, fetched = _load(_row(shipped, {"approved_terms": []}), shipped)
    assert not loaded.models and not fetched


def test_registry_load_ignores_unknown_approved_term(signal: Any) -> None:
    _t, shipped = signal
    loaded, _ = _load(_row(shipped, {"approved_terms": ["compute", "ghost"]}), shipped)
    assert set(loaded.models) == {"compute"}


def test_registry_load_missing_artefact_of_withheld_term_is_irrelevant(
    signal: Any,
) -> None:
    _t, shipped = signal
    row = _row(shipped, {"approved_terms": ["compute"]})
    members = tuple(
        replace(m, artefact_digest="gone") if m.term == "env" else m
        for m in row.members
    )
    loaded, _ = _load(replace(row, members=members), shipped)
    assert set(loaded.models) == {"compute"}


# ---------------------------------------------------------------- criterion 6


def test_c6_api_fields_only_added_and_correct(signal: Any) -> None:
    _t, shipped = signal
    row = _row(
        shipped,
        {
            "approved_terms": ["compute"],
            "rejected_terms": {"env": "coverage: env: 0.5 < 0.7"},
        },
    )
    out = _set(row).model_dump()
    assert out["approved_terms"] == ["compute"]
    by = {m["term"]: m for m in out["members"]}
    assert by["compute"]["serves"] == "ml" and by["compute"]["gate_reason"] is None
    assert by["env"]["serves"] == "calibration"
    assert by["env"]["gate_reason"] == "coverage: env: 0.5 < 0.7"
    for key in (
        "set_version",
        "status",
        "created_at",
        "reason",
        "members",
        "dataset_digest",
        "feature_version",
    ):
        assert key in out
    for key in ("term", "version", "algorithm", "metrics", "reason", "artefact_bytes"):
        assert key in by["env"]
    old = _set(_row(shipped, {"accepted": True})).model_dump()
    assert old["approved_terms"] is None
    assert all(m["serves"] == "ml" for m in old["members"] if m["algorithm"] != "none")


def test_c6_rejected_set_serves_calibration_everywhere(signal: Any) -> None:
    _t, shipped = signal
    for gate in ({"accepted": False}, {"approved_terms": ["compute"]}):
        row = _row(shipped, gate, status="rejected")
        assert all(m.serves == "calibration" for m in _members(row))


def test_c6_none_member_serves_calibration_even_pre_d462(signal: Any) -> None:
    _t, shipped = signal
    row = _row(_env_none(shipped), {})
    by = {m.term: m for m in _members(row)}
    assert by["env"].serves == "calibration"
    assert by["compute"].serves == "ml"


def test_c6_cli_robust_to_old_and_odd_payloads() -> None:
    from rich.console import Console

    def text(t: Any) -> str:
        c = Console(width=220, record=True)
        c.print(t)
        return c.export_text()

    old = {
        "sets": [
            {
                "set_version": "sha256:a",
                "status": "active",
                "members": [{"term": "compute", "algorithm": "ridge", "metrics": {}}],
            }
        ]
    }
    out = text(ml_sets_table(old))
    assert "all learned" in out and "(cal)" not in out
    new = {
        "sets": [
            {
                "set_version": "x",
                "status": "rejected",
                "approved_terms": [],
                "members": [],
            }
        ]
    }
    assert "none" in text(ml_sets_table(new))
    odd = {
        "sets": [
            {
                "set_version": "x",
                "status": "active",
                "approved_terms": ["[bold]x"],
                "members": [],
            }
        ]
    }
    text(ml_sets_table(odd))
    text(
        ml_set_table(
            {
                "members": [
                    {"term": "a", "algorithm": "ridge", "metrics": {}, "serves": None}
                ]
            }
        )
    )
    text(ml_set_table({"members": "garbage"}))
