"""Two planner inputs read from what the fabric already did (V1.0 G12).

* :func:`node_chunk_history` (D459) - per (node, runtime kind) the learned
  fixed chunk cost and first-chunk cost from the node's latest committed
  chunks of **any** workload (:func:`~meshforge.controller.planner.chunk_cost.
  pooled_chunk_cost`). The dispatcher hands it to a split job whose own
  fingerprint has no cost learned on a node (a new workload), for the
  runtime kind that job would use there.
* :func:`previous_modes` (D460) - per fingerprint the mode (``local`` /
  ``offload`` / ``split``) the latest dispatched decision of an unpinned job
  chose: the planner's mode hysteresis keeps it on a near tie.

Both reads are bounded (row caps, a window of the latest chunk records) and
run only when the tick's batch needs them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import and_, func, literal, literal_column, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnClause

from meshforge.controller.planner.chunk_cost import (
    RUNTIME_KINDS,
    ChunkSample,
    NodeChunkCost,
    PooledSample,
    pooled_chunk_cost,
)
from meshforge.controller.store.models.job import Attempt, Job
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.controller.store.models.telemetry import ChunkRecord

__all__ = [
    "MAX_SCAN_CHUNKS",
    "SCAN_CHUNKS_PER_NODE_RUN",
    "node_chunk_history",
    "previous_modes",
]

SCAN_CHUNKS_PER_NODE_RUN: Final = 16
"""The latest-chunks window :func:`node_chunk_history` ranks in: this many
chunk records per known node and per ``history_runs`` (most recent first, by
the ``recorded_at`` index) - room for every node's latest ``runs`` steady and
``runs`` first chunks even when one busy node wrote most of the recent ones."""
MAX_SCAN_CHUNKS: Final = 50_000
"""Hard cap on that window, whatever the node count."""
_PREVIOUS_PER_FINGERPRINT: Final = 8
"""Latest dispatched decisions read per fingerprint to find an unpinned one."""
_MODES: Final = frozenset({"local", "offload", "split"})
_RANK_ONE_MODE: Final[ColumnClause[Any]] = literal_column(
    "'$[*] ? (@.rank == 1).mode'::jsonpath"
)

NodeHistory = dict[tuple[str, str], NodeChunkCost]
"""``(node_id, runtime kind) -> NodeChunkCost``."""


async def node_chunk_history(
    db: AsyncSession, node_ids: Sequence[str], runs: int
) -> NodeHistory:
    """D459: per (node, runtime kind), :func:`pooled_chunk_cost` over the
    node's latest ``runs`` committed chunks on that runtime plus up to ``runs``
    latest first chunks of a lane (the D456 sample rules: wall = ``ended_at -
    leased_at``, controller clock; a lane's first chunk = no earlier lease in
    its lane). The runtime kind is the lane attempt's ``attempt.runtime``; a
    chunk whose lane attempt is gone or has none is skipped."""
    if not node_ids:
        return {}
    runs = max(1, runs)
    window = min(MAX_SCAN_CHUNKS, SCAN_CHUNKS_PER_NODE_RUN * runs * len(node_ids))
    recent = (
        select(
            ChunkRecord.job_id,
            ChunkRecord.chunk_index,
            ChunkRecord.lease_seq,
            ChunkRecord.lane_attempt_id,
            ChunkRecord.node_id,
            ChunkRecord.items.label("chunk_items"),
            ChunkRecord.run_ms,
            ChunkRecord.stage_ms,
            ChunkRecord.env_ms,
            ChunkRecord.leased_at,
            ChunkRecord.ended_at,
        )
        .where(
            ChunkRecord.outcome == "committed",
            ChunkRecord.run_ms.is_not(None),
            ChunkRecord.run_ms > 0,
            ChunkRecord.ended_at.is_not(None),
            ChunkRecord.node_id.in_(sorted(node_ids)),
        )
        .order_by(ChunkRecord.recorded_at.desc())
        .limit(window)
        .subquery()
    )
    older = aliased(ChunkRecord)
    earlier = (
        select(literal(1))
        .where(
            older.job_id == recent.c.job_id,  # the primary key's prefix
            older.lane_attempt_id == recent.c.lane_attempt_id,
            older.leased_at < recent.c.leased_at,
        )
        .exists()
    )
    joined = (
        select(
            recent.c.job_id,
            recent.c.chunk_index,
            recent.c.lease_seq,
            recent.c.node_id,
            recent.c.chunk_items,
            recent.c.run_ms,
            recent.c.stage_ms,
            recent.c.env_ms,
            recent.c.ended_at,
            Attempt.runtime.label("runtime"),
            func.coalesce(Job.fingerprint, recent.c.job_id).label("workload"),
            func.extract("epoch", recent.c.ended_at - recent.c.leased_at).label("wall"),
            (~earlier).label("first"),
        )
        .join(Attempt, Attempt.id == recent.c.lane_attempt_id)
        .join(Job, Job.id == recent.c.job_id)
        .where(Attempt.runtime.in_(RUNTIME_KINDS))
        .subquery()
    )
    order = (
        joined.c.ended_at.desc(),
        joined.c.job_id,
        joined.c.chunk_index,
        joined.c.lease_seq,
    )
    part = (joined.c.node_id, joined.c.runtime)
    ranked = select(
        joined,
        func.row_number().over(partition_by=part, order_by=order).label("n"),
        func.row_number()
        .over(partition_by=(*part, joined.c.first), order_by=order)
        .label("nf"),
    ).subquery()
    rows = await db.execute(
        select(
            ranked.c.node_id,
            ranked.c.runtime,
            ranked.c.workload,
            ranked.c.chunk_items,
            ranked.c.run_ms,
            ranked.c.stage_ms,
            ranked.c.env_ms,
            ranked.c.wall,
            ranked.c.first,
        )
        .where(
            or_(
                ranked.c.n <= runs,
                and_(ranked.c.first.is_(True), ranked.c.nf <= runs),
            )
        )
        .order_by(ranked.c.node_id, ranked.c.runtime, ranked.c.n)
        .limit(2 * runs * len(node_ids) * len(RUNTIME_KINDS))
    )
    grouped: dict[tuple[str, str], list[PooledSample]] = {}
    for node_id, runtime, workload, items, run_ms, stage, env, wall, first in rows:
        if wall is None:
            continue
        grouped.setdefault((str(node_id), str(runtime)), []).append(
            PooledSample(
                workload=str(workload),
                sample=ChunkSample(
                    items=int(items),
                    wall_s=float(wall),
                    stage_s=int(stage or 0) / 1000.0,
                    env_s=int(env or 0) / 1000.0,
                    first=bool(first),
                ),
                items_per_s=int(items) / (int(run_ms) / 1000.0),
            )
        )
    out: NodeHistory = {}
    for key in sorted(grouped):
        cost = pooled_chunk_cost(grouped[key])
        if cost is not None:
            out[key] = cost
    return out


def _unconstrained(placement: Mapping[str, Any] | None) -> bool:
    """The job let the planner choose: no pinned node (``--node`` /
    ``--local``), no forced set, no excluded node."""
    if not isinstance(placement, Mapping):
        return True
    return not (
        placement.get("node_id")
        or placement.get("node_set")
        or placement.get("exclude_nodes")
    )


async def previous_modes(
    db: AsyncSession, fingerprints: Sequence[str], *, limit: int
) -> dict[str, str]:
    """D460: fingerprint -> the stored mode of the rank-1 row of the latest
    ``dispatched`` decision of an unconstrained job (:func:`_unconstrained`)
    with that fingerprint; a fingerprint with none is absent. Reads the latest
    :data:`_PREVIOUS_PER_FINGERPRINT` dispatched decisions per fingerprint (at
    most ``limit`` fingerprints)."""
    wanted = sorted(set(fingerprints))[:limit]
    if not wanted:
        return {}
    ranked = (
        select(
            Job.fingerprint.label("fp"),
            Job.placement.label("placement"),
            func.jsonb_path_query_first(
                PlanDecision.candidates, _RANK_ONE_MODE, type_=JSONB
            ).label("mode"),
            func.row_number()
            .over(partition_by=Job.fingerprint, order_by=PlanDecision.id.desc())
            .label("n"),
            PlanDecision.id.label("id"),
        )
        .join(Job, Job.id == PlanDecision.job_id)
        .where(Job.fingerprint.in_(wanted), PlanDecision.outcome == "dispatched")
        .subquery()
    )
    rows = await db.execute(
        select(ranked.c.fp, ranked.c.placement, ranked.c.mode)
        .where(ranked.c.n <= _PREVIOUS_PER_FINGERPRINT)
        .order_by(ranked.c.fp, ranked.c.id.desc())
        .limit(len(wanted) * _PREVIOUS_PER_FINGERPRINT)
    )
    out: dict[str, str] = {}
    for fp, placement, mode in rows:
        key = str(fp)
        if key in out or not _unconstrained(placement):
            continue
        if isinstance(mode, str) and mode in _MODES:
            out[key] = mode
        else:
            out[key] = ""  # the latest unconstrained decision had no rank-1 row
    return {k: v for k, v in out.items() if v}
