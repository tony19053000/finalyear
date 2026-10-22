"""Synthetic-truth ML datasets in the real G1 schema (V0.15 G2 tests).

:func:`synthetic_rows` builds dataset rows (every
:data:`~meshforge.controller.ml.dataset.COLUMN_NAMES` key) whose residual
``y`` follows a **known** rule, so a test can say what the comparison must
find:

* ``"signal"`` (truth i) - ``y = 0.9 x load/100 - 0.5 x ln(gflops/100) +
  N(0, noise)``: half of it is per node (calibration's node factor learns
  that), half is the live load (only a model can) -> a learned model must
  win on held-out families;
* ``"node_noise"`` (truth ii) - ``y = c[node] + N(0, noise)`` with ``c``
  not monotone in any hardware feature: calibration's per-node factor is
  the truth -> no learned model may be ``better``;
* ``"signal"`` with ``far_family`` (truth iii) - like (i), plus one family
  whose jobs are ~1000x larger than any other family's -> held out, it is
  out of distribution.

:func:`parquet_file` writes them with G1's own canonical writer
(:func:`~meshforge.controller.ml.dataset.parquet_bytes`), optionally in a
shuffled row order (a non-canonical file).
"""

from __future__ import annotations

import datetime as dt
import math
import random
from pathlib import Path
from typing import Any

from meshforge.controller.ml.dataset import parquet_bytes, sort_key
from meshforge.controller.ml.schema import NUMERIC_FEATURES

__all__ = ["NODE_GFLOPS", "parquet_file", "synthetic_rows"]

NODE_GFLOPS = (60.0, 90.0, 140.0, 200.0, 320.0, 480.0)
_NODE_CONST = (0.4, -0.3, 0.6, -0.5, 0.2, -0.1)  # not monotone in gflops
_BASE = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def synthetic_rows(
    kind: str,
    *,
    families: int = 6,
    groups_per_family: int = 6,
    runs_per_group: int = 10,
    terms: tuple[str, ...] = ("compute",),
    noise: float = 0.05,
    far_family: bool = False,
    seed: int = 1,
    nan_column: str | None = None,
    constant_y: bool = False,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    rows: list[dict[str, Any]] = []
    decision = 0
    names = [f"fam{f:02d}" for f in range(families)]
    if far_family:
        names.append("far")
    for term in terms:
        for family in names:
            for g in range(groups_per_family):
                group = f"{term}:{family}:{g:02d}"
                size = 10 ** rng.uniform(6.0, 8.0)
                if family == "far":
                    size *= 1000.0
                for _ in range(runs_per_group):
                    decision += 1
                    node = rng.randrange(len(NODE_GFLOPS))
                    gflops = NODE_GFLOPS[node]
                    load = rng.uniform(0.0, 100.0)
                    raw = rng.uniform(5.0, 120.0)
                    if constant_y:
                        y = 0.25
                    elif kind == "signal":
                        y = (
                            0.9 * load / 100.0
                            - 0.5 * math.log(gflops / 100.0)
                            + rng.gauss(0.0, noise)
                        )
                    elif kind == "node_noise":
                        y = _NODE_CONST[node] + rng.gauss(0.0, noise)
                    else:
                        raise ValueError(kind)
                    features = dict.fromkeys(NUMERIC_FEATURES, math.nan)
                    features.update(
                        {
                            "hw_cpu_gflops": gflops,
                            "hw_cpu_threads": 4.0 + 2.0 * node,
                            "hw_ram_bytes": (8.0 + 4.0 * (node % 3)) * 2**30,
                            "hw_gpu_count": 0.0,
                            "load_cpu_percent": load,
                            "load_live_attempts": float(rng.randrange(3)),
                            "job_input_bytes": size,
                            "job_cpus": 1.0,
                            "job_lanes": 1.0,
                            "an_raw_s": raw,
                            "an_src_assumed": 1.0,
                            "an_src_measured": 0.0,
                            "an_src_history": 0.0,
                            "an_src_scaled": 0.0,
                        }
                    )
                    if nan_column is not None:
                        features[nan_column] = math.nan
                    rows.append(
                        {
                            "decision_id": decision,
                            "attempt_id": f"att_{decision:06d}_{term}",
                            "job_id": f"job_{decision:06d}",
                            "node_id": f"node_{node:02d}",
                            "term": term,
                            "group": group,
                            "family": family,
                            "finished_at": _BASE + dt.timedelta(seconds=decision),
                            "source": "assumed",
                            **features,
                            "y": y,
                            "raw_s": raw,
                            "actual_s": raw * math.exp(y),
                        }
                    )
    return rows


def parquet_file(
    rows: list[dict[str, Any]], path: Path, *, shuffle_seed: int | None = None
) -> Path:
    """``rows`` as a G1 Parquet file: canonical order, or shuffled by
    ``shuffle_seed`` (a non-canonical file with the same content)."""
    ordered = sorted(rows, key=sort_key)
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(ordered)  # noqa: S311 - test data
    path.write_bytes(parquet_bytes(ordered))
    return path
