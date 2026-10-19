"""Benchmark labels as a flat table and a byte-reproducible Parquet file (V0.9 G5, D218/D222).

One row per (group, configuration) of :func:`.labels.build_labels`, sorted by
(``group_id``, ``config_id``): the group's label repeated on each of its
configuration rows, then that configuration's costs (``<metric>_n``,
``_median``, ``_mean``, ``_std``, ``_cv``, ``_ci_low``, ``_ci_high``,
``_ci_coverage``, ``_outliers`` for every :data:`~.labels.METRICS`). The
columns are declared once (:data:`LABEL_COLUMNS`) and pinned by
:data:`LABEL_FINGERPRINTS` per :data:`~.labels.LABELS_VERSION` - a unit test
fails if they change without a version bump, like the run-record schema.

Same reproducibility rules as the record export (D222, :mod:`.export`): the
same records give the same bytes - stable row order and column order, ``json``
columns as canonical JSON text, no export-time value in the file (its only
custom metadata is the labels version), the pinned writer options, and the
file is encoded one row group at a time (:func:`iter_labels_parquet`, streamed
by the API).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Generator, Mapping, Sequence
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from meshforge.bench.stats import Summary
from meshforge.controller.telemetry.export import (
    ARROW_TYPES,
    ROW_GROUP_SIZE,
    WRITER_OPTIONS,
    Chunks,
)
from meshforge.controller.telemetry.labels import (
    LABELS_VERSION,
    METRICS,
    CampaignLabels,
)
from meshforge.controller.telemetry.record import canonical_json
from meshforge.controller.telemetry.schema import Column, Kind

__all__ = [
    "LABELS_METADATA_KEY",
    "LABEL_COLUMNS",
    "LABEL_COLUMN_NAMES",
    "LABEL_FINGERPRINTS",
    "iter_labels_parquet",
    "label_fingerprint",
    "labels_arrow_schema",
    "labels_parquet_bytes",
    "labels_table_rows",
]

LABELS_METADATA_KEY: Final = b"meshforge.labels.version"


def _c(name: str, kind: Kind, doc: str, *, nullable: bool = True) -> Column:
    return Column(name, kind, nullable, doc)


_T, _I, _F, _B, _J = Kind.TEXT, Kind.INT64, Kind.FLOAT64, Kind.BOOL, Kind.JSON

_STAT_COLUMNS: Final = (
    ("n", _I, "values measured", False),
    ("median", _F, "median", True),
    ("mean", _F, "mean", True),
    ("std", _F, "sample standard deviation (n - 1)", True),
    ("cv", _F, "coefficient of variation; null when n < 2 or mean = 0", True),
    ("ci_low", _F, "median CI lower bound (order statistic)", True),
    ("ci_high", _F, "median CI upper bound (order statistic)", True),
    ("ci_coverage", _F, "achieved coverage of that interval", True),
    ("outliers", _I, "values outside 3.5 scaled MADs", True),
)

LABEL_COLUMNS: Final[tuple[Column, ...]] = (
    # --- the group and its label -----------------------------------------
    _c("labels_version", _I, "Labels version of this row.", nullable=False),
    _c("campaign_id", _T, "The campaign.", nullable=False),
    _c("group_id", _T, "Workload + params + condition (D218).", nullable=False),
    _c("family", _T, "Workload family.", nullable=False),
    _c("condition", _T, "Canonical condition (none = baseline).", nullable=False),
    _c("params", _J, "The group's workload parameters.", nullable=False),
    _c("label", _T, "best / tie / insufficient / pending.", nullable=False),
    _c("best_config_id", _T, "label = best: the cheapest configuration."),
    _c("best_strategy", _T, "label = best: its strategy (local / mode1)."),
    _c("best_node_id", _T, "label = best: its node."),
    _c("margin_pct", _F, "Runner-up median over the lowest median, in % above."),
    _c(
        "tied_config_ids",
        _J,
        "label = tie: the configurations not separated from the lowest median.",
        nullable=False,
    ),
    _c("group_planner_node_id", _T, "The planner's preferred node (most frequent)."),
    _c("planner_agrees", _B, "label = best: best node == planner preference."),
    _c("reason", _T, "Why insufficient / pending."),
    # --- one configuration --------------------------------------------------
    _c(
        "config_id", _T, "The configuration (strategy + node + origin).", nullable=False
    ),
    _c("strategy", _T, "local / mode1.", nullable=False),
    _c("node_id", _T, "Where it ran.", nullable=False),
    _c("origin_node_id", _T, "The submitter.", nullable=False),
    _c("config_state", _T, "measured / unsupported.", nullable=False),
    _c("repetitions", _I, "Measured repetitions kept.", nullable=False),
    _c("planner_node_id", _T, "Planner preference in its records (most frequent)."),
    _c("is_best", _B, "This configuration is the group's best.", nullable=False),
    _c("in_tie", _B, "This configuration is in the group's tied set.", nullable=False),
    # --- its costs ----------------------------------------------------------
    *(
        _c(f"{metric}_{stat}", kind, f"{metric}: {doc}.", nullable=nullable)
        for metric in METRICS
        for stat, kind, doc, nullable in _STAT_COLUMNS
    ),
)
LABEL_COLUMN_NAMES: Final = tuple(c.name for c in LABEL_COLUMNS)


def label_fingerprint(columns: tuple[Column, ...] = LABEL_COLUMNS) -> str:
    """SHA-256 of the columns' (name, kind, nullable), in order."""
    payload = json.dumps(
        [[c.name, c.kind.value, c.nullable] for c in columns], separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


LABEL_FINGERPRINTS: Final[dict[int, str]] = {
    1: "bd6828e1d2590c3a5c7b47c5334aaa83c3a0492ea1171d1b2e86494dfe8e8508",
}
"""Published labels versions -> their column fingerprint. Never edit one."""


def labels_arrow_schema() -> pa.Schema:
    return pa.schema(
        [
            pa.field(c.name, ARROW_TYPES[c.kind], nullable=c.nullable)
            for c in LABEL_COLUMNS
        ],
        metadata={LABELS_METADATA_KEY: str(LABELS_VERSION).encode()},
    )


def _stats(metric: str, summary: Summary | None) -> dict[str, Any]:
    if summary is None:
        return {
            f"{metric}_{stat}": (0 if stat == "n" else None)
            for stat, *_ in _STAT_COLUMNS
        }
    return {
        f"{metric}_n": summary.n,
        f"{metric}_median": summary.median,
        f"{metric}_mean": summary.mean,
        f"{metric}_std": summary.std,
        f"{metric}_cv": summary.cv,
        f"{metric}_ci_low": summary.ci_low,
        f"{metric}_ci_high": summary.ci_high,
        f"{metric}_ci_coverage": summary.ci_coverage,
        f"{metric}_outliers": summary.outliers,
    }


def labels_table_rows(labels: CampaignLabels) -> list[dict[str, Any]]:
    """The flat rows (module docstring), in file order. Pure."""
    out: list[dict[str, Any]] = []
    for group in sorted(labels.groups, key=lambda g: g.group_id):
        head = {
            "labels_version": LABELS_VERSION,
            "campaign_id": labels.campaign_id,
            "group_id": group.group_id,
            "family": group.family,
            "condition": group.condition,
            "params": dict(group.params),
            "label": group.label,
            "best_config_id": group.best_config_id,
            "best_strategy": group.best_strategy,
            "best_node_id": group.best_node_id,
            "margin_pct": group.margin_pct,
            "tied_config_ids": list(group.tied_config_ids),
            "group_planner_node_id": group.planner_node_id,
            "planner_agrees": group.planner_agrees,
            "reason": group.reason,
        }
        for config in sorted(group.configs, key=lambda c: c.config_id):
            row = {
                **head,
                "config_id": config.config_id,
                "strategy": config.strategy,
                "node_id": config.node_id,
                "origin_node_id": config.origin_node_id,
                "config_state": config.state,
                "repetitions": config.repetitions,
                "planner_node_id": config.planner_node_id,
                "is_best": config.config_id == group.best_config_id,
                "in_tie": config.config_id in group.tied_config_ids,
            }
            for metric in METRICS:
                row.update(_stats(metric, config.metrics.get(metric)))
            out.append(row)
    return out


def _cell(column: Column, row: Mapping[str, Any]) -> Any:
    value = row.get(column.name)
    if value is None:
        if not column.nullable:
            raise ValueError(f"label column {column.name} is null")
        return None
    if column.kind is Kind.JSON:
        return canonical_json(value)
    return value


def _table(rows: Sequence[Mapping[str, Any]], schema: pa.Schema) -> pa.Table:
    return pa.Table.from_arrays(
        [
            pa.array([_cell(c, r) for r in rows], type=ARROW_TYPES[c.kind])
            for c in LABEL_COLUMNS
        ],
        schema=schema,
    )


def iter_labels_parquet(
    rows: Sequence[Mapping[str, Any]],
) -> Generator[bytes, None, None]:
    """``rows`` (:func:`labels_table_rows`, already in file order) as
    consecutive pieces of one Parquet file: one per row group, the footer
    last - the record export's streaming pattern (:func:`.export.iter_parquet`)."""
    schema = labels_arrow_schema()
    sink = Chunks()
    writer = pq.ParquetWriter(sink, schema, **WRITER_OPTIONS)
    try:
        if not rows:
            writer.write_table(_table([], schema), row_group_size=ROW_GROUP_SIZE)
        for start in range(0, len(rows), ROW_GROUP_SIZE):
            group = _table(rows[start : start + ROW_GROUP_SIZE], schema)
            writer.write_table(group, row_group_size=ROW_GROUP_SIZE)
            del group
            piece = sink.take()
            if piece:
                yield piece
    finally:
        writer.close()
    tail = sink.take()
    if tail:
        yield tail


def labels_parquet_bytes(labels: CampaignLabels) -> bytes:
    """The whole labels file in memory (tests, scripts)."""
    return b"".join(iter_labels_parquet(labels_table_rows(labels)))
