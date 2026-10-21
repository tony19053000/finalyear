"""V0.14 G1 item 6 - decision-latency benchmark scaffold (D294, criterion 3).

What one dispatch decision costs the dispatcher per job: cut the snapshot to
the job (``project``), ``plan()`` it, build its replay (``to_replay`` +
canonical JSON, the bounded-size check). Synthetic fabrics of 4 and 16
online nodes with history and a full fresh network matrix, a whole job and a
split job (2000 items: lanes, prefix sets, chunk sizing).

Prints p50 / p99 per case (run with ``-s``). Asserts only criterion 3's
4-node budget (p99 < 100 ms - D302's ``decision_latency_budget_ms`` default,
a setting G3 adds); the 16-node figure (< 500 ms) is G3's to assert once
dominance pruning (D294) lands. Marked ``perf``: a bare run skips
it; ``pytest -m perf tests/unit/planner/test_v014_latency.py -s`` runs it.
"""

from __future__ import annotations

import time

import pytest
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.planner import plan
from meshforge.controller.planner.replay import canonical_json, project, to_replay

pytestmark = [pytest.mark.unit, pytest.mark.perf]

RUNS = 200
BUDGET_4_NODES_MS = 100.0


def _decide_ms(nodes: int, *, split: bool) -> list[float]:
    request, snapshot, now = synthetic_fabric(nodes, split=split, seed=nodes)
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


@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
@pytest.mark.parametrize("nodes", [4, 16])
def test_decision_latency(nodes: int, split: bool) -> None:
    samples = _decide_ms(nodes, split=split)
    p50, p99 = _pct(samples, 0.50), _pct(samples, 0.99)
    print(  # noqa: T201 - the measurement, recorded in the progress note
        f"\nV0.14 decision latency nodes={nodes} "
        f"job={'split' if split else 'whole'} runs={RUNS} "
        f"p50={p50:.2f}ms p99={p99:.2f}ms max={samples[-1]:.2f}ms"
    )
    if nodes == 4:
        assert p99 < BUDGET_4_NODES_MS, f"p99 {p99:.1f} ms over the 4-node budget"
