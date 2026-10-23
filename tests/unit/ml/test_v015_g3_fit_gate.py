"""V0.15 G3 (D339, D347, D358, D360): the shipped set and its activation gate.

* :func:`train_set` - learned members carry loadable artefacts whose
  version is their digest; a term below the D348 floor or whose selection
  kept a baseline is a ``none`` member with its reason; ``set_version`` is
  the D358 digest; the gate's ``fixed`` refit reuses the shipped config.
* The gate's held-out slice is the calibration's D329 split (per
  fingerprint, every row of a decision on one side).
* **Gate rules** on synthetic truths: signal -> accepted against the
  calibration; pure noise -> ``not_better``; one family the model gets wrong
  -> ``family_tolerance``; a much better reference (active set) ->
  ``worse_than_reference``; a reference artefact that cannot be read ->
  judged against the calibration and recorded; ``regret: pending_g4``.
* **Determinism (criterion 3)** - ``python -m meshforge.controller.ml.train
  fit`` in fresh processes with different ``PYTHONHASHSEED``, on the same
  dataset file and on a row-shuffled copy, writes byte-identical
  ``set.json`` and artefacts (same set version).
"""

from __future__ import annotations

import json
import math
import os
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

from meshforge.controller.ml.artefact import digest
from meshforge.controller.ml.serve import load_model
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import Table, table_from_rows
from meshforge.controller.ml.train.fit import set_version, train_set
from meshforge.controller.ml.train.fit_main import artefact_name, read_reference
from meshforge.controller.ml.train.gate import Reference, holdout_mask, run_gate

pytestmark = pytest.mark.unit

FAST = CompareSettings(
    terms=("compute",), algorithms=("ridge", "lightgbm"), min_rows=100
)


def _rows(kind: str = "signal", **kw: Any) -> list[dict[str, Any]]:
    args: dict[str, Any] = {
        "families": 6,
        "groups_per_family": 5,
        "runs_per_group": 10,
    }
    args.update(kw)
    return synthetic_rows(kind, **args)


def _set_y(row: dict[str, Any], y: float) -> None:
    row["y"] = y
    row["actual_s"] = row["raw_s"] * math.exp(y)


def _pure_noise() -> list[dict[str, Any]]:
    rng = random.Random(5)  # noqa: S311 - reproducible test data
    rows = _rows()
    for row in rows:
        _set_y(row, rng.gauss(0.0, 0.3))
    return rows


def _bad_family() -> list[dict[str, Any]]:
    """Signal everywhere but ``fam05``, where the residual is a constant the
    model (which cannot see the family) gets wrong and the calibration's
    node + family factor gets right."""
    rows = _rows()
    for row in rows:
        if row["family"] == "fam05":
            _set_y(row, 0.6)
    return rows


@pytest.fixture(scope="module")
def signal() -> Table:
    return table_from_rows(_rows())


def test_train_set_members_artefacts_and_version(signal: Table) -> None:
    settings = replace(FAST, terms=("compute", "env"))
    result = train_set(signal, settings)
    compute, env = result.member("compute"), result.member("env")
    assert compute is not None and env is not None
    assert compute.term == "compute" and compute.learned
    assert compute.artefact is not None
    assert compute.version == digest(compute.artefact)
    loaded = load_model(compute.artefact, expected_version=compute.version)
    assert loaded.algorithm == compute.algorithm
    assert compute.metrics["rows"] == 300 and compute.metrics["inner_k"] == 5
    # no env rows at all: below the floor, a "none" member with its reason
    assert not env.learned and env.artefact is None
    assert env.reason is not None and env.reason.startswith("insufficient_data")
    assert result.set_version == set_version(result.members, result.feature_version)
    assert result.set_version.startswith("sha256:")


def test_a_baseline_pick_is_a_none_member_with_its_reason() -> None:
    table = table_from_rows(_pure_noise())
    (member,) = train_set(table, FAST).members
    assert member.algorithm == "none"
    assert member.reason is not None and member.reason.startswith("no_learned_model")


def test_the_fixed_refit_reuses_the_shipped_configuration(signal: Table) -> None:
    shipped = train_set(signal, FAST)
    (member,) = shipped.members
    assert member.config is not None
    held, _ = holdout_mask(signal)
    refit = train_set(signal, FAST, allowed=~held, fixed={"compute": member.config})
    (again,) = refit.members
    assert again.algorithm == member.algorithm and again.params == member.params
    assert again.metrics["rows"] < member.metrics["rows"]


def test_the_held_out_slice_is_the_d329_split(signal: Table) -> None:
    held, info = holdout_mask(signal)
    assert info["rows_held"] == int(held.sum()) > 0
    # per fingerprint the newest fifth (10 runs per group -> 2 held)
    by_group: dict[str, list[int]] = {}
    for i in range(signal.n):
        by_group.setdefault(signal.group[i], []).append(i)
    for rows in by_group.values():
        flags = [
            bool(held[i]) for i in sorted(rows, key=signal.finished_at.__getitem__)
        ]
        assert flags == [False] * 8 + [True] * 2


