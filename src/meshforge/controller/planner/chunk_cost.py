"""Per-node fixed chunk cost and first-chunk cost from chunk history (V1.0 G11,
D456). Pure: no database, no clock.

Lab round 10 measured, per chunk, the controller's ``granted -> committed``
time: a Windows venv lane pays a **fixed** cost on every chunk (L4 ~20 s, L2
~10 s, L3 ~11 s; Linux Docker ~0) and **more on its first chunk** (+12-16 s).
One shared ``chunk_overhead`` per job cannot see that, so this module learns
both per node from the same rows the per-node item rate comes from (the
fingerprint's latest committed ``chunk_record`` rows on that node, D438 /
``[controller.planner] history_runs``).

**One sample** = one committed chunk on the node: ``items``, ``wall_s`` (its
lease's ``ended_at - leased_at``, both the controller's clock: what the lane
really spent, grant to commit), the agent-measured ``stage_s`` and ``env_s``,
and ``first`` (the lane's earliest lease - its first chunk). Given the node's
seconds per item ``s`` (the planner's own rate, ``1 / median(items / run
seconds)``), each sample's **residual** is::

    r = wall_s - stage_s - items x s

``stage_s`` is taken out because it is where a chunk fetches its items, which
the planner already prices per item (``items_fetch``). Then:

* ``fixed_s = max(0, median(r over non-first chunks))`` - needs at least
  :data:`MIN_FIXED_SAMPLES` of them;
* ``first_s = max(0, median(r over first chunks) - median(r over non-first
  chunks))`` - the extra a lane's first chunk costs; needs ``fixed_s`` and at
  least :data:`MIN_FIRST_SAMPLES` first chunks. A first chunk whose ``env_s``
  looks like an environment **build** (more than :data:`BUILD_ENV_FACTOR` x
  the steady chunks' median env check + :data:`BUILD_ENV_FLOOR_S`) is not a
  first-chunk sample: the planner prices a build with its ``env`` term.

Medians keep one stalled or retried chunk from moving either number; a node
with too few samples gets no entry and the planner falls back to the shared
``chunk_overhead`` (history, then ``[controller.split] chunk_overhead_s``).

**What it cannot separate.** ``s`` comes from the chunk process's run time,
which on a venv lane includes the interpreter start and the script's imports;
that part of the per-chunk cost is therefore inside ``s``, not ``fixed_s``.
At the chunk size the history was measured with, ``fixed_s + items x s`` is
the measured chunk time either way; at a very different size the split
between the two terms is approximate (ADR-0024's per-chunk ``spawn_s`` would
remove that).

**Node history (V1.0 G12, D459).** A new workload has no chunk on any node,
so every lane fell back to the shared overhead (lab v0.17.3: a 24-item split
predicted 38 s, ran 52 s). The fixed and first-chunk costs are mostly the
node's and its runtime's (interpreter start, venv activation, Docker exec),
not the workload's, so :func:`pooled_chunk_cost` learns them from the node's
latest committed chunks of **any** workload that ran on the **same runtime
kind** the job would use there (:func:`expected_runtime`). Same estimator,
same minimums; each chunk's residual uses its **own** workload's seconds per
item on that node (the median ``items / run seconds`` of that workload's
chunks among the samples), so a slow workload's items are not read as fixed
cost. The planner's order per node: the fingerprint's own costs, then this
node history, then the shared ``chunk_overhead`` (history, else assumed).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final

from meshforge.common.capabilities import (
    EXEC_DOCKER_CAPABILITY,
    EXEC_SUBPROCESS_CAPABILITY,
    EXEC_TORCH_CAPABILITY,
    TORCH_BASES,
)
from meshforge.controller.planner.feasibility import EXEC_VENV_CAPABILITY
from meshforge.controller.planner.types import History, NodeSnapshot

__all__ = [
    "BUILD_ENV_FACTOR",
    "BUILD_ENV_FLOOR_S",
    "MIN_FIRST_SAMPLES",
    "MIN_FIXED_SAMPLES",
    "RUNTIME_KINDS",
    "ChunkSample",
    "NodeChunkCost",
    "PooledSample",
    "chunk_costs",
    "expected_runtime",
    "node_chunk_cost",
    "pooled_chunk_cost",
    "with_node_history",
]

MIN_FIXED_SAMPLES: Final = 3
"""Non-first committed chunks a node needs before its own ``fixed_s`` is used."""
MIN_FIRST_SAMPLES: Final = 1
"""First chunks a node needs before its own ``first_s`` is used (a lane has
one first chunk, so this is the number of lanes the node ran)."""
BUILD_ENV_FACTOR: Final = 4.0
BUILD_ENV_FLOOR_S: Final = 1.0
"""A first chunk with ``env_s > BUILD_ENV_FACTOR x steady env +
BUILD_ENV_FLOOR_S`` built its environment: not a first-chunk sample."""
_MAX_S: Final = 1e9
_ROUND: Final = 6


@dataclass(frozen=True, slots=True)
class ChunkSample:
    """One committed chunk on one node (module docstring)."""

    items: int
    wall_s: float
    """Grant -> commit, controller clock."""
    stage_s: float = 0.0
    env_s: float = 0.0
    first: bool = False
    """The lane's first lease."""


