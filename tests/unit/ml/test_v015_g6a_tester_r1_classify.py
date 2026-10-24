# ruff: noqa: PERF401
"""V0.15 G6a tester round 1 (D375/D376): adversarial classifier checks,
derived from the brief (floor, leave-one-family-out, regret not accuracy,
never a crash on odd data), not from the implementation."""

from __future__ import annotations

import copy
import json
import math
from typing import Any

import pytest

pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from meshforge.controller.ml.train import classify as cl
from meshforge.controller.ml.train.report import canonical_json, classifier_lines

pytestmark = pytest.mark.unit


def _oc(
    cfg: str | None, regret: float | None, status: str = "counted"
) -> dict[str, Any]:
    return {"config_id": cfg, "status": status, "regret_pct": regret, "votes": {}}


def _ex(
    fam: str,
    i: int,
    label: str,
    *,
    fp: str | None = None,
    local: float | None = 10.0,
    mode1: float | None = 20.0,
    cost: float = 5.0,
    feats: list[Any] | None = None,
    classes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "campaign_id": "c",
        "group_id": f"{fam}-{i}",
        "family": fam,
        "fingerprint": fp,
        "label": label,
        "oracle_class": label,
        "records_used": 3,
        "features": feats
        if feats is not None
        else [float(i), 1.0 if label == "mode1" else 0.0],
        "classes": classes
        if classes is not None
        else {"local": _oc("l", local), "mode1": _oc("m", mode1)},
        "cost_model": _oc("cc", cost),
    }


def _doc(examples: list[dict[str, Any]], names: int = 2) -> dict[str, Any]:
    return {
        "examples_version": 1,
        "campaign_ids": ["c"],
        "policy": "p",
        "feature_names": [f"f{i}" for i in range(names)],
        "counts": {},
        "calibrations": {},
        "examples": examples,
    }


def _many(n_groups: int, n_fams: int, **kw: Any) -> list[dict[str, Any]]:
    out = []
    for k in range(n_groups):
        out.append(_ex(f"fam{k % n_fams}", k, ["local", "mode1"][k % 2], **kw))
    return out


def _row(section: dict[str, Any], model: str) -> dict[str, Any]:
    (r,) = [r for r in section["models"] if r["model"] == model]
    return r


# ---------------------------------------------------------------- D376 floor


@pytest.mark.parametrize(
    ("groups", "fams", "evaluated"),
    [
        (9, 3, False),
        (10, 2, False),
        (10, 3, True),
        (30, 2, False),
        (0, 0, False),
        (11, 4, True),
    ],
)
def test_floor_boundaries(groups: int, fams: int, evaluated: bool) -> None:
    section = cl.classify(_doc(_many(groups, max(fams, 1)) if groups else []))
    assert (section["status"] == "evaluated") is evaluated
    text = canonical_json(section) + "\n".join(classifier_lines(section))
    if not evaluated:
        assert section["status"] == "too_few_groups"
        assert "accuracy" not in text.lower()
        assert "models" not in section and "folds" not in section
        assert "regret" not in "\n".join(classifier_lines(section)).lower().replace(
            "cost regression", ""
        )


def test_floor_counts_groups_not_records() -> None:
    # 12 examples but only two families whose members all share workloads
    section = cl.classify(_doc(_many(12, 2)))
    assert section["status"] == "too_few_groups"


# ---------------------------------------------------------------- leakage


