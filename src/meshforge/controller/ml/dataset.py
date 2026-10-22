"""The ML training dataset: rows, canonical Parquet bytes, digest (V0.15 G1, D333).

:func:`build` turns :class:`~.targets.RunInput` values (the loader's read)
into a :class:`Dataset`. Per run: the replay is decoded once (missing ->
``missing_replay``, undecodable -> ``unreadable_replay``, counted once per
decision and never imputed); :func:`~.targets.observations` gives the
learnable (term, node) pairs; :func:`~.features.features` adds each pair's
features from that replay. Large reads go through :class:`Builder` page by
page, so a page's replays are dropped as soon as its rows exist.

**Columns** (:data:`COLUMNS`, in file order): the ids (``decision_id``,
``attempt_id``, ``job_id``, ``node_id``, ``term``, ``group`` = the workload
fingerprint or ``job:<id>`` without one, ``family`` = the run record's
workload family or null, ``finished_at``, ``source`` = the analytical term's
source), every :data:`~.schema.NUMERIC_FEATURES` column (float64, NaN =
missing) and the targets ``y`` = ``ln(actual_s / raw_s)``, ``raw_s``,
``actual_s``.

**Canonical** (D222 rules): rows sorted by ``(finished_at, decision_id,
attempt_id, term, node_id)`` - a total order, so any input order gives the
same file; the pinned writer options; schema metadata = the two versions only
(no build time). :attr:`Dataset.digest` = ``sha256:`` + the SHA-256 of those
bytes - the dataset's identity (G3's ``dataset_digest``).

Pure: no database, no clock, no logging (pyarrow encodes in memory).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from meshforge.controller.ml.features import Predicted, features
from meshforge.controller.ml.schema import (
    DATASET_VERSION,
    FEATURE_VERSION,
    NUMERIC_FEATURES,
    TERM_NAMES,
)
from meshforge.controller.ml.splits import (
    NO_FAMILY,
    Fold,
    group_kfold,
    leave_one_family_out,
)
from meshforge.controller.ml.targets import RunInput, Skip, observations
from meshforge.controller.planner.replay import ReplayError, from_replay
from meshforge.controller.telemetry.parquet_options import (
    ROW_GROUP_SIZE,
    WRITER_OPTIONS,
)

__all__ = [
    "COLUMNS",
    "COLUMN_FINGERPRINTS",
    "COLUMN_NAMES",
    "DATASET_METADATA_KEY",
    "FEATURE_METADATA_KEY",
    "Builder",
    "Dataset",
    "Fold",
    "arrow_schema",
    "build",
    "column_fingerprint",
    "group_kfold",
    "leave_one_family_out",
    "parquet_bytes",
    "sort_key",
]

DATASET_METADATA_KEY: Final = b"meshforge.ml.dataset_version"
FEATURE_METADATA_KEY: Final = b"meshforge.ml.feature_version"

_TEXT, _INT, _FLOAT, _TIME = (
    pa.string(),
    pa.int64(),
    pa.float64(),
    pa.timestamp("us", tz="UTC"),
)

COLUMNS: Final[tuple[tuple[str, pa.DataType, bool], ...]] = (
    ("decision_id", _INT, False),
    ("attempt_id", _TEXT, False),
    ("job_id", _TEXT, False),
    ("node_id", _TEXT, False),
    ("term", _TEXT, False),
    ("group", _TEXT, False),
    ("family", _TEXT, True),
    ("finished_at", _TIME, False),
    ("source", _TEXT, True),
    *((name, _FLOAT, False) for name in NUMERIC_FEATURES),
    ("y", _FLOAT, False),
    ("raw_s", _FLOAT, False),
    ("actual_s", _FLOAT, False),
)
"""(name, Arrow type, nullable). A NaN feature is a float value, not a null."""
COLUMN_NAMES: Final = tuple(name for name, _, _ in COLUMNS)


def column_fingerprint() -> str:
    """SHA-256 of the columns' (name, Arrow type, nullable), in order."""
    payload = json.dumps(
        [[name, str(kind), nullable] for name, kind, nullable in COLUMNS],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


COLUMN_FINGERPRINTS: Final[dict[int, str]] = {
    1: "a0c68990f8f232c0ea53eba0e5765febf1126f3b070ba7b858e769bc843199b3",
}
"""Published :data:`~.schema.DATASET_VERSION` -> :func:`column_fingerprint`.
Never edit one; a column change needs a version bump (a unit test)."""


def arrow_schema() -> pa.Schema:
    return pa.schema(
        [pa.field(name, kind, nullable=nullable) for name, kind, nullable in COLUMNS],
        metadata={
            DATASET_METADATA_KEY: str(DATASET_VERSION).encode(),
            FEATURE_METADATA_KEY: str(FEATURE_VERSION).encode(),
        },
    )


def sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        row["finished_at"],
        row["decision_id"],
        row["attempt_id"],
        row["term"],
        row["node_id"],
    )