@dataclass(frozen=True, slots=True)
class NodeChunkCost:
    """A node's learned per-chunk costs."""

    fixed_s: float
    fixed_samples: int
    first_s: float | None = None
    """None = too few usable first chunks."""
    first_samples: int = 0


def _finite(value: float) -> bool:
    return math.isfinite(value) and abs(value) < _MAX_S


def _usable(sample: ChunkSample) -> bool:
    return (
        sample.items >= 1
        and _finite(sample.wall_s)
        and sample.wall_s >= 0.0
        and _finite(sample.stage_s)
        and _finite(sample.env_s)
    )


def _residual(sample: ChunkSample, seconds_per_item: float) -> float:
    return sample.wall_s - max(0.0, sample.stage_s) - sample.items * seconds_per_item


def _median(values: Sequence[float]) -> float:
    """The median (mean of the two middle values for an even count)."""
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _valid_spi(seconds_per_item: float) -> bool:
    return _finite(seconds_per_item) and seconds_per_item > 0.0


def _cost(pairs: Sequence[tuple[ChunkSample, float]]) -> NodeChunkCost | None:
    """The estimator over ``(sample, its seconds per item)`` pairs (module
    docstring): one rate for a fingerprint's chunks, each chunk's own
    workload's rate for node history."""
    usable = [(s, spi) for s, spi in pairs if _usable(s) and _valid_spi(spi)]
    steady = [(s, spi) for s, spi in usable if not s.first]
    if len(steady) < MIN_FIXED_SAMPLES:
        return None
    base = _median([_residual(s, spi) for s, spi in steady])
    fixed = round(min(_MAX_S, max(0.0, base)), _ROUND)
    env_steady = _median([max(0.0, s.env_s) for s, _ in steady])
    build_above = BUILD_ENV_FACTOR * env_steady + BUILD_ENV_FLOOR_S
    firsts = [(s, spi) for s, spi in usable if s.first and s.env_s <= build_above]
    if len(firsts) < MIN_FIRST_SAMPLES:
        return NodeChunkCost(fixed, len(steady))
    extra = _median([_residual(s, spi) for s, spi in firsts]) - base
    first = round(min(_MAX_S, max(0.0, extra)), _ROUND)
    return NodeChunkCost(fixed, len(steady), first, len(firsts))


def node_chunk_cost(
    samples: Iterable[ChunkSample], seconds_per_item: float
) -> NodeChunkCost | None:
    """The node's ``fixed_s`` / ``first_s`` (module docstring), None when it
    has fewer than :data:`MIN_FIXED_SAMPLES` usable non-first chunks or no
    usable rate."""
    if not _valid_spi(seconds_per_item):
        return None
    return _cost([(s, seconds_per_item) for s in samples])


def chunk_costs(
    samples: Mapping[str, Sequence[ChunkSample]],
    seconds_per_item: Mapping[str, float],
) -> dict[str, NodeChunkCost]:
    """:func:`node_chunk_cost` for every node with a rate, in node-id order."""
    out: dict[str, NodeChunkCost] = {}
    for node_id in sorted(samples):
        spi = seconds_per_item.get(node_id)
        if spi is None:
            continue
        cost = node_chunk_cost(samples[node_id], spi)
        if cost is not None:
            out[node_id] = cost
    return out


