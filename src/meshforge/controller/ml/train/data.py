"""A G1 dataset file as arrays (V0.15 G2, D347/D352). Pure apart from reading
the one file it is given.

:func:`load` reads the Parquet file, refuses a ``dataset_version`` /
``feature_version`` this code does not know or a column list that is not
the published one (:class:`DatasetVersionError` - the CLI exits 2), puts the
rows in the canonical order (:func:`~meshforge.controller.ml.dataset.
sort_key`) and computes the **content digest** = the digest of the canonical
re-encoding (:func:`~meshforge.controller.ml.dataset.parquet_bytes`). A file
the controller wrote is already canonical, so the content digest equals its
G1 digest; a file whose rows were shuffled (or re-written by another tool)
gives the same rows, the same digest and the same report (D352).

:class:`Table` holds the numeric features as one ``float64`` matrix in
:data:`~meshforge.controller.ml.schema.NUMERIC_FEATURES` order (NaN =
missing, never 0, D334) and the per-row ids the baselines and splits need.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

from meshforge.controller.ml.dataset import (
    COLUMN_NAMES,
    DATASET_METADATA_KEY,
    FEATURE_METADATA_KEY,
    parquet_bytes,
    sort_key,
)
from meshforge.controller.ml.schema import (
    DATASET_VERSION,
    FEATURE_VERSION,
    FEATURES,
    NUMERIC_FEATURES,
    FeatureGroup,
)
from meshforge.controller.ml.splits import NO_FAMILY, group_families

__all__ = [
    "KNOWN_VERSIONS",
    "DatasetValueError",
    "DatasetVersionError",
    "FloatArray",
    "IntArray",
    "Table",
    "feature_indices",
    "load",
    "load_bytes",
    "table_from_rows",
]

type FloatArray = npt.NDArray[np.float64]
type IntArray = npt.NDArray[np.intp]

KNOWN_VERSIONS: Final = frozenset({(DATASET_VERSION, FEATURE_VERSION)})
"""(dataset_version, feature_version) pairs this code trains on."""


class DatasetVersionError(ValueError):
    """The file is not a dataset this code knows (version or columns)."""


class DatasetValueError(ValueError):
    """The file has the right schema but numbers G1 never writes: a
    non-finite ``y``, a non-finite or non-positive ``raw_s`` / ``actual_s``,
    or an infinite feature (a missing feature is NaN - fine - never inf)."""


@dataclass(frozen=True, slots=True)
class Table:
    digest: str
    """``sha256:`` + the SHA-256 of the canonical re-encoding (module
    docstring)."""
    dataset_version: int
    feature_version: int
    features: tuple[str, ...]
    X: FloatArray
    """(rows, features), NaN = missing."""
    y: FloatArray
    raw_s: FloatArray
    actual_s: FloatArray
    term: tuple[str, ...]
    group: tuple[str, ...]
    family: tuple[str, ...]
    """The row's run-record family; :data:`~..splits.NO_FAMILY` when null."""
    family_raw: tuple[str | None, ...]
    """The row's family as stored (None = null): the calibration baseline's
    ``node_family`` key."""
    node_id: tuple[str, ...]
    source: tuple[str | None, ...]
    decision_id: tuple[int, ...]
    attempt_id: tuple[str, ...]
    finished_at: tuple[dt.datetime, ...]

    @property
    def n(self) -> int:
        return len(self.term)

    def rows_of(self, term: str) -> IntArray:
        """Row indices of ``term``, in table order."""
        return np.asarray(
            [i for i, t in enumerate(self.term) if t == term], dtype=np.intp
        )

    def split_rows(self, rows: IntArray) -> list[Mapping[str, Any]]:
        """``rows`` as the mappings :mod:`~..splits` takes (group, family)."""
        return [{"group": self.group[i], "family": self.family_raw[i]} for i in rows]

    def families_of(self, rows: IntArray) -> dict[str, str]:
        """group -> its family (:func:`~..splits.group_families`) over
        ``rows``."""
        return group_families(self.split_rows(rows))


def feature_indices(*groups: FeatureGroup) -> tuple[int, ...]:
    """Column indices (in :data:`NUMERIC_FEATURES`) of the given groups."""
    wanted = set(groups)
    by_name = {f.name: f for f in FEATURES}
    return tuple(
        i for i, name in enumerate(NUMERIC_FEATURES) if by_name[name].group in wanted
    )


