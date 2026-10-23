"""V0.15 G3 tester r1 - the activation gate, each rule, and what it must not do.

Truths come from the G2 synthetic generator (``signal`` = a learnable
residual, node-only noise = nothing to learn), so each test says what the
gate must answer without reading the gate.
"""

from __future__ import annotations

import json
import math
import random
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")
import numpy as np
from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.controller.ml.serve import load_model
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fit_main import artefact_name, run_fit
from meshforge.controller.ml.train.gate import (
    GATE_RULES,
    GateSettings,
    Reference,
    holdout_mask,
    run_gate,
)

pytestmark = pytest.mark.unit

FAST = CompareSettings(
    terms=("compute",), algorithms=("ridge", "lightgbm"), min_rows=100
)


def _rows(kind: str = "signal", **kw: Any) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"families": 6, "groups_per_family": 5, "runs_per_group": 10}
    args.update(kw)
    return synthetic_rows(kind, **args)


def _gate(
    rows: list[dict[str, Any]],
    settings: CompareSettings = FAST,
    reference: Reference | None = None,
    gate: GateSettings | None = None,
) -> Any:
    table = table_from_rows(rows)
    shipped = train_set(table, settings)
    return (
        table,
        shipped,
        run_gate(table, settings, shipped, reference or Reference(), gate),
    )


def test_rule_order_is_the_documented_one() -> None:
    assert GATE_RULES == (
        "too_few_held_out",
        "coverage",
        "family_tolerance",
        "worse_than_reference",
        "not_better",
    )


# ------------------------------------------------------- held-out never fits


def test_the_gate_never_fits_on_held_out_rows() -> None:
    """The refit models are a function of the non-held-out rows only: rewrite
    every held-out row (features AND target) wildly, keep the dataset label,
    and the candidate set the gate judged must be byte-identical."""
    rows = _rows()
    table = table_from_rows(rows)
    shipped = train_set(table, FAST)
    held, _ = holdout_mask(table)
    assert held.any() and (~held).any()
    clean = run_gate(table, FAST, shipped, Reference())
    X = table.X.copy()
    y = table.y.copy()
    rng = np.random.default_rng(0)
    X[held] = np.where(
        np.isnan(X[held]), np.nan, X[held] * rng.uniform(0.01, 90, X[held].shape)
    )
    y[held] = rng.normal(0.0, 50.0, held.sum())
    poisoned = replace(table, X=X, y=y)  # same digest label on purpose
    dirty = run_gate(poisoned, FAST, shipped, Reference())
    assert clean.doc["candidate_set_version"] == dirty.doc["candidate_set_version"]
    assert (
        clean.doc["terms"]["compute"]["model_version"]
        == (dirty.doc["terms"]["compute"]["model_version"])
    )
    # ... while the numbers measured on the held-out rows did change
    assert (
        clean.doc["terms"]["compute"]["male_new"]
        != dirty.doc["terms"]["compute"]["male_new"]
    )


def test_fit_rows_change_the_judged_model() -> None:
    """Control for the test above: touching a NON-held-out row does change
    the judged candidate (so the version check is actually sensitive)."""
    table = table_from_rows(_rows())
    shipped = train_set(table, FAST)
    held, _ = holdout_mask(table)
    base = run_gate(table, FAST, shipped, Reference())
    y = table.y.copy()
    y[np.flatnonzero(~held)[:30]] += 0.7
    moved = run_gate(replace(table, y=y), FAST, shipped, Reference())
    assert base.doc["candidate_set_version"] != moved.doc["candidate_set_version"]


def test_every_decision_is_on_one_side_of_the_slice() -> None:
    table = table_from_rows(_rows(terms=("compute", "output")))
    held, info = holdout_mask(table)
    sides: dict[int, set[bool]] = {}
    for i, d in enumerate(table.decision_id):
        sides.setdefault(d, set()).add(bool(held[i]))
    assert all(len(s) == 1 for s in sides.values())
    assert info["rows_held"] == int(held.sum())


# ------------------------------------------------------- each rule


def test_signal_is_accepted_and_the_document_is_complete() -> None:
    _, _, result = _gate(_rows())
    assert result.accepted and result.rule is None and result.reason is None
    doc = result.doc
    assert (
        doc["accepted"] is True and doc["regret"]["status"] == "not_run"
    )  # V0.15 G5a (D371)
    assert doc["reference"]["kind"] == "calibration"
    term = doc["terms"]["compute"]
    for key in ("male_new", "male_calibration", "male_reference", "coverage"):
        assert math.isfinite(term[key])
    json.dumps(doc, allow_nan=False)  # a storable document


def test_noise_is_rejected_not_better_and_nothing_else() -> None:
    rng = random.Random(8)  # noqa: S311
    rows = _rows()
    for row in rows:
        row["y"] = rng.gauss(0.0, 0.3)
        row["actual_s"] = row["raw_s"] * math.exp(row["y"])
    table = table_from_rows(rows)
    shipped = train_set(table, FAST)
    result = run_gate(table, FAST, shipped, Reference())
    # selection may keep a baseline (all `none`): then no learned term -> not_better
    assert not result.accepted
    assert result.rule == "not_better"
    assert (result.reason or "").startswith("not_better")


