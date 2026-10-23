"""V0.15 G5a (D368): the evaluator's fold models.

* :func:`split_rows` - no row of the held-out family, none of its workload
  groups (a no-family row of the same fingerprint too), none of the extra
  groups, none after ``until``; the family's rows are the held-out rows;
* **leak test** - ``fit --exclude-family F`` writes a ``fold`` block whose
  ``trained_families`` never contains ``F``; a doctored fold that trained on
  ``F`` is refused by the controller (:func:`read_fold`);
* **production pipeline** - the fold's set version equals a direct
  :func:`train_set` on the same rows (the G3 retrain's function);
* through the serving path: the controller loads the fold's artefacts with
  :func:`load_model` into an ``MlInput`` that predicts like the child did;
* :func:`train_fold` failure paths (timeout, child failure, unreadable
  output) give no model and say why; :func:`pool_terms` metrics;
* :func:`fold_argv` = the retrain's training settings + the fold flags.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.controller.ml import folds
from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.child import ChildOutputError, read_output
from meshforge.controller.ml.train import __main__ as train_main
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fold import HELDOUT_FILE, split_rows
from meshforge.controller.ml_retrain_task import train_argv
from meshforge.controller.planner.predictor import TermQuery
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

UNTIL = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)
FAST_ARGS = ["--terms", "compute", "--algorithms", "ridge", "--min-rows", "60"]
FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=60)


def _rows() -> list[dict[str, Any]]:
    return synthetic_rows("signal", families=5, groups_per_family=3, runs_per_group=6)


def _fold(tmp_path: Path, rows: list[dict[str, Any]], *extra: str) -> Path:
    dataset = parquet_file(rows, tmp_path / "dataset.parquet")
    out = tmp_path / "out"
    code = train_main.main(
        [
            "fit",
            "--dataset",
            str(dataset),
            "--out",
            str(out),
            *FAST_ARGS,
            "--exclude-family",
            "fam00",
            "--until",
            UNTIL.isoformat(),
            *extra,
        ]
    )
    assert code == 0
    return out


# ----------------------------------------------------------- the split


def test_split_rows_leaves_out_the_family_its_groups_and_late_rows() -> None:
    rows = _rows()
    sneaky = dict(rows[0])  # a user job of fam00's workload: no family
    sneaky.update(family=None, decision_id=99_999, attempt_id="att_sneaky")
    assert rows[0]["family"] == "fam00"
    late = dict(rows[-1])
    late.update(finished_at=UNTIL + dt.timedelta(seconds=1), decision_id=99_998)
    extra_group = rows[-1]["group"]
    split = split_rows(
        [*rows, sneaky, late], "fam00", until=UNTIL, groups=[extra_group]
    )
    assert {r["family"] for r in split.train} == {"fam01", "fam02", "fam03", "fam04"}
    assert sneaky not in split.train and late not in split.train
    assert all(r["group"] != extra_group for r in split.train)
    assert {r["family"] for r in split.held} == {"fam00"}
    assert len(split.held) == 18 and split.late_rows == 1
    assert split.excluded_rows == len(rows) + 1 - len(split.train)
    assert split.excluded_groups == 4  # fam00's 3 groups + the extra one


def test_a_naive_until_is_read_as_utc() -> None:
    rows = _rows()
    naive = UNTIL.replace(tzinfo=None)
    assert len(split_rows(rows, "fam00", until=naive).train) == len(
        split_rows(rows, "fam00", until=UNTIL).train
    )


# ------------------------------------------------- the child (leak test)


def test_the_fold_never_trains_on_its_family_and_is_the_production_set(
    tmp_path: Path,
) -> None:
    rows = _rows()
    out = _fold(tmp_path, rows)
    doc, artefacts, _ = read_output(out)
    fold = doc["fold"]
    assert fold["family"] == "fam00"
    assert "fam00" not in fold["trained_families"]
    assert fold["trained_families"] == ["fam01", "fam02", "fam03", "fam04"]
    assert fold["train_rows"] == 72 and fold["heldout_rows"] == 18
    assert fold["until"] == UNTIL.isoformat()
    assert doc["accepted"] is None and doc["gate"] is None  # never gated
    # the production pipeline: the same set version as train_set on those rows
    direct = train_set(
        table_from_rows([r for r in rows if r["family"] != "fam00"]), FAST
    )
    assert doc["set_version"] == direct.set_version
    assert set(artefacts) == {m.version for m in direct.learned}
    held = json.loads((out / HELDOUT_FILE).read_text(encoding="utf-8"))
    assert set(held) == {"compute"} and len(held["compute"]["y"]) == 18


def test_the_controller_loads_the_fold_through_the_serving_path(
    tmp_path: Path,
) -> None:
    out = _fold(tmp_path, _rows())
    outcome = folds.read_fold(out, "fam00", UNTIL, math.log(2.0))
    assert outcome.ml is not None
    assert outcome.ml.version == outcome.entry["set_version"]
    assert outcome.entry["source"] == "fold" and outcome.entry["reason"] is None
    assert outcome.entry["learned_terms"] == ["compute"]
    # the loaded model answers the held-out rows exactly as the child did
    held = outcome.heldout["compute"]
    rows = [r for r in _rows() if r["family"] == "fam00"]
    table = table_from_rows(rows)
    answers = outcome.ml.predictor.predict(
        [TermQuery("compute", "n", tuple(x)) for x in table.X.tolist()]
    )
    assert [a.y_hat for a in answers] == pytest.approx(held["y_hat"], rel=1e-12)


def test_a_fold_that_trained_on_its_family_is_refused(tmp_path: Path) -> None:
    """Mutation guard (fold trained with F included): the controller never
    evaluates a family on a set whose fold block names it."""
    out = _fold(tmp_path, _rows())
    path = out / "set.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["fold"]["trained_families"].append("fam00")
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ChildOutputError, match="trained on its held-out family"):
        folds.read_fold(out, "fam00", UNTIL, math.log(2.0))


def test_a_fold_without_enough_rows_has_no_model(tmp_path: Path) -> None:
    rows = synthetic_rows("signal", families=3, groups_per_family=2, runs_per_group=4)
    out = _fold(tmp_path, rows)
    outcome = folds.read_fold(out, "fam00", UNTIL, math.log(2.0))
    assert outcome.ml is None and outcome.entry["reason"] == "insufficient_data"
    assert outcome.heldout == {}


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--until", "2026-12-01T00:00:00"], "time zone"),
        (["--reference", "x"], "never gated"),
        (["--exclude-groups", "missing.json"], "missing.json"),
    ],
)
def test_fold_arguments_are_checked(
    tmp_path: Path,
    argv: list[str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dataset = parquet_file(_rows(), tmp_path / "d.parquet")
    code = train_main.main(
        [
            "fit",
            "--dataset",
            str(dataset),
            "--out",
            str(tmp_path / "o"),
            "--exclude-family",
            "fam00",
            *[str(tmp_path / a) if a.endswith(".json") else a for a in argv],
        ]
    )
    assert code == 2 and message in capsys.readouterr().err


def test_until_without_a_fold_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dataset = parquet_file(_rows(), tmp_path / "d.parquet")
    code = train_main.main(
        [
            "fit",
            "--dataset",
            str(dataset),
            "--out",
            str(tmp_path / "o"),
            "--until",
            UNTIL.isoformat(),
        ]
    )
    assert code == 2 and "need --exclude-family" in capsys.readouterr().err


def test_exclude_groups_file_is_read(tmp_path: Path) -> None:
    rows = _rows()
    groups = tmp_path / "groups.json"
    groups.write_text(json.dumps([rows[-1]["group"]]), encoding="utf-8")
    out = _fold(tmp_path, rows, "--exclude-groups", str(groups))
    doc, _, _ = read_output(out)
    assert doc["fold"]["train_rows"] == 72 - 6
    assert doc["fold"]["excluded_groups"] == 4


# --------------------------------------------------- controller side


def test_fold_argv_uses_the_retrains_training_settings(tmp_path: Path) -> None:
    settings = PlannerSettings()
    argv = folds.fold_argv(
        "py", tmp_path / "d", tmp_path / "o", "fam00", UNTIL, None, settings
    )
    retrain = train_argv("py", tmp_path / "d", tmp_path / "o", None, settings)
    shared = folds.train_settings_argv(settings)
    assert retrain[-len(shared) :] == shared
    i = argv.index("--min-rows")
    assert argv[i : i + len(shared)] == shared
    assert argv[-4:] == ["--exclude-family", "fam00", "--until", UNTIL.isoformat()]
    with_groups = folds.fold_argv(
        "py", tmp_path / "d", tmp_path / "o", "f", UNTIL, tmp_path / "g", settings
    )
    assert with_groups[-2:] == ["--exclude-groups", str(tmp_path / "g")]


def _runner(code: int = 0, stderr: str = "", exc: BaseException | None = None) -> Any:
    seen: list[Sequence[str]] = []

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        seen.append(list(argv))
        if exc is not None:
            raise exc
        return code, stderr

    child.seen = seen  # type: ignore[attr-defined]
    return child


@pytest.mark.parametrize(
    ("child", "reason"),
    [
        (_runner(exc=TimeoutError()), "timeout: killed after"),
        (_runner(code=1, stderr="x\nboom"), "child_failed: exit 1: boom"),
        (_runner(code=0), "child_output_invalid"),
    ],
)
def test_a_failed_fold_has_no_model_and_says_why(
    tmp_path: Path, child: Any, reason: str
) -> None:
    outcome = asyncio.run(
        folds.train_fold(
            python="py",
            dataset=tmp_path / "d",
            work=tmp_path,
            family="fam00",
            fingerprints={"fp2", "fp1"},
            until=UNTIL,
            settings=PlannerSettings(),
            child=child,
        )
    )
    assert outcome.ml is None and outcome.entry["source"] == "none"
    assert outcome.entry["reason"].startswith(reason)
    assert json.loads((tmp_path / "groups.json").read_text()) == ["fp1", "fp2"]
    assert "--exclude-groups" in child.seen[0]


def test_train_fold_end_to_end_with_the_real_child_in_process(tmp_path: Path) -> None:
    dataset = parquet_file(_rows(), tmp_path / "d.parquet")

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        args = list(argv[argv.index("fit") :])
        return train_main.main([*args, *FAST_ARGS]), ""

    work = tmp_path / "w"
    work.mkdir()
    outcome = asyncio.run(
        folds.train_fold(
            python="py",
            dataset=dataset,
            work=work,
            family="fam00",
            fingerprints=(),
            until=UNTIL,
            settings=PlannerSettings(),
            child=child,
        )
    )
    assert outcome.ml is not None and outcome.entry["train_rows"] == 72
    metrics = folds.pool_terms([outcome], max_interval_log=math.log(2.0))
    compute = metrics["compute"]
    assert compute["rows"] == 18 and compute["families"] == 1
    assert compute["male_model"] < compute["male_raw"]  # the signal is learned
    assert 0.0 <= compute["coverage"] <= 1.0


def test_loaded_set_needs_every_learned_artefact() -> None:
    doc = {
        "set_version": "sha256:" + "aa" * 32,
        "feature_version": 2,
        "members": [
            {"term": "env", "algorithm": "none", "reason": "insufficient_data: x"},
            {"term": "compute", "algorithm": "ridge", "version": "sha256:" + "bb" * 32},
        ],
    }
    with pytest.raises(ArtefactError):
        folds.loaded_set(doc, {})
    only_none = {**doc, "members": doc["members"][:1]}
    loaded = folds.loaded_set(only_none, {})
    assert loaded.without_model == {"env": "insufficient_data: x"}
    assert folds.planner_input(loaded, math.log(2.0)) is None


def test_pool_terms_metrics_by_hand() -> None:
    block = {
        "y": [0.0, 1.0, 0.5, None],
        "raw_s": [10.0, 10.0, 10.0, 10.0],
        "actual_s": [10.0, 10.0 * math.e, 10.0 * math.exp(0.5), 1.0],
        "y_hat": [0.1, 0.8, 0.5, 0.0],
        "q": [0.2, 0.1, None, 0.1],
        "ood": [False, True, False, False],
    }
    one = folds.FoldOutcome("famA", None, {}, {"compute": block, "bad": {"y": 1}})
    two = folds.FoldOutcome("famB", None, {}, {"compute": dict(block)})
    out = folds.pool_terms([one, two], max_interval_log=0.5)
    assert set(out) == {"compute"}  # a malformed block is skipped
    compute = out["compute"]
    assert compute["rows"] == 6 and compute["families"] == 2  # None rows skipped
    assert compute["male_model"] == pytest.approx((0.1 + 0.2 + 0.0) / 3)
    assert compute["male_raw"] == pytest.approx(0.5)
    # |0 - 0.1| <= 0.2 yes; |1 - 0.8| > 0.1 no; q missing = no interval (inf) yes
    assert compute["coverage"] == pytest.approx(2 / 3)
    assert compute["ood_rate"] == pytest.approx(1 / 3)
    assert compute["usable_rate"] == pytest.approx(1 / 3)  # q=0.2: 0.4<=0.5
    assert folds.HELDOUT_FILE == HELDOUT_FILE


def test_an_ml_input_of_a_fold_has_no_time_budget(tmp_path: Path) -> None:
    out = _fold(tmp_path, _rows())
    outcome = folds.read_fold(out, "fam00", UNTIL, math.log(2.0))
    assert outcome.ml is not None
    assert type(outcome.ml.predictor).__name__ == "SetPredictor"
    assert replace(outcome.ml).max_interval_log == math.log(2.0)
