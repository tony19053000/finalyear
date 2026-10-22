"""V0.15 G4 (D365, V0.14 criterion 3 kept as V0.15 criterion 4): decision
latency **with ML on**.

The V0.14 harness (``test_v014_latency.py``: what one dispatch decision
costs - cut the snapshot to the job, ``plan()``, build the replay - on a
4-node synthetic fabric with history, a full fresh network matrix, every
node's reliability, the dispatcher's G3 configuration; a whole job and a
2000-item split job; each objective) with the **worst realistic model**
plugged in through the production path (``ml_input``: the real feature
builder + the 20 ms predict budget):

* a model for **every** learnable term (8), each a LightGBM booster of
  **400 trees** (``num_leaves`` 15, the largest grid point) + the D338 OOD
  test with a kNN reference set of **20 000 rows** (brute force, k = 5) and
  the range test;
* the conformal interval narrow and the OOD threshold above every distance,
  so every queried term is really corrected (an OOD or wide answer would be
  cheaper - it is not the worst case).

Asserts: p99 < 100 ms at 4 nodes (and < 500 ms at 16), **no** decision fell
back (a ``budget_exceeded`` fallback would make a slow model look fast), and
prints p50 / p99 of the decision and of the slowest predict call. Marked
``perf`` (``pytest -m perf tests/unit/planner/test_v015_g4_latency.py -s
--run-perf``); numbers recorded in the progress note.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import math
import random
import time
from typing import Any

import pytest

pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("sklearn", reason="needs the ml extra")

import numpy as np
from mf_planner_fabrics import synthetic_fabric
from sklearn.neighbors import NearestNeighbors

from meshforge.common.manifest import OBJECTIVES
from meshforge.controller.ml.predictor import BudgetedPredictor, ml_input
from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.ml.train.algorithms import (
    Config,
    FitContext,
    fit_one,
)
from meshforge.controller.ml.train.data import feature_indices, table_from_rows
from meshforge.controller.ml.train.memory import InMemoryPredictor, TermModel
from meshforge.controller.ml.train.ood import RANGE_GROUPS, OodModel
from meshforge.controller.ml.train.prep import fit_prep, transform
from meshforge.controller.planner import plan
from meshforge.controller.planner.predictor import ML_TERMS
from meshforge.controller.planner.replay import canonical_json, project, to_replay
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    PlanRequest,
    Reliability,
    RiskConfig,
)

pytestmark = [pytest.mark.unit, pytest.mark.perf]

RUNS = 200
BUDGET_MS = {4: 100.0, 16: 500.0}
OOD_REFS = 20_000
TREES = 400
TRAIN_ROWS = 4_000


def _rows(n: int, seed: int) -> list[dict[str, Any]]:
    """Training rows spanning the synthetic fabrics' feature ranges (so the
    range test passes and every term is really corrected)."""
    rng = random.Random(seed)  # noqa: S311 - test data
    base = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    rows = []
    for i in range(n):
        values = {name: rng.uniform(0.0, 1e12) for name in NUMERIC_FEATURES}
        values.update(
            hw_cpu_gflops=rng.uniform(1.0, 2000.0),
            load_cpu_percent=rng.uniform(0.0, 100.0),
            an_raw_s=rng.uniform(0.01, 1e4),
        )
        y = 0.3 * math.sin(values["load_cpu_percent"] / 10.0) + rng.gauss(0.0, 0.05)
        rows.append(
            {
                "decision_id": i,
                "attempt_id": f"a{i}",
                "job_id": f"j{i}",
                "node_id": f"n{i % 7}",
                "term": "compute",
                "group": f"g{i % 50}",
                "family": f"f{i % 5}",
                "finished_at": base + dt.timedelta(seconds=i),
                "source": "history",
                **values,
                "y": y,
                "raw_s": 10.0,
                "actual_s": 10.0 * math.exp(y),
            }
        )
    return rows


def _worst_model() -> InMemoryPredictor:
    table = table_from_rows(_rows(TRAIN_ROWS, seed=1))
    ctx = FitContext(table=table, seed=0)
    config = Config("lightgbm", (("n_estimators", TREES), ("num_leaves", 15)), index=3)
    booster = fit_one(config, ctx, np.arange(table.n, dtype=np.intp))
    refs = np.asarray(
        [[r[name] for name in NUMERIC_FEATURES] for r in _rows(OOD_REFS, seed=2)],
        dtype=np.float64,
    )
    columns = feature_indices(*RANGE_GROUPS)
    prep = fit_prep(refs)
    neighbours = NearestNeighbors(n_neighbors=5, algorithm="brute", n_jobs=1).fit(
        transform(prep, refs, standardise=True)
    )
    ood = OodModel(
        columns=columns,
        low=np.full(len(columns), -np.inf),
        high=np.full(len(columns), np.inf),
        prep=prep,
        neighbours=neighbours,
        threshold=math.inf,  # never OOD: the worst case runs every model
        k=5,
    )
    model = TermModel(term="any", model=booster, q=0.01, ood=ood, config=config.name)
    return InMemoryPredictor(
        version="mem-latency", models=dict.fromkeys(ML_TERMS, model)
    )


@pytest.fixture(scope="module")
def worst() -> InMemoryPredictor:
    return _worst_model()


def _dispatcher_inputs(
    nodes: int, *, split: bool, objective: str
) -> tuple[PlanRequest, ClusterSnapshot, dt.datetime]:
    """The V0.14 harness's inputs (``test_v014_latency._dispatcher_inputs``)."""
    request, snapshot, now = synthetic_fabric(nodes, split=split, seed=nodes)
    rng = random.Random(nodes)  # noqa: S311 - test data
    snapshot = dataclasses.replace(
        snapshot,
        nodes=tuple(
            dataclasses.replace(
                n,
                reliability=Reliability(
                    failures=rng.randint(0, 3),
                    total=rng.randint(3, 60),
                    window_days=7.0,
                ),
            )
            for n in snapshot.nodes
        ),
        config=dataclasses.replace(
            snapshot.config,
            risk=RiskConfig(prior=(1.0, 19.0), lease_release_s=60.0),
            min_useful_bw_bytes_s=1e6,
            prune_dominated=True,
        ),
    )
    request = dataclasses.replace(
        request,
        objective=objective,  # type: ignore[arg-type]
        # an output size, so the output term is learnable too
        history=dataclasses.replace(
            request.history, output_bytes=5e8, output_samples=3
        ),
    )
    return request, snapshot, now