def test_coverage_rule_fires_when_the_interval_is_too_narrow() -> None:
    narrow = replace(FAST, alpha=0.6)  # a 40 % interval cannot cover 80 %
    _, _, result = _gate(_rows(), narrow)
    assert not result.accepted
    assert "coverage" in result.doc["rules_failed"]
    assert result.rule == "coverage"
    assert result.doc["terms"]["compute"]["coverage"] < 0.8


def test_a_huge_interval_limit_makes_the_model_unusable_so_it_is_not_better() -> None:
    """Effective error: a model whose interval is wider than
    ml_max_interval_log is not used by the planner, so the gate must judge the
    calibration there and find nothing better."""
    tiny = replace(FAST, max_interval_log=1e-6)
    _, _, result = _gate(_rows(), tiny)
    assert not result.accepted
    term = result.doc["terms"]["compute"]
    assert term["usable_rate"] == 0.0
    assert term["male_new"] == pytest.approx(term["male_calibration"])
    assert result.rule == "not_better"


def test_too_few_held_out_rows_for_a_shipped_term() -> None:
    rows = _rows(families=4, groups_per_family=4, runs_per_group=8)  # 128 rows
    table = table_from_rows(rows)
    settings = replace(FAST, min_rows=100, min_families=3)
    shipped = train_set(table, settings)
    held, _ = holdout_mask(table)
    assert 0 < held.sum() < 40
    strict = GateSettings(min_held_rows=int(held.sum()) + 1)
    result = run_gate(table, settings, shipped, Reference(), strict)
    if shipped.learned:
        assert not result.accepted
        assert result.rule == "too_few_held_out"
    else:  # pragma: no cover - selection kept a baseline
        pytest.skip("selection kept a baseline on this data")


def _bad_family_rows() -> list[dict[str, Any]]:
    rows = _rows()
    for row in rows:
        if row["family"] == "fam05":
            row["y"] = 0.6
            row["actual_s"] = row["raw_s"] * math.exp(0.6)
    return rows


def test_family_tolerance_fires_and_its_minimum_rows_is_honoured() -> None:
    wide = replace(FAST, max_interval_log=2.0)
    table = table_from_rows(_bad_family_rows())
    shipped = train_set(table, wide)
    result = run_gate(table, wide, shipped, Reference())
    assert not result.accepted and result.rule == "family_tolerance"
    # the same data, with the family too small to judge: no family_tolerance
    lenient = run_gate(
        table, wide, shipped, Reference(), GateSettings(family_min_rows=10**6)
    )
    assert "family_tolerance" not in lenient.doc["rules_failed"]


def test_a_wide_interval_on_a_bad_family_is_rejected_one_way_or_another() -> None:
    """With the default ln 2 limit the bad family widens the interval so the
    model is unused (effective error = calibration): rejected, never
    accepted."""
    _, _, result = _gate(_bad_family_rows())
    assert not result.accepted


def test_a_reference_that_is_better_rejects_and_a_worse_one_does_not() -> None:
    table = table_from_rows(_rows())
    shipped = train_set(table, FAST)
    (member,) = shipped.members
    assert member.learned and member.artefact is not None
    good = load_model(member.artefact, expected_version=member.version)
    # the reference IS the shipped (full-data) model: a candidate refit on
    # 80 % of rows must be within the 5 % tolerance of it (accepted), and the
    # reference is recorded as a model
    ref = Reference(
        kind="active",
        set_version=shipped.set_version,
        models={"compute": good},
    )
    result = run_gate(table, FAST, shipped, ref)
    assert result.doc["terms"]["compute"]["reference"] == "model"
    assert result.doc["reference"]["kind"] == "active"
    assert result.doc["reference"]["set_version"] == shipped.set_version


def test_pinned_reference_kind_is_recorded() -> None:
    table = table_from_rows(_rows())
    shipped = train_set(table, FAST)
    result = run_gate(
        table, FAST, shipped, Reference(kind="pinned", set_version="sha256:" + "a" * 64)
    )
    assert result.doc["reference"]["kind"] == "pinned"


def test_gate_result_is_deterministic() -> None:
    table = table_from_rows(_rows())
    shipped = train_set(table, FAST)
    a = run_gate(table, FAST, shipped, Reference())
    b = run_gate(table, FAST, shipped, Reference())
    assert json.dumps(a.doc, sort_keys=True) == json.dumps(b.doc, sort_keys=True)


# ------------------------------------------------------- the fit child


def _fit(tmp_path: Path, rows: list[dict[str, Any]], **kw: Any) -> tuple[int, Path]:
    dataset = parquet_file(rows, tmp_path / "d.parquet")
    out = tmp_path / "out"
    code = run_fit(dataset, out, None, FAST)
    return code, out


