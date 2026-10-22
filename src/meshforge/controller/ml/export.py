"""The ML dataset as a service: build, summarise, write (V0.15 G1, D333). I/O.

* :func:`build_dataset` - :func:`~.loader.iter_runs` page by page into a
  :class:`~.dataset.Builder` (each page's pure work in a worker thread, so a
  large build never stalls the event loop), then sorted, encoded, digested.
  The API route (``GET /v1/planner/ml/dataset``) and ``compute planner ml
  dataset`` (through that route) both use it, so they return the same bytes.
* :func:`summary` - the JSON **sidecar**: versions, digest, counts (rows per
  term and family, skips by reason), the read cap, and the schema (columns,
  features with unit and group, terms with what measures them). Deterministic:
  no build time in it, so the same data gives the same sidecar.
* :func:`write_files` - ``PATH`` (Parquet) and ``PATH.json`` (sidecar).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.logging import get_logger
from meshforge.controller.ml.dataset import COLUMNS, Builder, Dataset
from meshforge.controller.ml.loader import DEFAULT_MAX_RUNS, iter_runs
from meshforge.controller.ml.schema import (
    DATASET_VERSION,
    FEATURE_VERSION,
    FEATURES,
    TERMS,
    schema_fingerprint,
)

__all__ = ["BuiltDataset", "build_dataset", "summary", "summary_json", "write_files"]

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BuiltDataset:
    dataset: Dataset
    max_runs: int
    truncated: bool
    """The cap stopped the read while older runs were left."""


async def build_dataset(
    engine: AsyncEngine, *, max_runs: int = DEFAULT_MAX_RUNS
) -> BuiltDataset:
    """The dataset of the newest ``max_runs`` matched runs (module docstring).
    :class:`~.loader.MlLoadError` when the database fails."""
    builder = Builder()
    truncated = False
    async for page in iter_runs(engine, max_runs=max_runs):
        await asyncio.to_thread(builder.add, page.runs)
        truncated = truncated or page.truncated
    dataset = await asyncio.to_thread(builder.finish)
    log.info(
        "controller.ml.dataset_built",
        rows=dataset.counts["rows"],
        runs=dataset.counts["runs"],
        digest=dataset.digest,
        truncated=truncated,
    )
    return BuiltDataset(dataset=dataset, max_runs=max_runs, truncated=truncated)


def summary(built: BuiltDataset) -> dict[str, Any]:
    """The sidecar (module docstring)."""
    dataset = built.dataset
    by_feature = {f.name: f for f in FEATURES}
    return {
        "dataset_version": DATASET_VERSION,
        "feature_version": FEATURE_VERSION,
        "digest": dataset.digest,
        "bytes": len(dataset.parquet),
        "rows": dataset.counts["rows"],
        "max_runs": built.max_runs,
        "truncated": built.truncated,
        "counts": dict(dataset.counts),
        "schema": {
            "fingerprint": schema_fingerprint(),
            "columns": [
                {
                    "name": name,
                    "type": str(kind),
                    "nullable": nullable,
                    "group": by_feature[name].group.value
                    if name in by_feature
                    else None,
                    "unit": by_feature[name].unit if name in by_feature else None,
                }
                for name, kind, nullable in COLUMNS
            ],
            "features": [
                {
                    "name": f.name,
                    "kind": f.kind,
                    "unit": f.unit,
                    "group": f.group.value,
                    "doc": f.doc,
                }
                for f in FEATURES
            ],
            "terms": [
                {
                    "name": t.name,
                    "scope": t.scope.value,
                    "planner_term": t.planner_term,
                    "phase": t.phase,
                    "actual": t.actual,
                }
                for t in TERMS
            ],
        },
    }


def summary_json(built: BuiltDataset) -> str:
    """:func:`summary` as canonical text (sorted keys, 2-space indent)."""
    return json.dumps(summary(built), sort_keys=True, indent=2, allow_nan=False) + "\n"


def write_files(built: BuiltDataset, out: Path) -> tuple[Path, Path]:
    """``out`` (Parquet) and ``out`` + ``.json`` (sidecar); both created anew
    (an existing file raises :class:`FileExistsError`)."""
    sidecar = out.with_name(out.name + ".json")
    with out.open("xb") as fh:
        fh.write(built.dataset.parquet)
    with sidecar.open("x", encoding="utf-8") as fh:
        fh.write(summary_json(built))
    return out, sidecar
