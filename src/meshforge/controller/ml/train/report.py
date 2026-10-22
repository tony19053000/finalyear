"""The comparison report: canonical JSON + a short Markdown summary (V0.15 G2,
D351-D354).

**Canonical** (D352): sorted keys, floats rounded to 6 significant digits,
a non-finite number written as ``null`` (an infinite conformal ``q`` is
"no usable interval"), no wall time, no path - the same dataset content +
the same library versions give **byte-identical** ``report.json``. The
header names the dataset's content digest and versions, every setting and
seed, and the library versions.

**Failure classifier** (D354): the dataset's ``node_failures`` (from the
G1 sidecar ``FILE.json``, when present and naming the same digest) ->
``not_trained`` (``N < 50`` events) or ``eligible``; ``unknown`` without a
matching sidecar. The classifier itself is built only once a real dataset
reaches 50 (roadmap tech-debt item).
"""

from __future__ import annotations

import json
import math
import platform
from collections.abc import Mapping, Sequence
from importlib import metadata
from typing import Any, Final

from meshforge.controller.ml.train.algorithms import GRID, Config
from meshforge.controller.ml.train.compare import CompareSettings, TermResult
from meshforge.controller.ml.train.data import Table
from meshforge.controller.ml.train.select import Selection

__all__ = [
    "FAILURE_CLASSIFIER_MIN_EVENTS",
    "REPORT_VERSION",
    "canonical_json",
    "failure_classifier",
    "library_versions",
    "markdown",
    "report",
]

REPORT_VERSION: Final = 1
FAILURE_CLASSIFIER_MIN_EVENTS: Final = 50
_LIBRARIES: Final = (
    ("numpy", "numpy"),
    ("scikit-learn", "scikit-learn"),
    ("lightgbm", "lightgbm"),
    ("xgboost", "xgboost"),
    ("scipy", "scipy"),
    ("pyarrow", "pyarrow"),
)
_XGBOOST_DISTS: Final = ("xgboost", "xgboost-cpu")


def _num(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    if value == 0.0:
        return 0.0
    return float(f"{value:.6g}")


def _clean(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str | int):
        return value
    if isinstance(value, float):
        return _num(value)
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, Sequence):
        return [_clean(v) for v in value]
    if hasattr(value, "item"):  # a numpy scalar
        return _clean(value.item())
    raise TypeError(f"not a report value: {type(value).__name__}")


def library_versions() -> dict[str, str | None]:
    """The installed versions the report depends on (D352)."""
    out: dict[str, str | None] = {"python": platform.python_version()}
    for key, dist in _LIBRARIES:
        names = _XGBOOST_DISTS if key == "xgboost" else (dist,)
        version = None
        for name in names:
            try:
                version = metadata.version(name)
                break
            except metadata.PackageNotFoundError:
                continue
        out[key] = version
    return out


def failure_classifier(node_failures: int | None) -> dict[str, Any]:
    """D354's line (module docstring)."""
    if node_failures is None:
        status = "unknown"
    elif node_failures < FAILURE_CLASSIFIER_MIN_EVENTS:
        status = "not_trained"
    else:
        status = "eligible"
    return {
        "node_failures": node_failures,
        "min_events": FAILURE_CLASSIFIER_MIN_EVENTS,
        "status": status,
    }


def _config(config: Config) -> dict[str, Any]:
    return {
        "name": config.name,
        "algorithm": config.algorithm,
        "params": dict(config.params),
        "learned": config.learned,
    }


def _selection(sel: Selection, q: float | None) -> dict[str, Any]:
    return {
        "config": _config(sel.selected),
        "best": sel.best.name,
        "inner_k": sel.k,
        "conformal_q": q,
        "interval_width": None if q is None else 2.0 * q,
        "inner_scores": [
            {"config": s.config.name, "mean_male": s.mean, "se": s.se}
            for s in sel.scores
        ],
    }


def _term(result: TermResult) -> dict[str, Any]:
    out: dict[str, Any] = {
        "term": result.term,
        "status": result.status,
        "rows": result.rows,
        "families": result.families,
        "groups": result.groups,
        "reason": result.reason,
    }
    if result.status == "insufficient_data" or result.final is None:
        return out
    out.update(
        {
            "shipped": _selection(result.final, result.final_q),
            "outer": [
                {
                    "family": f.family,
                    "train_rows": f.train_rows,
                    "test_rows": f.test_rows,
                    "selected": f.selected.name,
                    "best": f.best.name,
                    "inner_k": f.inner_k,
                    "conformal_q": f.q,
                    "quantile_coverage": f.quantile_coverage,
                    "quantile_median_width": f.quantile_median_width,
                }
                for f in result.folds
            ],
            "per_family": result.per_family,
            "pooled": result.pooled,
            "vs_calibration": result.verdict,
            "would_reject": result.would_reject,
            "interval_alternatives": {
                "conformal": {
                    "coverage": result.pooled.get("coverage"),
                    "median_width": result.pooled.get("median_width"),
                },
                "lightgbm_quantile": result.quantile,
                "shipped": "conformal",
            },
            "importance": [
                {"feature": name, "male_increase": value}
                for name, value in result.importance
            ],
        }
    )
    return out


