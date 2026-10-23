"""V0.15 G6a (D343, D375, D376): the 0.15.10 classifier experiment (the
``ml`` child's side).

* deterministic: the same examples give the same section, byte for byte
  (in process and through the real ``classify`` child entry, twice);
* no leak: every fold trains without its family and without any group of
  the same workload (fingerprint) - checked on what the models were
  actually given, not only on the fold's bookkeeping;
* ``too_few_groups`` below 10 groups or 3 families: no model, no accuracy
  (not in the JSON, not in the Markdown);
* the "always the most common class" row's regret equals a hand-computed
  value; a predicted class scores the regret of the planner's best
  candidate of that class (precomputed per class), never 0 for "the label";
  a class with no candidate / no measured configuration is not counted;
* the paired comparison with the cost model and its verdicts.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from meshforge.controller.ml.train import classify as cl
from meshforge.controller.ml.train.report import canonical_json, classifier_lines

pytestmark = pytest.mark.unit

FAMILIES = ("fam.a", "fam.b", "fam.c", "fam.d")
LABELS = {
    "fam.a": ("local", "local", "mode1"),
    "fam.b": ("local", "mode1", "mode1"),
    "fam.c": ("local", "local", "local"),
    "fam.d": ("mode1", "local", "local"),
}
"""Every leave-one-family-out training side has more ``local`` than
``mode1``: the majority row always predicts ``local``."""


def _outcome(
    config: str | None, regret: float | None, status: str = "counted"
) -> dict[str, Any]:
    return {
        "config_id": config,
        "status": status,
        "regret_pct": regret,
        "votes": {} if config is None else {config: 3},
    }


def _example(
    family: str,
    index: int,
    label: str,
    *,
    local_regret: float | None,
    mode1_regret: float,
    cost_regret: float,
    fingerprint: str | None = None,
    local_status: str = "counted",
) -> dict[str, Any]:
    x = 1.0 if label == "mode1" else 0.0
    return {
        "campaign_id": "camp",
        "group_id": f"{family}-{index}",
        "family": family,
        "fingerprint": fingerprint or f"fp-{family}-{index}",
        "label": label,
        "oracle_class": label,
        "records_used": 3,
        "features": [x, 0.5 * index, None],
        "classes": {
            "local": _outcome("cfg-local", local_regret, local_status),
            "mode1": _outcome("cfg-mode1", mode1_regret),
        },
        "cost_model": _outcome("cfg-cost", cost_regret),
    }


def _doc(
    examples: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if examples is None:
        examples = []
        n = 0
        for family in FAMILIES:
            for index, label in enumerate(LABELS[family]):
                examples.append(
                    _example(
                        family,
                        index,
                        label,
                        local_regret=float(n),
                        mode1_regret=50.0 + n,
                        cost_regret=2.0,
                    )
                )
                n += 1
    return {
        "examples_version": 1,
        "campaign_ids": ["camp"],
        "policy": "cost_model:calibrated:min_time",
        "feature_names": ["x", "size", "gone"],
        "counts": {"groups": len(examples), "examples": len(examples)},
        "calibrations": {},
        "examples": list(examples),
    }


def _row(section: dict[str, Any], model: str) -> dict[str, Any]:
    (row,) = [r for r in section["models"] if r["model"] == model]
    return row


def test_the_section_is_deterministic() -> None:
    first = canonical_json(cl.classify(_doc()))
    assert first == canonical_json(cl.classify(_doc()))
    # the input order does not matter (examples are sorted by group)
    shuffled = _doc()
    shuffled["examples"] = list(reversed(shuffled["examples"]))
    assert canonical_json(cl.classify(shuffled)) == first


def test_the_child_entry_writes_the_same_bytes_twice(tmp_path: Path) -> None:
    examples = tmp_path / "examples.json"
    examples.write_text(json.dumps(_doc()), encoding="utf-8")
    texts = []
    for name in ("a", "b"):
        done = subprocess.run(
            [
                sys.executable,
                "-m",
                "meshforge.controller.ml.train",
                "classify",
                "--examples",
                str(examples),
                "--out",
                str(tmp_path / name),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert done.returncode == 0, done.stderr
        texts.append((tmp_path / name / "classify.json").read_bytes())
        md = (tmp_path / name / "classify.md").read_text(encoding="utf-8")
        assert "## Classifier vs cost regression" in md
    assert texts[0] == texts[1]
    doc = json.loads(texts[0])
    assert doc["classifier"]["status"] == "evaluated"


def test_the_child_refuses_a_bad_document(tmp_path: Path) -> None:
    examples = tmp_path / "examples.json"
    examples.write_text(json.dumps({"examples_version": 9}), encoding="utf-8")
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "classify",
            "--examples",
            str(examples),
            "--out",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 2, done.stderr
    assert "unknown examples_version 9" in done.stderr


def test_no_fold_sees_its_family_or_its_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    examples = _doc()["examples"]
    # a group of fam.b runs the same workload as one of fam.a
    examples[3]["fingerprint"] = examples[0]["fingerprint"]
    # family-less fingerprints: only the family rule keeps these out
    examples[1]["fingerprint"] = None
    examples[7]["fingerprint"] = None
    seen: list[tuple[set[str], set[str | None], set[str]]] = []
    real = cl._fit_predict

    def spy(
        model: str,
        train: Sequence[cl.Example],
        test: Sequence[cl.Example],
        width: int,
        settings: cl.ClassifySettings,
    ) -> list[str]:
        seen.append(
            (
                {e.family for e in train},
                {e.fingerprint for e in train},
                {e.family for e in test},
            )
        )
        test_prints = {e.fingerprint for e in test if e.fingerprint is not None}
        assert not test_prints & {e.fingerprint for e in train}
        return real(model, train, test, width, settings)

    monkeypatch.setattr(cl, "_fit_predict", spy)
    section = cl.classify(_doc(examples))
    assert len(seen) == len(FAMILIES) * len(cl.MODELS)
    for train_families, _prints, test_families in seen:
        (held,) = test_families
        assert held not in train_families
    for fold in section["folds"]:
        assert fold["family"] not in fold["train_families"]
    by_family = {f["family"]: f for f in section["folds"]}
    # the shared workload is dropped from the other family's training side
    assert by_family["fam.a"]["excluded_same_workload"] == 1
    assert by_family["fam.b"]["excluded_same_workload"] == 1
    assert by_family["fam.c"]["excluded_same_workload"] == 0


@pytest.mark.parametrize(
    ("groups", "families"),
    [(9, 4), (12, 2)],
    ids=["9-groups", "2-families"],
)
def test_too_few_groups_prints_no_accuracy(groups: int, families: int) -> None:
    examples = [
        _example(
            FAMILIES[i % families],
            i,
            "local",
            local_regret=0.0,
            mode1_regret=1.0,
            cost_regret=0.0,
        )
        for i in range(groups)
    ]
    section = cl.classify(_doc(examples))
    assert section["status"] == "too_few_groups"
    assert f"{groups} labelled group(s) in {families} family" in section["reason"]
    text = canonical_json(section)
    assert "accuracy" not in text and "models" not in section
    md = "\n".join(classifier_lines(section))
    assert "accuracy" not in md and "too_few_groups" in md


def test_the_majority_row_regret_is_hand_computed() -> None:
    examples = _doc()["examples"]
    # one group's `local` class has no measured configuration
    examples[5]["classes"]["local"] = _outcome("cfg-local", None, "unmeasured")
    section = cl.classify(_doc(examples))
    majority = _row(section, "majority")
    assert majority["predicted_classes"] == {"local": 12}
    # local regrets are 0..11; group 5 is not counted
    want = (sum(range(12)) - 5) / 11
    assert majority["counted_groups"] == 11
    assert majority["unmeasured_groups"] == 1
    assert majority["mean_regret_pct"] == pytest.approx(want, abs=1e-5)
    # 8 of the 12 labels are `local`
    assert majority["accuracy"] == pytest.approx(8 / 12, abs=1e-5)
    # paired with the cost model (regret 2.0 everywhere) on the 11 groups
    paired = majority["vs_cost_model"]
    assert paired["common_groups"] == 11
    assert paired["mean_diff_pct"] == pytest.approx(want - 2.0, abs=1e-5)
    assert paired["verdict"] == "cost_model_better"  # diffs -2..9, mean 3.5
    assert section["cost_model"]["mean_regret_pct"] == pytest.approx(2.0)


def test_a_correct_class_scores_the_planners_candidate_not_zero() -> None:
    examples = _doc()["examples"]
    for e in examples:
        # every group's label is right for the majority (local) ...
        e["label"] = "local"
        # ... but the planner's best `local` candidate is 12.5 % off
        e["classes"]["local"] = _outcome("cfg-local", 12.5)
    section = cl.classify(_doc(examples))
    majority = _row(section, "majority")
    assert majority["accuracy"] == pytest.approx(1.0)
    assert majority["mean_regret_pct"] == pytest.approx(12.5)
    assert majority["counted_groups"] == 12


def test_a_class_the_planner_did_not_enumerate_is_not_counted() -> None:
    example = cl.parse_examples(_doc())[1][0]
    found = cl.outcome_of(example, "mode3_3")
    assert found["status"] == "not_enumerated" and found["regret_pct"] is None
    assert cl.outcome_of(example, "local")["status"] == "counted"


def test_the_models_learn_a_separable_rule() -> None:
    section = cl.classify(_doc())
    for model in ("logistic", "lightgbm"):
        row = _row(section, model)
        assert row["predicted_groups"] == 12
        assert row["accuracy"] is not None
    # x = 1.0 exactly for `mode1`: logistic regression gets it right
    assert _row(section, "logistic")["accuracy"] == pytest.approx(1.0)


def test_paired_verdicts() -> None:
    settings = cl.ClassifySettings()
    keys = [("c", f"g{i}") for i in range(12)]
    better = {k: {"status": "counted", "regret_pct": 0.0} for k in keys}
    worse = {k: {"status": "counted", "regret_pct": 30.0} for k in keys}
    assert cl._paired(better, worse, settings)["verdict"] == "classifier_better"
    assert cl._paired(worse, better, settings)["verdict"] == "cost_model_better"
    few = dict(list(better.items())[:9])
    assert cl._paired(few, worse, settings)["verdict"] == "too_few_groups"


@pytest.mark.parametrize(
    "broken",
    [
        {"examples_version": 2},
        {"examples_version": 1, "feature_names": ["a"], "examples": "x"},
        {
            "examples_version": 1,
            "feature_names": ["a"],
            "examples": [{"features": [1.0, 2.0]}],
        },
    ],
)
def test_parse_refuses_malformed_documents(broken: dict[str, Any]) -> None:
    with pytest.raises(cl.ExamplesError):
        cl.parse_examples(broken)


def test_a_group_listed_twice_is_refused() -> None:
    doc = _doc()
    doc["examples"].append(doc["examples"][0])
    with pytest.raises(cl.ExamplesError, match="twice"):
        cl.parse_examples(doc)


def test_the_markdown_section() -> None:
    lines = classifier_lines(cl.classify(_doc()))
    assert lines[0] == "## Classifier vs cost regression (0.15.10, report only)"
    table = [line for line in lines if line.startswith("| ")]
    assert [row.split(" | ")[0].strip("| ") for row in table[1:]] == [
        "majority",
        "logistic",
        "lightgbm",
        "cost_model",
    ]


def test_the_compare_report_gets_the_section(tmp_path: Path) -> None:
    from mf_ml_synthetic import parquet_file, synthetic_rows

    from meshforge.controller.ml.train import __main__ as train_main

    rows = synthetic_rows("signal", families=3, groups_per_family=2, runs_per_group=4)
    dataset = parquet_file(rows, tmp_path / "d.parquet")
    examples = tmp_path / "examples.json"
    examples.write_text(json.dumps(_doc()), encoding="utf-8")
    out = tmp_path / "o"
    base = ["compare", "--dataset", str(dataset), "--out", str(out)]
    fast = ["--min-rows", "10", "--algorithms", "ridge", "--terms", "compute"]
    assert train_main.main([*base, *fast, "--classify-examples", str(examples)]) == 0
    doc = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert doc["classifier"] == json.loads(canonical_json(cl.classify(_doc())))
    md = (out / "report.md").read_text(encoding="utf-8")
    assert "## Classifier vs cost regression" in md
    # an unreadable examples file is a usage error, nothing written
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    other = tmp_path / "o2"
    argv = ["compare", "--dataset", str(dataset), "--out", str(other), *fast]
    code = train_main.main([*argv, "--classify-examples", str(bad)])
    assert code == 2
    assert not other.exists()