def parquet_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    """``rows`` (already in file order) as one Parquet file in memory."""
    schema = arrow_schema()
    sink = pa.BufferOutputStream()
    writer = pq.ParquetWriter(sink, schema, **WRITER_OPTIONS)
    try:
        starts = range(0, len(rows), ROW_GROUP_SIZE) if rows else range(1)
        for start in starts:
            chunk = rows[start : start + ROW_GROUP_SIZE]
            table = pa.Table.from_arrays(
                [
                    pa.array([row[name] for row in chunk], type=kind)
                    for name, kind, _ in COLUMNS
                ],
                schema=schema,
            )
            writer.write_table(table, row_group_size=ROW_GROUP_SIZE)
    finally:
        writer.close()
    data: bytes = sink.getvalue().to_pybytes()
    return data


@dataclass(frozen=True, slots=True)
class Dataset:
    rows: tuple[Mapping[str, Any], ...]
    """In file order (:func:`sort_key`)."""
    counts: Mapping[str, Any]
    """``runs``, ``runs_with_rows``, ``rows``, ``rows_by_term`` (every term),
    ``rows_by_family``, ``skipped`` (every :class:`~.targets.Skip`)."""
    parquet: bytes
    digest: str

    def leave_one_family_out(self) -> list[Fold]:
        return leave_one_family_out(self)

    def group_kfold(self, k: int, seed: int) -> list[Fold]:
        return group_kfold(self, k, seed)


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


@dataclass(slots=True)
class Builder:
    """Accumulates rows page by page (module docstring); :meth:`finish`
    sorts, encodes and digests."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    runs: int = 0
    runs_with_rows: int = 0

    def add(self, runs: Iterable[RunInput]) -> None:
        for run in runs:
            self.add_run(run)

    def add_run(self, run: RunInput) -> None:
        self.runs += 1
        if run.replay is None:
            self.skipped[Skip.MISSING_REPLAY.value] += 1
            return
        try:
            request, snapshot, now = from_replay(run.replay)
        except ReplayError:
            self.skipped[Skip.UNREADABLE_REPLAY.value] += 1
            return
        found, skipped = observations(run)
        self.skipped.update(skipped)
        if found:
            self.runs_with_rows += 1
        outcome = run.outcome
        group = outcome.fingerprint or f"job:{run.job_id}"
        for obs in found:
            values = features(
                request,
                snapshot,
                now,
                obs.node_id,
                Predicted(
                    raw_s=obs.raw_s,
                    source=obs.source,
                    detail=obs.detail,
                    lanes=obs.lanes,
                    chunk_items=obs.chunk_items,
                ),
            )
            self.rows.append(
                {
                    "decision_id": outcome.decision_id,
                    "attempt_id": obs.attempt_id,
                    "job_id": run.job_id,
                    "node_id": obs.node_id,
                    "term": obs.term,
                    "group": group,
                    "family": outcome.family,
                    "finished_at": _utc(obs.finished_at),
                    "source": obs.source,
                    **values,
                    "y": obs.y,
                    "raw_s": obs.raw_s,
                    "actual_s": obs.actual_s,
                }
            )

    def counts(self) -> dict[str, Any]:
        by_term = Counter(str(row["term"]) for row in self.rows)
        by_family = Counter(
            str(row["family"]) if row["family"] else NO_FAMILY for row in self.rows
        )
        return {
            "runs": self.runs,
            "runs_with_rows": self.runs_with_rows,
            "rows": len(self.rows),
            "rows_by_term": {name: by_term.get(name, 0) for name in TERM_NAMES},
            "rows_by_family": dict(sorted(by_family.items())),
            "skipped": {
                reason.value: self.skipped.get(reason.value, 0) for reason in Skip
            },
        }

    def finish(self) -> Dataset:
        self.rows.sort(key=sort_key)
        data = parquet_bytes(self.rows)
        return Dataset(
            rows=tuple(self.rows),
            counts=self.counts(),
            parquet=data,
            digest="sha256:" + hashlib.sha256(data).hexdigest(),
        )


def build(runs: Iterable[RunInput]) -> Dataset:
    """The dataset of ``runs`` (module docstring)."""
    builder = Builder()
    builder.add(runs)
    return builder.finish()