def report(
    table: Table,
    settings: CompareSettings,
    results: Sequence[TermResult],
    *,
    node_failures: int | None,
    sidecar: str,
) -> dict[str, Any]:
    """The report document (canonical once through :func:`canonical_json`).
    ``sidecar`` = ``matched`` | ``missing`` | ``digest_mismatch`` |
    ``unreadable``."""
    doc: dict[str, Any] = _clean(
        {
            "report_version": REPORT_VERSION,
            "dataset": {
                "digest": table.digest,
                "dataset_version": table.dataset_version,
                "feature_version": table.feature_version,
                "rows": table.n,
                "sidecar": sidecar,
            },
            "settings": {
                "terms": list(settings.terms),
                "algorithms": list(settings.algorithms),
                "grid": {name: list(GRID[name]) for name in sorted(GRID)},
                "with_family": settings.with_family,
                "min_rows": settings.min_rows,
                "min_families": settings.min_families,
                "inner_k": settings.inner_k,
                "alpha": settings.alpha,
                "max_interval_log": settings.max_interval_log,
                "family_tolerance_log": settings.family_tolerance_log,
                "coverage_reject": settings.coverage_reject,
                "bootstrap_resamples": settings.bootstrap_resamples,
                "bootstrap_seed": settings.bootstrap_seed,
                "seed": settings.seed,
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
            },
            "libraries": library_versions(),
            "failure_classifier": failure_classifier(node_failures),
            "terms": [_term(r) for r in results],
        }
    )
    return doc


def canonical_json(doc: Mapping[str, Any]) -> str:
    """Sorted keys, 2-space indent, no NaN (D352)."""
    return json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _cell(value: Any, digits: int = 3) -> str:
    if isinstance(value, bool) or value is None:
        return "-" if value is None else str(value).lower()
    if isinstance(value, int | float):
        return f"{value:.{digits}f}"
    return str(value)


def _pct(value: Any) -> str:
    return "-" if not isinstance(value, int | float) else f"{100.0 * value:.1f}%"


def term_rows(doc: Mapping[str, Any]) -> list[list[str]]:
    """The per-term table (the CLI prints the same columns)."""
    rows = []
    for term in doc.get("terms", []):
        shipped = term.get("shipped") or {}
        pooled = term.get("pooled") or {}
        verdict = term.get("vs_calibration") or {}
        rows.append(
            [
                str(term.get("term")),
                str(term.get("rows")),
                str(term.get("families")),
                str(term.get("status")),
                str((shipped.get("config") or {}).get("name", "-")),
                _cell(pooled.get("male_model")),
                _cell(pooled.get("male_calibration")),
                str(verdict.get("verdict", "-")),
                _pct(pooled.get("coverage")),
                _pct(pooled.get("usable_rate")),
            ]
        )
    return rows


TABLE_HEADER: Final = (
    "term",
    "rows",
    "families",
    "status",
    "selected",
    "MALE model",
    "MALE calib",
    "vs calib",
    "coverage",
    "usable",
)


def markdown(doc: Mapping[str, Any]) -> str:
    """A short Markdown summary of the report (deterministic too)."""
    dataset = doc["dataset"]
    failures = doc["failure_classifier"]
    libs = ", ".join(f"{k} {v}" for k, v in sorted(doc["libraries"].items()))
    lines = [
        "# ML comparison report",
        "",
        f"- dataset `{dataset['digest']}` (dataset v{dataset['dataset_version']}, "
        f"features v{dataset['feature_version']}, {dataset['rows']} rows)",
        f"- libraries: {libs}",
        f"- seeds: model {doc['settings']['seed']}, bootstrap "
        f"{doc['settings']['bootstrap_seed']} ({doc['settings']['bootstrap_resamples']}"
        " resamples)",
        "- held-out numbers come from leave-one-family-out; the model was chosen on "
        "the other families only (inner grouped k-fold, one-standard-error rule)",
        "",
        "| " + " | ".join(TABLE_HEADER) + " |",
        "|" + "---|" * len(TABLE_HEADER),
    ]
    lines += ["| " + " | ".join(row) + " |" for row in term_rows(doc)]
    lines.append("")
    for term in doc.get("terms", []):
        if term.get("status") == "insufficient_data":
            lines.append(f"- **{term['term']}**: insufficient data ({term['reason']})")
            continue
        reject = term.get("would_reject") or {}
        flags = []
        if reject.get("coverage_below"):
            flags.append("held-out coverage below the limit")
        if reject.get("families_worse"):
            flags.append(
                "worse than calibration on " + ", ".join(reject["families_worse"])
            )
        top = ", ".join(i["feature"] for i in term.get("importance", [])[:5]) or "-"
        lines.append(
            f"- **{term['term']}**: {term['status']}; would reject: "
            f"{'; '.join(flags) if flags else 'no'}; top features: {top}"
        )
    if failures["status"] == "not_trained":
        line = (
            f"failure classifier: not trained, {failures['node_failures']} < "
            f"{failures['min_events']} events"
        )
    elif failures["status"] == "eligible":
        line = (
            f"failure classifier: eligible ({failures['node_failures']} >= "
            f"{failures['min_events']} events)"
        )
    else:
        line = "failure classifier: unknown (no matching dataset sidecar)"
    lines += ["", line, ""]
    return "\n".join(lines)
