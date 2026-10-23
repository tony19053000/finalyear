"""V0.15 G5a tester: adversarial fold-model tests (D368), derived from the brief.

Empty / single-family / tiny / NaN datasets, hostile fold-child outputs,
fold-vs-direct-train equality with an extra exclusion, no-family rows and
group-less rows, and the controller never raising for a failed fold.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.controller.ml import folds
from meshforge.controller.ml.child import ChildOutputError, read_output
from meshforge.controller.ml.train import __main__ as train_main
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fold import HELDOUT_FILE, split_rows
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

UNTIL = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)
FAST_ARGS = ["--terms", "compute", "--algorithms", "ridge", "--min-rows", "60"]
FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=60)
LOG2 = math.log(2.0)


def _rows(**kw: Any) -> list[dict[str, Any]]:
    kw.setdefault("families", 5)
    kw.setdefault("groups_per_family", 3)
    kw.setdefault("runs_per_group", 6)
    return synthetic_rows("signal", **kw)


def _fit(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    family: str = "fam00",
    *extra: str,
) -> tuple[int, Path]:
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
            family,
            "--until",
            UNTIL.isoformat(),
            *extra,
        ]
    )
    return code, out


# ------------------------------------------------------------ split edge cases


def test_split_empty_input_is_empty_not_an_error() -> None:
    split = split_rows([], "fam00", until=UNTIL)
    assert split.train == [] and split.held == []
    assert split.excluded_rows == 0 and split.late_rows == 0


def test_split_unknown_family_holds_nothing_and_trains_on_all() -> None:
    rows = _rows()
    split = split_rows(rows, "nope", until=UNTIL)
    assert split.held == [] and len(split.train) == len(rows)


def test_split_rows_without_family_are_kept_unless_their_group_is_excluded() -> None:
    rows = _rows()
    famless = dict(rows[-1], family=None, group="unrelated", decision_id=77_777)
    split = split_rows([*rows, famless], "fam00", until=UNTIL)
    assert famless in split.train


def test_split_no_until_keeps_every_row() -> None:
    rows = _rows()
    late = dict(rows[0], finished_at=dt.datetime(2099, 1, 1, tzinfo=dt.UTC))
    split = split_rows([*rows, late], "fam01", until=None)
    assert split.late_rows == 0
    assert late in split.train


def test_split_rows_on_exactly_the_cutoff_are_kept() -> None:
    rows = _rows()
    edge = dict(rows[-1], finished_at=UNTIL, family="fam04", decision_id=1)
    split = split_rows([edge], "fam00", until=UNTIL)
    assert split.train == [edge] and split.late_rows == 0


# ---------------------------------------------------------------- child edge cases


def test_fold_of_a_dataset_with_a_single_family_does_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = _rows(families=1)
    code, out = _fit(tmp_path, rows)
    err = capsys.readouterr().err
    assert "Traceback" not in err
    if code == 0:
        doc, _arts, _ = read_output(out)
        assert doc["fold"]["train_rows"] == 0 and doc["fold"]["trained_families"] == []
        outcome = folds.read_fold(out, "fam00", UNTIL, LOG2)
        assert outcome.ml is None
        assert outcome.entry["reason"] in {"insufficient_data", "no_learned_model"}
        assert outcome.heldout == {}
    else:
        assert err.strip(), "a failing fold child must say why on stderr"


def test_fold_of_an_unknown_family_trains_on_everything_and_holds_nothing(
    tmp_path: Path,
) -> None:
    code, out = _fit(tmp_path, _rows(), "ghost")
    assert code == 0
    doc, _a, _t = read_output(out)
    assert doc["fold"]["heldout_rows"] == 0
    assert json.loads((out / HELDOUT_FILE).read_text()) == {}


def test_fold_of_tiny_families_is_insufficient_data_not_a_crash(tmp_path: Path) -> None:
    rows = _rows(families=3, groups_per_family=1, runs_per_group=2)
    code, out = _fit(tmp_path, rows)
    assert code == 0
    outcome = folds.read_fold(out, "fam00", UNTIL, LOG2)
    assert outcome.ml is None
    assert outcome.entry["reason"] == "insufficient_data"


def test_fold_with_a_nan_feature_column_still_finishes(tmp_path: Path) -> None:
    col = "f_load_cpu" if False else None
    from meshforge.controller.ml.schema import NUMERIC_FEATURES

    col = next(iter(NUMERIC_FEATURES))
    rows = _rows(nan_column=getattr(col, "value", str(col)))
    code, out = _fit(tmp_path, rows)
    assert code == 0
    outcome = folds.read_fold(out, "fam00", UNTIL, LOG2)
    assert "fam00" not in outcome.entry["trained_families"]
    for block in outcome.heldout.values():
        assert len(block["y"]) == len(block["y_hat"]) == len(block["ood"])


def test_fold_equals_a_direct_train_when_extra_groups_are_excluded(
    tmp_path: Path,
) -> None:
    rows = _rows()
    extra = rows[-1]["group"]
    groups = tmp_path / "g.json"
    groups.write_text(json.dumps([extra]))
    code, out = _fit(tmp_path, rows, "fam00", "--exclude-groups", str(groups))
    assert code == 0
    doc, _a, _t = read_output(out)
    kept = [r for r in rows if r["family"] != "fam00" and r["group"] != extra]
    assert doc["set_version"] == train_set(table_from_rows(kept), FAST).set_version
    assert doc["fold"]["train_rows"] == len(kept)


def test_fold_is_deterministic_across_runs(tmp_path: Path) -> None:
    rows = _rows()
    versions = []
    for i in range(2):
        d = tmp_path / str(i)
        d.mkdir()
        code, out = _fit(d, rows)
        assert code == 0
        versions.append(
            (
                json.loads((out / "set.json").read_text())["set_version"],
                (out / HELDOUT_FILE).read_bytes(),
            )
        )
    assert versions[0] == versions[1]


def test_no_fold_training_row_belongs_to_any_held_out_group_for_every_family() -> None:
    """Leak property over every family of a bigger dataset."""
    rows = _rows(families=6, groups_per_family=4)
    for family in sorted({r["family"] for r in rows}):
        split = split_rows(rows, family, until=UNTIL)
        held_groups = {r["group"] for r in split.held}
        assert held_groups
        assert not any(r["group"] in held_groups for r in split.train)
        assert not any(r["family"] == family for r in split.train)


# ------------------------------------------------ hostile output, controller side


def _good(tmp_path: Path) -> Path:
    code, out = _fit(tmp_path, _rows())
    assert code == 0
    return out


def test_read_fold_rejects_a_missing_heldout(tmp_path: Path) -> None:
    out = _good(tmp_path)
    (out / HELDOUT_FILE).unlink()
    with pytest.raises(ChildOutputError):
        folds.read_fold(out, "fam00", UNTIL, LOG2)


@pytest.mark.parametrize("content", ["not json", "[1, 2]", "", "null"])
def test_read_fold_rejects_a_hostile_heldout(tmp_path: Path, content: str) -> None:
    out = _good(tmp_path)
    (out / HELDOUT_FILE).write_text(content)
    with pytest.raises(ChildOutputError):
        folds.read_fold(out, "fam00", UNTIL, LOG2)


def test_read_fold_rejects_a_fold_block_for_another_family(tmp_path: Path) -> None:
    out = _good(tmp_path)
    with pytest.raises(ChildOutputError, match="does not name this family"):
        folds.read_fold(out, "fam01", UNTIL, LOG2)


def test_read_fold_rejects_a_missing_fold_block(tmp_path: Path) -> None:
    out = _good(tmp_path)
    doc = json.loads((out / "set.json").read_text())
    del doc["fold"]
    (out / "set.json").write_text(json.dumps(doc))
    with pytest.raises(ChildOutputError):
        folds.read_fold(out, "fam00", UNTIL, LOG2)


def test_read_fold_rejects_a_corrupted_artefact(tmp_path: Path) -> None:
    out = _good(tmp_path)
    art = next(out.glob("*.art"))
    blob = bytearray(art.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    art.write_bytes(bytes(blob))
    with pytest.raises(ChildOutputError):
        folds.read_fold(out, "fam00", UNTIL, LOG2)


def test_read_fold_rejects_a_truncated_artefact_and_a_missing_one(
    tmp_path: Path,
) -> None:
    out = _good(tmp_path)
    art = next(out.glob("*.art"))
    data = art.read_bytes()
    art.write_bytes(data[:10])
    with pytest.raises(ChildOutputError):
        folds.read_fold(out, "fam00", UNTIL, LOG2)
    art.unlink()
    with pytest.raises(ChildOutputError):
        folds.read_fold(out, "fam00", UNTIL, LOG2)


def test_read_fold_rejects_garbage_set_json(tmp_path: Path) -> None:
    out = _good(tmp_path)
    for text in ("{", "[]", '{"fold": 3}', ""):
        (out / "set.json").write_text(text)
        with pytest.raises(ChildOutputError):
            folds.read_fold(out, "fam00", UNTIL, LOG2)


# --------------------------------------------- train_fold: never raises


def _settings(timeout: float = 10.0) -> PlannerSettings:
    return PlannerSettings(ml_retrain_timeout_s=timeout)


async def _train(tmp_path: Path, child: Any) -> folds.FoldOutcome:
    return await folds.train_fold(
        python="python",
        dataset=tmp_path / "d.parquet",
        work=tmp_path,
        family="fam00",
        fingerprints=["a", "b", "a"],
        until=UNTIL,
        settings=_settings(),
        child=child,
    )


def test_train_fold_timeout_and_crash_and_oom_give_no_model(tmp_path: Path) -> None:
    async def timeout(_a: Any, _t: float) -> tuple[int, str]:
        raise TimeoutError

    async def crash(_a: Any, _t: float) -> tuple[int, str]:
        return 1, "boom\nMemoryError"

    async def killed(_a: Any, _t: float) -> tuple[int, str]:
        return -9, ""

    async def silent_ok(_a: Any, _t: float) -> tuple[int, str]:
        return 0, ""  # exit 0 but wrote nothing

    for child, prefix in (
        (timeout, "timeout"),
        (crash, "child_failed"),
        (killed, "child_failed"),
        (silent_ok, "child_output_invalid"),
    ):
        outcome = asyncio.run(_train(tmp_path, child))
        assert outcome.ml is None
        assert outcome.entry["reason"].startswith(prefix), outcome.entry
        assert outcome.entry["source"] == "none"
    # the groups file was deduplicated and sorted
    assert json.loads((tmp_path / "groups.json").read_text()) == ["a", "b"]


def test_train_fold_cancellation_propagates(tmp_path: Path) -> None:
    async def hang(_a: Any, _t: float) -> tuple[int, str]:
        await asyncio.sleep(60)
        return 0, ""

    async def go() -> None:
        task = asyncio.create_task(_train(tmp_path, hang))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


def test_pool_terms_survives_hostile_heldout_blocks() -> None:
    bad = {
        "compute": {"y": [0.1, None, float("nan")], "raw_s": [1, 1], "actual_s": [1]},
        "net": {"y": [], "raw_s": [], "actual_s": [], "y_hat": [], "q": [], "ood": []},
    }
    good = {
        "compute": {
            "y": [0.1, 0.2],
            "raw_s": [10.0, 10.0],
            "actual_s": [11.0, 0.0],  # zero actual: no APE, no division
            "y_hat": [0.1, 1000.0],  # overflow in exp
            "q": [None, 0.1],
            "ood": [False, True],
        }
    }
    out = folds.pool_terms(
        [
            folds.FoldOutcome("a", None, {}, bad),  # type: ignore[arg-type]
            folds.FoldOutcome("b", None, {}, good),
        ],
        max_interval_log=LOG2,
    )
    assert set(out) == {"compute"}
    assert out["compute"]["families"] == 1 and out["compute"]["rows"] == 2
    assert out["compute"]["usable_rate"] == 0.0
    assert out["compute"]["ood_rate"] == 0.5


def test_pool_terms_empty() -> None:
    assert folds.pool_terms([], max_interval_log=LOG2) == {}
