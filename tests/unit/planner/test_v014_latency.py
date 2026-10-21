"""V0.14 criterion 3 - decision latency (D294; G1 scaffold, G3 benchmark).

What one dispatch decision costs the dispatcher per job: cut the snapshot to
the job (``project``), ``plan()`` it, build its replay (``to_replay`` +
canonical JSON, the bounded-size check). Synthetic fabrics of 4 and 16
online nodes with history, a full fresh network matrix, **every node's
reliability** (the risk term) and the dispatcher's G3 configuration (the
``link_infeasible`` floor on, dominance pruning on); a whole job and a split
job (2000 items: lanes, prefix sets, chunk sizing); each objective.

Asserts criterion 3: p99 < 100 ms at 4 nodes (``[controller.planner]
decision_latency_budget_ms`` default) and < 500 ms at 16. Prints p50 / p99
per case (run with ``-s``). Marked ``perf``: a bare run skips it;
``pytest -m perf tests/unit/planner/test_v014_latency.py -s`` runs it.

Calibration (G2) multiplies terms by stored factors - O(terms) per candidate;
the merge adds a calibration to these inputs.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import random
import time

import pytest
from mf_planner_fabrics import synthetic_fabric

from meshforge.common.manifest import OBJECTIVES
from meshforge.controller.planner import plan
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


def _dispatcher_inputs(
    nodes: int, *, split: bool, objective: str
) -> tuple[PlanRequest, ClusterSnapshot, dt.datetime]:
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
    return dataclasses.replace(request, objective=objective), snapshot, now  # type: ignore[arg-type]


def _decide_ms(nodes: int, *, split: bool, objective: str) -> list[float]:
    request, snapshot, now = _dispatcher_inputs(nodes, split=split, objective=objective)
    samples: list[float] = []
    for _ in range(RUNS):
        started = time.perf_counter()
        view = project(request, snapshot)
        decision = plan(request, view, "cost_model", now)
        canonical_json(to_replay(request, view, now))
        samples.append((time.perf_counter() - started) * 1000.0)
        assert decision.chosen is not None
    return sorted(samples)


def _pct(samples: list[float], q: float) -> float:
    return samples[min(len(samples) - 1, int(q * len(samples)))]


@pytest.mark.parametrize("objective", OBJECTIVES)
@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
@pytest.mark.parametrize("nodes", [4, 16])
def test_decision_latency(nodes: int, split: bool, objective: str) -> None:
    samples = _decide_ms(nodes, split=split, objective=objective)
    p50, p99 = _pct(samples, 0.50), _pct(samples, 0.99)
    print(  # noqa: T201 - the measurement, recorded in the progress note
        f"\nV0.14 decision latency nodes={nodes} "
        f"job={'split' if split else 'whole'} objective={objective} runs={RUNS} "
        f"p50={p50:.2f}ms p99={p99:.2f}ms max={samples[-1]:.2f}ms"
    )
    budget = BUDGET_MS[nodes]
    assert p99 < budget, f"p99 {p99:.1f} ms over the {nodes}-node budget {budget} ms"