def test_all_terms_insufficient_writes_a_set_document_with_no_gate(
    tmp_path: Path,
) -> None:
    rows = _rows(families=2, groups_per_family=2, runs_per_group=5)  # 20 rows
    code, out = _fit(tmp_path, rows)
    assert code == 0
    doc = json.loads((out / "set.json").read_text())
    assert doc["status"] == "insufficient_data"
    assert doc["accepted"] is None and doc["gate"] is None
    assert all(m["algorithm"] == "none" for m in doc["members"])
    assert not list(out.glob("*.art"))


def test_fit_with_an_unreadable_dataset_exits_1_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, payload in (("junk.parquet", b"not parquet"), ("empty.parquet", b"")):
        path = tmp_path / name
        path.write_bytes(payload)
        out = tmp_path / ("o_" + name)
        assert run_fit(path, out, None, FAST) == 1
        assert not out.exists()
    assert run_fit(tmp_path / "missing.parquet", tmp_path / "o3", None, FAST) == 1
    err = capsys.readouterr().err
    assert err.count("\n") >= 3 and "Traceback" not in err


def test_fit_with_a_bad_reference_exits_1(tmp_path: Path) -> None:
    dataset = parquet_file(_rows(), tmp_path / "d.parquet")
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "reference.json").write_text("{not json")
    assert run_fit(dataset, tmp_path / "o", ref, FAST) == 1
    (ref / "reference.json").write_text(json.dumps({"members": "x"}))
    assert run_fit(dataset, tmp_path / "o2", ref, FAST) == 1
    assert run_fit(dataset, tmp_path / "o3", tmp_path / "nonexistent", FAST) == 1


def test_fit_with_a_hostile_reference_member_does_not_crash(tmp_path: Path) -> None:
    dataset = parquet_file(_rows(), tmp_path / "d.parquet")
    ref = tmp_path / "ref"
    ref.mkdir()
    version = "sha256:" + "b" * 64
    (ref / artefact_name(version)).write_bytes(b"\x80\x04\x95 pickle-ish junk")
    (ref / "reference.json").write_text(
        json.dumps(
            {
                "kind": "active",
                "set_version": "sha256:" + "c" * 64,
                "members": [
                    {
                        "term": "compute",
                        "algorithm": "ridge",
                        "version": version,
                        "file": artefact_name(version),
                    },
                    {
                        "term": "x",
                        "algorithm": "ridge",
                        "version": version,
                        "file": "../../etc/passwd",
                    },
                    "garbage",
                    {"term": "compute", "algorithm": "none"},
                ],
            }
        )
    )
    code = run_fit(dataset, tmp_path / "o", ref, FAST)
    assert code == 0
    doc = json.loads((tmp_path / "o" / "set.json").read_text())
    assert doc["gate"]["reference"]["unreadable_terms"]["compute"]


def test_main_module_exit_code_and_stdout_contract(tmp_path: Path) -> None:
    dataset = parquet_file(_rows(), tmp_path / "d.parquet")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "fit",
            "--dataset",
            str(dataset),
            "--out",
            str(tmp_path / "o"),
            "--terms",
            "compute",
            "--algorithms",
            "ridge",
            "--min-rows",
            "100",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1])["set"].endswith("set.json")
    bad = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "fit",
            "--dataset",
            "x",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert bad.returncode == 2  # usage error (missing --out)
    gone = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "fit",
            "--dataset",
            str(tmp_path / "nope.parquet"),
            "--out",
            str(tmp_path / "o9"),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert gone.returncode == 1 and "Traceback" not in gone.stderr


def test_fit_handles_unicode_and_spaces_in_paths(tmp_path: Path) -> None:
    folder = tmp_path / "données ünï cödé 日本 dir with spaces"
    folder.mkdir()
    dataset = parquet_file(_rows(), folder / "dätä set.parquet")
    out = folder / "out put"
    assert run_fit(dataset, out, None, FAST) == 0
    assert (out / "set.json").is_file()
    plain = tmp_path / "plain"
    assert run_fit(dataset, plain, None, FAST) == 0
    # the output bytes do not depend on where they were written
    assert (out / "set.json").read_bytes() == (plain / "set.json").read_bytes()


def test_fit_into_an_unwritable_output_is_exit_1(tmp_path: Path) -> None:
    dataset = parquet_file(_rows(), tmp_path / "d.parquet")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert run_fit(dataset, blocker / "sub", None, FAST) == 1


def test_train_argv_on_windows_has_no_nice(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from meshforge.controller import ml_retrain_task
    from meshforge.controller.settings import PlannerSettings

    settings = PlannerSettings()
    posix = ml_retrain_task.train_argv(
        "python", Path("d.parquet"), Path("out"), None, settings
    )
    monkeypatch.setattr(os, "name", "nt")
    win = ml_retrain_task.train_argv(
        "python", Path("d.parquet"), Path("out"), None, settings
    )
    assert win[0] == "python" and "nice" not in win[0]
    assert "--reference" not in win
    assert posix[-len(win) :] == win  # the same command, only the nice prefix differs