def test_signal_is_accepted_against_the_calibration(signal: Table) -> None:
    shipped = train_set(signal, FAST)
    gate = run_gate(signal, FAST, shipped, Reference())
    assert gate.accepted, gate.reason
    term = gate.doc["terms"]["compute"]
    assert term["male_new"] < term["male_calibration"] - 0.02
    assert term["reference"] == "calibration"
    assert gate.doc["regret"] == "pending_g4"
    assert gate.doc["reference"]["kind"] == "calibration"
    assert gate.doc["better_terms"] == ["compute"]


def test_pure_noise_is_rejected_not_better() -> None:
    table = table_from_rows(_pure_noise())
    shipped = train_set(table, FAST)
    gate = run_gate(table, FAST, shipped, Reference())
    assert not gate.accepted
    assert gate.rule == "not_better"
    assert gate.reason is not None and gate.reason.startswith("not_better: ")


def test_one_bad_family_is_rejected_family_tolerance() -> None:
    table = table_from_rows(_bad_family())
    # a wide interval limit, so the model is used on every row (the default
    # ln 2 would keep the calibration on the bad family's rows)
    settings = replace(FAST, max_interval_log=2.0)
    shipped = train_set(table, settings)
    gate = run_gate(table, settings, shipped, Reference())
    assert not gate.accepted
    assert gate.rule == "family_tolerance"
    assert "fam05" in (gate.reason or "")
    fam = gate.doc["terms"]["compute"]["families"]["fam05"]
    assert fam["male_new"] - fam["male_calibration"] > 0.05


def _reference(table: Table, algorithms: tuple[str, ...]) -> Reference:
    ref = train_set(table, replace(FAST, algorithms=algorithms))
    (member,) = ref.members
    assert member.artefact is not None
    model = load_model(member.artefact, expected_version=member.version)
    return Reference(
        kind="active", set_version=ref.set_version, models={"compute": model}
    )


def test_a_new_set_worse_than_the_active_one_is_rejected(signal: Table) -> None:
    reference = _reference(signal, ("lightgbm",))
    coarse = replace(FAST, algorithms=("tree",), max_interval_log=2.0)
    shipped = train_set(signal, coarse)
    (member,) = shipped.members
    assert member.algorithm == "tree"
    gate = run_gate(signal, coarse, shipped, reference)
    term = gate.doc["terms"]["compute"]
    assert term["reference"] == "model"
    assert term["male_new"] > term["male_reference"] * 1.05
    assert gate.rule == "worse_than_reference", gate.reason
    assert gate.doc["reference"] == {
        "kind": "active",
        "set_version": reference.set_version,
        "unreadable_terms": {},
    }
    # the same set judged against the calibration is accepted: the
    # reference, not the set, decided
    assert run_gate(signal, coarse, shipped, Reference()).accepted


def test_an_unreadable_reference_member_falls_back_to_calibration(
    signal: Table, tmp_path: Path
) -> None:
    ref = train_set(signal, FAST)
    (member,) = ref.members
    assert member.artefact is not None
    (tmp_path / "reference.json").write_text(
        json.dumps(
            {
                "kind": "active",
                "set_version": ref.set_version,
                "members": [
                    {
                        "term": "compute",
                        "algorithm": member.algorithm,
                        "version": member.version,
                        "file": artefact_name(member.version),
                    }
                ],
            }
        )
    )
    bad = bytearray(member.artefact)
    bad[100] ^= 0xFF
    (tmp_path / artefact_name(member.version)).write_bytes(bytes(bad))
    reference = read_reference(tmp_path)
    assert reference.models == {}
    assert reference.unreadable == {"compute": "digest_mismatch"}
    gate = run_gate(signal, FAST, train_set(signal, FAST), reference)
    assert gate.doc["terms"]["compute"]["reference"] == "calibration"
    assert gate.doc["terms"]["compute"]["reference_unreadable"] == "digest_mismatch"
    (tmp_path / artefact_name(member.version)).unlink()
    assert read_reference(tmp_path).unreadable == {"compute": "artefact_missing"}


def _fit_child(dataset: Path, out: Path, seed: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "fit",
            "--dataset",
            str(dataset),
            "--out",
            str(out),
            "--terms",
            "compute",
            "--algorithms",
            "ridge,forest,lightgbm,xgboost",
            "--min-rows",
            "100",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=600,
    )


def _files(directory: Path) -> dict[str, bytes]:
    return {
        p.name: p.read_bytes()
        for p in sorted(directory.iterdir())
        if p.name != "timings.json"
    }


@pytest.mark.timeout(900)
def test_fit_in_fresh_processes_is_byte_identical(tmp_path: Path) -> None:
    rows = _rows()
    first = parquet_file(rows, tmp_path / "d.parquet")
    shuffled = parquet_file(rows, tmp_path / "s.parquet", shuffle_seed=3)
    outputs = []
    for name, dataset, seed in (
        ("a", first, "0"),
        ("b", first, "4242"),
        ("c", shuffled, "17"),
    ):
        out = tmp_path / name
        result = _fit_child(dataset, out, seed)
        assert result.returncode == 0, result.stderr[-2000:]
        outputs.append(_files(out))
    assert outputs[0] == outputs[1] == outputs[2]
    doc = json.loads(outputs[0]["set.json"])
    assert doc["status"] == "trained" and doc["accepted"] is True
    (member,) = doc["members"]
    art = outputs[0][member["artefact"]]
    assert digest(art) == member["version"]
    loaded = load_model(art, expected_version=member["version"])
    assert np.isfinite(loaded.q)