def _pct(samples: list[float], q: float) -> float:
    return samples[min(len(samples) - 1, int(q * len(samples)))]


@pytest.mark.parametrize("objective", OBJECTIVES)
@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
@pytest.mark.parametrize("nodes", [4, 16])
def test_decision_latency_with_ml_on(
    worst: InMemoryPredictor, nodes: int, split: bool, objective: str
) -> None:
    request, snapshot, now = _dispatcher_inputs(nodes, split=split, objective=objective)
    ml = ml_input(worst, budget_ms=20.0)
    budgeted = ml.predictor
    assert isinstance(budgeted, BudgetedPredictor)
    samples: list[float] = []
    predict_ms: list[float] = []
    calls_before = worst.calls
    for _ in range(RUNS):
        started = time.perf_counter()
        view = project(request, snapshot)
        decision = plan(request, view, "cost_model", now, ml=ml)
        canonical_json(to_replay(request, view, now, ml=decision.ml))
        samples.append((time.perf_counter() - started) * 1000.0)
        assert decision.chosen is not None
        assert decision.ml is not None and decision.ml.error is None, decision.ml
        assert budgeted.last_ms is not None
        predict_ms.append(budgeted.last_ms)
    used = [
        term.detail["ml"]["used"]
        for row in decision.candidates
        if row.estimate is not None
        for term in row.estimate.terms().values()
        if "ml" in term.detail
    ]
    assert any(used), "the model corrected nothing - not the worst case"
    calls = (worst.calls - calls_before) / RUNS
    samples.sort()
    predict_ms.sort()
    p50, p99 = _pct(samples, 0.50), _pct(samples, 0.99)
    print(  # noqa: T201 - the measurement, recorded in the progress note
        f"\nV0.15 G4 decision latency ML on nodes={nodes} "
        f"job={'split' if split else 'whole'} objective={objective} runs={RUNS} "
        f"p50={p50:.2f}ms p99={p99:.2f}ms max={samples[-1]:.2f}ms "
        f"predict_calls/decision={calls:.0f} last_predict_p99="
        f"{_pct(predict_ms, 0.99):.2f}ms corrected_terms={sum(used)}"
    )
    budget = BUDGET_MS[nodes]
    assert p99 < budget, f"p99 {p99:.1f} ms over the {nodes}-node budget {budget} ms"