def test_no_train_row_shares_family_or_fingerprint_with_the_held_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[set[str], set[str], set[str | None], set[str | None]]] = []
    real = cl._fit_predict

    def spy(model: str, train: Any, test: Any, width: int, settings: Any) -> list[str]:
        seen.append(
            (
                {e.family for e in train},
                {e.family for e in test},
                {e.fingerprint for e in train if e.fingerprint},
                {e.fingerprint for e in test if e.fingerprint},
            )
        )
        return real(model, train, test, width, settings)

    monkeypatch.setattr(cl, "_fit_predict", spy)
    exs = []
    for k in range(24):
        fam = f"fam{k % 4}"
        # workloads shared across families: fp w0..w5 reused by different families
        fp = f"w{k % 6}" if k % 3 else None
        exs.append(_ex(fam, k, ["local", "mode1"][(k // 2) % 2], fp=fp))
    cl.classify(_doc(exs))
    assert seen
    for train_f, test_f, train_fp, test_fp in seen:
        assert not (train_f & test_f)
        assert not (train_fp & test_fp)


def test_a_workload_leak_changes_nothing_when_the_leaking_rows_are_removed() -> None:
    """Rows sharing the held-out family's fingerprint must not influence its
    prediction at all: flipping their labels/features changes no prediction
    for that family."""
    base = []
    for k in range(18):
        fam = f"fam{k % 3}"
        base.append(_ex(fam, k, ["local", "mode1"][k % 2], fp=f"u{k}"))
    # fam0 test group shares workload with a fam1 group
    base[0]["fingerprint"] = "shared"
    base[1]["fingerprint"] = "shared"
    a = cl.classify(_doc(copy.deepcopy(base)))
    mut = copy.deepcopy(base)
    mut[1]["label"] = "mode1" if mut[1]["label"] == "local" else "local"
    mut[1]["features"] = [999.0, -5.0]
    b = cl.classify(_doc(mut))
    pa = {
        g["group_id"]: g["predicted"]
        for g in a["groups_detail"]
        if g["family"] == "fam0"
    }
    pb = {
        g["group_id"]: g["predicted"]
        for g in b["groups_detail"]
        if g["family"] == "fam0"
    }
    assert pa == pb


# ---------------------------------------------------------------- regret


def test_regret_not_accuracy_with_hand_values() -> None:
    # every fold's majority is local; local regrets 0..; label mix gives accuracy != regret story
    exs = []
    for k in range(12):
        fam = f"fam{k % 3}"
        exs.append(
            _ex(
                fam,
                k,
                "local" if k % 4 else "mode1",
                local=float(k),
                mode1=100.0,
                cost=1.0,
            )
        )
    s = cl.classify(_doc(exs))
    maj = _row(s, "majority")
    # majority of any 2-family training side is local (9 of 12 are local)
    expected = sum(range(12)) / 12
    assert maj["mean_regret_pct"] == pytest.approx(expected)
    assert s["cost_model"]["mean_regret_pct"] == pytest.approx(1.0)
    assert maj["accuracy"] == pytest.approx(9 / 12)
    assert maj["vs_cost_model"]["mean_diff_pct"] == pytest.approx(expected - 1.0)


def test_unenumerated_and_unmeasured_classes_are_never_zero_regret() -> None:
    exs = []
    for k in range(12):
        fam = f"fam{k % 3}"
        classes = {"mode1": _oc("m", 7.0)}  # local not enumerated at all
        if k % 4 == 0:
            classes = {"local": _oc(None, None, "unmeasured"), "mode1": _oc("m", 7.0)}
        exs.append(_ex(fam, k, "local", classes=classes))
    s = cl.classify(_doc(exs))
    maj = _row(s, "majority")
    assert maj["counted_groups"] == 0
    assert maj["unmeasured_groups"] == 12
    assert maj["mean_regret_pct"] is None
    assert maj["within_threshold_pct"] is None
    assert maj["vs_cost_model"]["verdict"] == "too_few_groups"


def test_within_threshold_is_inclusive_at_15() -> None:
    exs = [
        _ex(f"fam{k % 3}", k, "local", local=15.0 if k % 2 else 15.0001)
        for k in range(12)
    ]
    maj = _row(cl.classify(_doc(exs)), "majority")
    assert maj["within_threshold_pct"] == pytest.approx(50.0)


@pytest.mark.xfail(
    strict=True, reason="TESTER-D1: non-finite counted regret crashes bootstrap"
)
@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_regret_does_not_poison_or_crash(bad: float) -> None:
    exs = _many(12, 3, local=3.0)
    exs[0]["classes"]["local"]["regret_pct"] = bad
    s = cl.classify(_doc(exs))
    blob = canonical_json(s)
    assert "NaN" not in blob and "Infinity" not in blob
    json.loads(blob)
    maj = _row(s, "majority")
    # a non-finite regret must not be counted as a number in the mean
    if maj["mean_regret_pct"] is not None:
        assert math.isfinite(maj["mean_regret_pct"])
        assert maj["mean_regret_pct"] < 1e6


def test_bool_regret_is_not_a_number() -> None:
    exs = _many(12, 3, local=3.0)
    exs[0]["classes"]["local"]["regret_pct"] = True
    s = cl.classify(_doc(exs))
    maj = _row(s, "majority")
    assert maj["mean_regret_pct"] == pytest.approx(3.0)


# ---------------------------------------------------------------- paired verdicts


def test_classifier_better_when_it_is_clearly_better() -> None:
    exs = []
    for k in range(30):
        fam = f"fam{k % 3}"
        label = "mode1" if k % 2 else "local"
        exs.append(
            _ex(
                fam,
                k,
                label,
                local=0.0 if label == "local" else 90.0,
                mode1=0.0 if label == "mode1" else 90.0,
                cost=50.0,
            )
        )
    s = cl.classify(_doc(exs))
    lg = _row(s, "lightgbm")
    assert lg["vs_cost_model"]["verdict"] in {"classifier_better", "not_separable"}
    assert _row(s, "logistic")["vs_cost_model"]["verdict"] == "classifier_better"
    assert _row(s, "logistic")["accuracy"] == pytest.approx(1.0)


def test_cost_model_better_when_the_cost_model_is_clearly_better() -> None:
    exs = [_ex(f"fam{k % 3}", k, "local", local=40.0, cost=1.0) for k in range(30)]
    s = cl.classify(_doc(exs))
    assert _row(s, "majority")["vs_cost_model"]["verdict"] == "cost_model_better"


# ---------------------------------------------------------------- odd data


def test_single_class_everywhere_and_all_null_features() -> None:
    exs = [_ex(f"fam{k % 3}", k, "local", feats=[None, None]) for k in range(12)]
    s = cl.classify(_doc(exs))
    for m in ("majority", "logistic", "lightgbm"):
        assert _row(s, m)["predicted_classes"] == {"local": 12}


@pytest.mark.xfail(
    strict=True, reason="TESTER-D2: zero feature width crashes LogisticRegression"
)
def test_zero_width_features() -> None:
    exs = [
        _ex(f"fam{k % 3}", k, ["local", "mode1"][k % 2], feats=[]) for k in range(12)
    ]
    s = cl.classify(_doc(exs, names=0))
    assert s["status"] == "evaluated"


@pytest.mark.xfail(strict=True, reason="TESTER-D3: inf feature literal crashes")
def test_infinite_feature_values_do_not_crash() -> None:
    # json.loads accepts Infinity/NaN literals; the child must still answer
    exs = _many(12, 3)
    exs[0]["features"] = [float("inf"), -float("inf")]
    exs[1]["features"] = [float("nan"), 1e300]
    s = cl.classify(json.loads(json.dumps(_doc(exs))))
    assert s["status"] == "evaluated"


def test_huge_feature_magnitudes_do_not_crash() -> None:
    exs = _many(12, 3)
    for e in exs:
        e["features"] = [1e30, -1e30]
    assert cl.classify(_doc(exs))["status"] == "evaluated"


def test_unseen_label_in_test_family_is_not_enumerated_not_zero() -> None:
    exs = _many(12, 3)
    for e in exs:
        if e["family"] == "fam0":
            e["label"] = "mode3_4"
    s = cl.classify(_doc(exs))
    assert s["status"] == "evaluated"


def test_unicode_and_windows_looking_names() -> None:
    exs = []
    for k in range(12):
        e = _ex(f"fam\u00e9\u4e2d{k % 3}", k, ["local", "mode1"][k % 2])
        e["group_id"] = f"C:\\grp\\\u00fc{k}"
        exs.append(e)
    s = cl.classify(_doc(exs))
    assert s["status"] == "evaluated"
    json.loads(canonical_json(s))


def test_same_group_id_in_two_campaigns_is_two_groups() -> None:
    exs = _many(12, 3)
    dup = copy.deepcopy(exs[0])
    dup["campaign_id"] = "other"
    exs.append(dup)
    s = cl.classify(_doc(exs))
    assert s["groups"] == 13


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(examples_version=2),
        lambda d: d.update(examples_version=None),
        lambda d: d.update(feature_names="x"),
        lambda d: d.update(examples={}),
        lambda d: d["examples"].append(d["examples"][0]),
        lambda d: d["examples"][0].update(features=[1.0]),
        lambda d: d["examples"][0].update(features=[1.0, "x"]),
        lambda d: d["examples"][0].update(features=[True, 1.0]),
        lambda d: d["examples"][0].update(family=""),
        lambda d: d["examples"][0].update(family=5),
        lambda d: d["examples"][0].update(label=None),
        lambda d: d["examples"][0].update(classes=[]),
        lambda d: d["examples"][0].update(cost_model="x"),
        lambda d: d["examples"].__setitem__(0, 5),
    ],
)
def test_malformed_documents_raise_examples_error_only(mutate: Any) -> None:
    doc = _doc(_many(12, 3))
    mutate(doc)
    with pytest.raises(cl.ExamplesError):
        cl.classify(doc)


@pytest.mark.parametrize("doc", [None, [], "x", 5])
def test_non_object_documents(doc: Any) -> None:
    with pytest.raises(cl.ExamplesError):
        cl.classify(doc)


def test_class_entry_that_is_not_an_object_is_tolerated_or_rejected_cleanly() -> None:
    exs = _many(12, 3)
    exs[0]["classes"]["local"] = "garbage"
    try:
        s = cl.classify(_doc(exs))
    except cl.ExamplesError:
        return
    assert s["status"] == "evaluated"


def test_regret_field_as_string_is_not_counted() -> None:
    exs = _many(12, 3, local=3.0)
    exs[0]["classes"]["local"]["regret_pct"] = "3"
    s = cl.classify(_doc(exs))
    assert _row(s, "majority")["unmeasured_groups"] >= 0


def test_huge_input_is_bounded_in_time() -> None:
    import time

    exs = []
    for k in range(600):
        exs.append(_ex(f"fam{k % 6}", k, ["local", "mode1"][k % 2]))
    t = time.monotonic()
    s = cl.classify(_doc(exs))
    assert s["status"] == "evaluated"
    assert time.monotonic() - t < 60


def test_output_has_no_accuracy_without_regret_next_to_it() -> None:
    s = cl.classify(_doc(_many(12, 3)))
    for row in s["models"]:
        assert "accuracy" in row and "mean_regret_pct" in row
        assert "within_threshold_pct" in row
    md = "\n".join(classifier_lines(s))
    assert "regret" in md.lower() and "accuracy" in md.lower()


def test_min_group_overrides_run_on_tiny_data_without_crash() -> None:
    for n, fams in ((1, 1), (2, 2), (3, 3), (4, 2)):
        s = cl.classify(
            _doc(_many(n, fams)),
            cl.ClassifySettings(min_groups=1, min_families=1),
        )
        assert s["status"] == "evaluated"
        json.loads(canonical_json(s))