def _version(metadata: Mapping[bytes, bytes] | None, key: bytes) -> int:
    raw = (metadata or {}).get(key)
    try:
        return int(raw.decode()) if raw is not None else -1
    except ValueError:
        return -1


def _check_values(
    X: FloatArray, y: FloatArray, raw_s: FloatArray, actual_s: FloatArray
) -> None:
    """:class:`DatasetValueError` naming how many rows are bad (G1 never
    writes these values; a hand-edited or corrupt file is refused whole)."""
    problems = []
    bad_y = int(np.count_nonzero(~np.isfinite(y)))
    if bad_y:
        problems.append(f"{bad_y} row(s) with a non-finite y")
    for name, values in (("raw_s", raw_s), ("actual_s", actual_s)):
        bad = int(np.count_nonzero(~np.isfinite(values) | (values <= 0.0)))
        if bad:
            problems.append(f"{bad} row(s) with a non-finite or non-positive {name}")
    bad_x = int(np.count_nonzero(np.isinf(X)))
    if bad_x:
        problems.append(f"{bad_x} infinite feature value(s)")
    if problems:
        raise DatasetValueError(
            "the dataset holds values G1 never writes: " + "; ".join(problems)
        )


def table_from_rows(rows: Sequence[Mapping[str, Any]]) -> Table:
    """A :class:`Table` of dataset rows (any order; mappings with every
    :data:`~..dataset.COLUMN_NAMES` key)."""
    ordered = sorted(rows, key=sort_key)
    data = parquet_bytes(ordered)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    n = len(ordered)
    matrix = np.empty((n, len(NUMERIC_FEATURES)), dtype=np.float64)
    for j, name in enumerate(NUMERIC_FEATURES):
        matrix[:, j] = [float(row[name]) for row in ordered]

    def column(name: str) -> FloatArray:
        return np.asarray([float(row[name]) for row in ordered], dtype=np.float64)

    _check_values(matrix, column("y"), column("raw_s"), column("actual_s"))
    family_raw = tuple(str(row["family"]) if row["family"] else None for row in ordered)
    return Table(
        digest=digest,
        dataset_version=DATASET_VERSION,
        feature_version=FEATURE_VERSION,
        features=NUMERIC_FEATURES,
        X=matrix,
        y=column("y"),
        raw_s=column("raw_s"),
        actual_s=column("actual_s"),
        term=tuple(str(row["term"]) for row in ordered),
        group=tuple(str(row["group"]) for row in ordered),
        family=tuple(f if f is not None else NO_FAMILY for f in family_raw),
        family_raw=family_raw,
        node_id=tuple(str(row["node_id"]) for row in ordered),
        source=tuple(
            None if row["source"] is None else str(row["source"]) for row in ordered
        ),
        decision_id=tuple(int(row["decision_id"]) for row in ordered),
        attempt_id=tuple(str(row["attempt_id"]) for row in ordered),
        finished_at=tuple(row["finished_at"] for row in ordered),
    )


def _from_arrow(table: pa.Table) -> Table:
    metadata = table.schema.metadata
    versions = (
        _version(metadata, DATASET_METADATA_KEY),
        _version(metadata, FEATURE_METADATA_KEY),
    )
    if versions not in KNOWN_VERSIONS:
        hint = (
            "; feature_version 1 datasets are not read since D366 (a Mode 3 "
            "set's lane features changed): rebuild it with `compute planner ml "
            "dataset --out FILE`"
            if versions[1] == 1
            else ""
        )
        raise DatasetVersionError(
            f"dataset_version {versions[0]} / feature_version {versions[1]} is not "
            f"one this code knows ({sorted(KNOWN_VERSIONS)}){hint}"
        )
    if tuple(table.column_names) != COLUMN_NAMES:
        raise DatasetVersionError(
            "the file's columns are not the published dataset columns"
        )
    return table_from_rows(table.to_pylist())


def load(path: Path) -> Table:
    """The dataset in ``path`` (module docstring); :class:`DatasetVersionError`
    for an unknown version or column list, :class:`DatasetValueError` for
    values G1 never writes, ``OSError`` /
    ``pyarrow.ArrowInvalid`` for an unreadable file."""
    return _from_arrow(pq.read_table(path))


def load_bytes(data: bytes) -> Table:
    """:func:`load` of in-memory Parquet bytes (tests)."""
    return _from_arrow(pq.read_table(pa.BufferReader(data)))