# --- V1.0 G12 (D459): node history ----------------------------------------------

RUNTIME_KINDS: Final = ("docker", "venv", "subprocess")
"""The runtime kinds an attempt records (``attempt.runtime``)."""
_VENV_BASES: Final = frozenset({"python-cpu"})
"""Bases the venv runtime provides (``selection.py`` ``VENV_BASES``)."""


@dataclass(frozen=True, slots=True)
class PooledSample:
    """One committed chunk of any workload on a node (node history)."""

    workload: str
    """The chunk's job fingerprint (its job id when it has none)."""
    sample: ChunkSample
    items_per_s: float
    """This chunk's ``items / run seconds`` (the agent's chunk process)."""


def pooled_chunk_cost(samples: Iterable[PooledSample]) -> NodeChunkCost | None:
    """A node's ``fixed_s`` / ``first_s`` from chunks of several workloads
    (module docstring, "Node history"): each workload's seconds per item is
    ``1 / median(items_per_s)`` of its own chunks here; a workload without a
    usable rate contributes nothing. None = too few samples (the same
    :data:`MIN_FIXED_SAMPLES`)."""
    rows = list(samples)
    rates: dict[str, list[float]] = {}
    for row in rows:
        if _finite(row.items_per_s) and row.items_per_s > 0.0:
            rates.setdefault(row.workload, []).append(row.items_per_s)
    spi = {w: 1.0 / _median(r) for w, r in sorted(rates.items())}
    return _cost([(r.sample, spi[r.workload]) for r in rows if r.workload in spi])


def expected_runtime(capabilities: Iterable[str], base: str) -> str | None:
    """The runtime kind a node's agent would run the job with - its
    preference order (``agent/execution/selection.py`` ``choose_runtime``:
    docker > venv > subprocess) over the runtimes its capability tags offer
    that can provide ``base``; None = none fits.

    Approximate where the tags cannot tell: they do not say which runtime
    passes a GPU through, so a GPU job on a node offering Docker is assumed
    to use Docker. Used only to pick which node history describes the job
    (D459), never for feasibility."""
    tags = frozenset(capabilities)
    if EXEC_DOCKER_CAPABILITY in tags:
        return "docker"
    if EXEC_VENV_CAPABILITY in tags and base in _VENV_BASES:
        return "venv"
    if EXEC_SUBPROCESS_CAPABILITY in tags and (
        base not in TORCH_BASES or EXEC_TORCH_CAPABILITY in tags
    ):
        return "subprocess"
    return None


def with_node_history(
    history: History,
    costs: Mapping[tuple[str, str], NodeChunkCost],
    nodes: Iterable[NodeSnapshot],
    base: str,
) -> History:
    """``history`` plus the node-history costs (D459) of every node in
    ``nodes`` that has no cost learned from the fingerprint itself: the costs
    learned on the runtime kind the job would use there
    (:func:`expected_runtime`). Unchanged when no node gets one."""
    fixed: dict[str, float] = {}
    fixed_n: dict[str, int] = {}
    first: dict[str, float] = {}
    first_n: dict[str, int] = {}
    runtime_of: dict[str, str] = {}
    for node in sorted(nodes, key=lambda n: n.node_id):
        if node.node_id in history.chunk_fixed_s_by_node:
            continue
        runtime = expected_runtime(node.capabilities, base)
        cost = None if runtime is None else costs.get((node.node_id, runtime))
        if runtime is None or cost is None:
            continue
        fixed[node.node_id] = cost.fixed_s
        fixed_n[node.node_id] = cost.fixed_samples
        runtime_of[node.node_id] = runtime
        if cost.first_s is not None:
            first[node.node_id] = cost.first_s
            first_n[node.node_id] = cost.first_samples
    if not fixed:
        return history
    return replace(
        history,
        node_chunk_fixed_s_by_node=fixed,
        node_chunk_fixed_samples_by_node=fixed_n,
        node_chunk_first_s_by_node=first,
        node_chunk_first_samples_by_node=first_n,
        node_chunk_runtime_by_node=runtime_of,
    )
