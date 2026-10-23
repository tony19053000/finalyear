"""``python -m meshforge.controller.ml.train fit`` - one retrain in a child
process (V0.15 G3, D361). The controller's retrain task
(:mod:`meshforge.controller.ml_retrain_task`) runs it; it never touches the
database or the blob store.

Input: the G1 dataset file the controller built, and optionally a
**reference** directory - what serves now (the pinned, else the active set):
``reference.json`` = ``{"kind", "set_version", "members": [{"term",
"algorithm", "version", "file"}]}`` + each learned member's artefact file.

Work: :func:`~.fit.train_set` on all rows (what ships), then
:func:`~.gate.run_gate` (every shipped artefact is loaded and served through
:mod:`..serve` here - so the bytes the controller will later load have
already been parsed once, by this process). When every term is
``insufficient_data`` nothing is gated (status ``insufficient_data``).

Output (``--out DIR``): ``set.json`` (canonical; the set and member rows
the controller stores, the gate document, library versions) and one
``<hex>.art`` per learned member (its bytes; the name is its digest);
``timings.json`` (wall times, kept out of ``set.json`` so it stays
byte-identical). Prints ``{"set": ..., "seconds": ...}`` on stdout.

**Fold mode** (V0.15 G5a, D368: ``--exclude-family F [--until TS]
[--exclude-groups FILE]``): the planner evaluator's model for held-out
family ``F`` - the same :func:`~.fit.train_set` on the rows
:func:`~.fold.split_rows` keeps (no row of ``F`` or of its workload groups,
none after ``TS``), **no gate** (a fold set is never registered); ``set.json``
gets a ``fold`` block (what it trained on) and ``heldout.json`` holds every
learned member's predictions for ``F``'s rows, made through the serving
loader (:func:`~.fold.heldout_predictions`).

Exit codes: 0 done (accepted or rejected - the gate's answer is data);
1 = the dataset or the reference cannot be read, the output cannot be
written, or the training failed. Messages go to stderr, one line.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from meshforge.common.exit_codes import ExitCode
from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.serve import LoadedModel, load_model
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import (
    DatasetValueError,
    DatasetVersionError,
    load,
    load_rows,
    table_from_rows,
)
from meshforge.controller.ml.train.fit import TrainResult, train_set
from meshforge.controller.ml.train.fold import (
    HELDOUT_FILE,
    fold_document,
    held_table,
    heldout_predictions,
    split_rows,
)
from meshforge.controller.ml.train.gate import GateResult, Reference, run_gate
from meshforge.controller.ml.train.report import clean, library_versions

__all__ = [
    "SET_DOC_VERSION",
    "SET_FILE",
    "FoldArgs",
    "artefact_name",
    "read_groups",
    "read_reference",
    "run_fit",
    "run_fold",
]

SET_FILE: Final = "set.json"
SET_DOC_VERSION: Final = 1
_REFERENCE_FILE: Final = "reference.json"


@dataclass(frozen=True, slots=True)
class FoldArgs:
    """``fit --exclude-family`` (module docstring, fold mode)."""

    family: str
    until: dt.datetime | None = None
    groups: Sequence[str] = ()


def read_groups(path: Path | None) -> list[str]:
    """``--exclude-groups FILE``: a JSON list of workload fingerprints;
    ``ValueError`` for anything else."""
    if path is None:
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, list) or not all(isinstance(g, str) for g in doc):
        raise ValueError("--exclude-groups is not a JSON list of strings")
    return list(doc)


def artefact_name(version: str) -> str:
    """``sha256:<hex>`` -> ``<hex>.art``."""
    return version.split(":", 1)[1] + ".art"


def _err(message: str) -> None:
    sys.stderr.write(message.rstrip("\n") + "\n")


def read_reference(directory: Path | None) -> Reference:
    """The serving reference (module docstring); a member whose artefact is
    missing or cannot be loaded is judged against the calibration
    (``unreadable``). ``ValueError`` for an unreadable ``reference.json``."""
    if directory is None:
        return Reference()
    doc = json.loads((directory / _REFERENCE_FILE).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("members"), list):
        raise ValueError("reference.json is not a reference document")  # noqa: TRY004 - bad file content, not a caller type error
    models: dict[str, LoadedModel] = {}
    unreadable: dict[str, str] = {}
    for member in doc["members"]:
        if not isinstance(member, dict) or member.get("algorithm") == "none":
            continue
        term = str(member.get("term"))
        try:
            data = (directory / str(member.get("file"))).read_bytes()
            models[term] = load_model(data, expected_version=str(member.get("version")))
        except OSError:
            unreadable[term] = "artefact_missing"
        except ArtefactError as exc:
            unreadable[term] = str(exc.details.get("reason", "unreadable"))
    return Reference(
        kind=str(doc.get("kind", "active")),
        set_version=doc.get("set_version"),
        models=models,
        unreadable=unreadable,
    )


def _settings_doc(settings: CompareSettings) -> dict[str, Any]:
    return {
        "terms": list(settings.terms),
        "algorithms": list(settings.algorithms),
        "min_rows": settings.min_rows,
        "min_families": settings.min_families,
        "inner_k": settings.inner_k,
        "alpha": settings.alpha,
        "seed": settings.seed,
        "max_interval_log": settings.max_interval_log,
        "family_tolerance_log": settings.family_tolerance_log,
        "min_effect_log": settings.min_effect_log,
        "ood": {
            "range_margin": settings.ood_margin,
            "k": settings.ood_k,
            "percentile": settings.ood_percentile,
        },
        "calibration": {
            "window_runs": settings.calibration.window_runs,
            "shrink_k": settings.calibration.shrink_k,
            "clamp": list(settings.calibration.clamp),
        },
    }


def set_document(
    result: TrainResult,
    gate: GateResult | None,
    settings: CompareSettings,
) -> dict[str, Any]:
    """``set.json`` (module docstring)."""
    insufficient = all(
        (m.reason or "").startswith("insufficient_data") for m in result.members
    )
    doc: dict[str, Any] = clean(
        {
            "set_doc_version": SET_DOC_VERSION,
            "set_version": result.set_version,
            "dataset_digest": result.dataset_digest,
            "feature_version": result.feature_version,
            "rows": result.rows,
            "status": "insufficient_data" if insufficient else "trained",
            "accepted": None if gate is None else gate.accepted,
            "rule": None if gate is None else gate.rule,
            "reason": None if gate is None else gate.reason,
            "gate": None if gate is None else gate.doc,
            "members": [
                {
                    "term": m.term,
                    "algorithm": m.algorithm,
                    "version": m.version,
                    "params": m.params,
                    "metrics": m.metrics,
                    "reason": m.reason,
                    "artefact": None
                    if m.artefact is None
                    else artefact_name(m.version),
                    "artefact_bytes": None if m.artefact is None else len(m.artefact),
                }
                for m in result.members
            ],
            "settings": _settings_doc(settings),
            "libraries": library_versions(),
        }
    )
    return doc


def run_fit(
    dataset: Path, out: Path, reference_dir: Path | None, settings: CompareSettings
) -> int:
    """The ``fit`` command (module docstring)."""
    started = time.monotonic()
    try:
        table = load(dataset)
    except (DatasetVersionError, DatasetValueError) as exc:
        _err(f"{dataset}: {exc}")
        return int(ExitCode.FAILURE)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is one answer
        _err(f"{dataset}: cannot read the dataset ({type(exc).__name__})")
        return int(ExitCode.FAILURE)
    try:
        reference = read_reference(reference_dir)
    except (OSError, ValueError) as exc:
        _err(f"cannot read the reference set ({type(exc).__name__})")
        return int(ExitCode.FAILURE)
    loaded = time.monotonic()
    try:
        result = train_set(table, settings)
        trained = time.monotonic()
        doc = set_document(result, None, settings)
        gate = None
        if doc["status"] == "trained":
            gate = run_gate(table, settings, result, reference)
            doc = set_document(result, gate, settings)
    except Exception as exc:  # noqa: BLE001 - one line for the controller's log
        detail = str(exc).splitlines()[0] if str(exc) else "no detail"
        _err(f"the training failed ({type(exc).__name__}: {detail})")
        return int(ExitCode.FAILURE)
    gated = time.monotonic()
    try:
        out.mkdir(parents=True, exist_ok=True)
        for member in result.members:
            if member.artefact is not None:
                (out / artefact_name(member.version)).write_bytes(member.artefact)
        path = out / SET_FILE
        path.write_text(
            json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        timings = {
            "load_s": round(loaded - started, 3),
            "train_s": round(trained - loaded, 3),
            "gate_s": round(gated - trained, 3),
            "total_s": round(time.monotonic() - started, 3),
            "rows": table.n,
        }
        (out / "timings.json").write_text(
            json.dumps(timings, sort_keys=True) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        _err(f"cannot write the set to {out}: {exc.strerror or exc}")
        return int(ExitCode.FAILURE)
    sys.stdout.write(json.dumps({"set": str(path), "seconds": timings["total_s"]}))
    sys.stdout.write("\n")
    return int(ExitCode.OK)


def _write(out: Path, result: TrainResult, files: dict[str, str]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for member in result.members:
        if member.artefact is not None:
            (out / artefact_name(member.version)).write_bytes(member.artefact)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")


def _canonical(doc: object) -> str:
    return json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n"


def run_fold(
    dataset: Path, out: Path, fold: FoldArgs, settings: CompareSettings
) -> int:
    """The ``fit --exclude-family`` command (module docstring, fold mode)."""
    started = time.monotonic()
    try:
        rows = load_rows(dataset)
        split = split_rows(rows, fold.family, until=fold.until, groups=fold.groups)
        table = table_from_rows(split.train)
        held = held_table(split)
    except (DatasetVersionError, DatasetValueError) as exc:
        _err(f"{dataset}: {exc}")
        return int(ExitCode.FAILURE)
    except Exception as exc:  # noqa: BLE001 - any unreadable file is one answer
        _err(f"{dataset}: cannot read the dataset ({type(exc).__name__})")
        return int(ExitCode.FAILURE)
    loaded = time.monotonic()
    try:
        result = train_set(table, settings)
        trained = time.monotonic()
        doc = set_document(result, None, settings)
        doc["fold"] = fold_document(split, result)
        predictions = heldout_predictions(result, held)
    except Exception as exc:  # noqa: BLE001 - one line for the controller's log
        detail = str(exc).splitlines()[0] if str(exc) else "no detail"
        _err(f"the fold training failed ({type(exc).__name__}: {detail})")
        return int(ExitCode.FAILURE)
    timings = {
        "load_s": round(loaded - started, 3),
        "train_s": round(trained - loaded, 3),
        "predict_s": round(time.monotonic() - trained, 3),
        "total_s": round(time.monotonic() - started, 3),
        "rows": table.n,
    }
    try:
        _write(
            out,
            result,
            {
                SET_FILE: _canonical(doc),
                HELDOUT_FILE: _canonical(predictions),
                "timings.json": json.dumps(timings, sort_keys=True) + "\n",
            },
        )
    except OSError as exc:
        _err(f"cannot write the fold set to {out}: {exc.strerror or exc}")
        return int(ExitCode.FAILURE)
    path = out / SET_FILE
    sys.stdout.write(json.dumps({"set": str(path), "seconds": timings["total_s"]}))
    sys.stdout.write("\n")
    return int(ExitCode.OK)
