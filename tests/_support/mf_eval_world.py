"""V0.14 G4: hand-built benchmark campaigns for the offline evaluator's tests.

A tidy fabric of four online nodes (``node_a`` is every run's origin and the
submitter) whose **predicted** compute time per node is set by the job's
history (``by_node_s``), and campaigns whose **measured** medians are set per
configuration - so a test knows exactly what each policy picks and what the
regret of that pick is.

* :func:`fabric` - ``(request, snapshot)`` for a whole job (or a split job);
* :func:`group` - an :class:`~meshforge.controller.planner_eval.model.EvalGroup`
  with ``local`` on the origin and ``mode1`` on every other node (plus
  ``mode3`` sets when asked), medians per node;
* :func:`records` - the group's records, each with the replay the dispatcher
  stores for a campaign's forced run: the pinned request plus the unpinned
  **shadow** request (D215, ``replay.to_replay(..., shadow=)``).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any

from meshforge.controller.planner.replay import to_replay
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    ControllerLinkStat,
    History,
    LinkStat,
    NodeSnapshot,
    PlanRequest,
    SplitInfo,
    TreeNeed,
)
from meshforge.controller.planner_eval.model import ConfigRef, EvalGroup, EvalRecord

__all__ = [
    "NODES",
    "NOW",
    "ORIGIN",
    "config_id",
    "fabric",
    "group",
    "records",
]

GIB = 1024**3
NOW = dt.datetime(2026, 10, 22, 9, 0, tzinfo=dt.UTC)
NODES = ("node_a", "node_b", "node_c", "node_d")
ORIGIN = "node_a"
TREE = "sha256:" + "11" * 32


def fabric(
    compute_s: Mapping[str, float],
    *,
    fingerprint: str = "f" * 64,
    split_items: int | None = None,
    cpu_score: Mapping[str, float] | None = None,
    cpu_percent: Mapping[str, float] | None = None,
) -> tuple[PlanRequest, ClusterSnapshot]:
    """Four nodes; ``compute_s`` = the history's median run seconds per node
    (what ``cost_model`` predicts the compute term from)."""
    scores = cpu_score or {}
    loads = cpu_percent or {}
    nodes = tuple(
        NodeSnapshot(
            node_id=node_id,
            state="online",
            connected=True,
            capabilities=frozenset({"exec", "exec-subprocess", "exec-split"}),
            ram_bytes=16 * GIB,
            cpu_threads=8,
            cpu_slots=2,
            cpu_percent=loads.get(node_id, 10.0),
            ram_free_bytes=8 * GIB,
            disk_free_bytes=100 * GIB,
            cpu_score=scores.get(node_id, 100.0),
            cached=frozenset({TREE}) if node_id == ORIGIN else frozenset(),
        )
        for node_id in NODES
    )
    links = {
        (src, dst): LinkStat(
            bw_bytes_s=1e8,
            rtt_ms=1.0,
            samples=5,
            measured_at=NOW - dt.timedelta(seconds=30),
        )
        for src in NODES
        for dst in NODES
        if src != dst
    }
    request = PlanRequest(
        job_id="job_" + "0" * 26,
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        trees=(TreeNeed(TREE, 10 * 10**6),),
        submitter_node_id=ORIGIN,
        seeded=True,
        fingerprint=fingerprint,
        history=History(
            by_node_s=dict(compute_s),
            samples_by_node=dict.fromkeys(compute_s, 5),
        ),
        split=None
        if split_items is None
        else SplitInfo(items=split_items, item_bytes=None, items_tree=None),
    )
    snapshot = ClusterSnapshot(
        nodes=nodes,
        links=links,
        controller_links={
            node_id: ControllerLinkStat(bw_bytes_s=1e8, samples=5) for node_id in NODES
        },
    )
    return request, snapshot


def config_id(
    group_id: str, strategy: str, node: str, node_set: Sequence[str] = ()
) -> str:
    tail = "+".join(sorted(node_set)) if node_set else node
    return f"cfg_{group_id}_{strategy}_{tail}"


def group(
    group_id: str,
    medians: Mapping[str, float],
    *,
    campaign_id: str = "camp_1",
    family: str = "cpu.matmul",
    label: str = "best",
    tied: Sequence[str] = (),
    sets: Mapping[tuple[str, ...], float] | None = None,
    unsupported: Sequence[str] = (),
    reason: str | None = None,
) -> EvalGroup:
    """``medians`` = node -> median total ms of the configuration on it
    (``local`` for the origin, ``mode1`` elsewhere; a node left out has no
    configuration). ``sets`` = sorted node set -> median of a ``mode3``
    configuration. ``label`` ``best`` names the lowest median; ``tie`` uses
    ``tied`` (nodes)."""
    configs: list[ConfigRef] = []
    for node, median in medians.items():
        strategy = "local" if node == ORIGIN else "mode1"
        configs.append(
            ConfigRef(
                config_id=config_id(group_id, strategy, node),
                strategy=strategy,
                node_id=node,
                origin_node_id=ORIGIN,
                state="unsupported" if node in unsupported else "measured",
                median_ms=None if node in unsupported else float(median),
                repetitions=0 if node in unsupported else 3,
            )
        )
    for node_set, median in (sets or {}).items():
        ordered = tuple(sorted(node_set))
        configs.append(
            ConfigRef(
                config_id=config_id(group_id, "mode3", ordered[0], ordered),
                strategy="mode3",
                node_id=ordered[0],
                origin_node_id=ORIGIN,
                node_set=ordered,
                median_ms=float(median),
                repetitions=3,
            )
        )
    measured = [c for c in configs if c.state == "measured" and c.median_ms is not None]
    best = (
        min(measured, key=lambda c: (c.median_ms, c.config_id)).config_id
        if label == "best" and measured
        else None
    )
    tied_ids = tuple(
        sorted(
            config_id(group_id, "local" if n == ORIGIN else "mode1", n) for n in tied
        )
    )
    return EvalGroup(
        campaign_id=campaign_id,
        group_id=group_id,
        family=family,
        condition="none",
        label=label,  # type: ignore[arg-type]
        best_config_id=best,
        tied_config_ids=tied_ids if label == "tie" else (),
        configs=tuple(configs),
        reason=reason,
    )


def records(
    the_group: EvalGroup,
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    *,
    reps: int = 3,
    shadow: bool = True,
    replay: Any = "auto",
) -> list[EvalRecord]:
    """One record per (measured configuration, repetition), each with its
    forced run's replay (``shadow=False``: the pinned request alone;
    ``replay=None``: no replay stored)."""
    out: list[EvalRecord] = []
    n = 0
    for config in the_group.configs:
        if config.state != "measured":
            continue
        for rep in range(1, reps + 1):
            n += 1
            job_id = f"job_{the_group.group_id}_{n:04d}"
            mine = dataclasses.replace(request, job_id=job_id)
            pinned = dataclasses.replace(
                mine,
                pinned_node_id=None if config.node_set else config.node_id,
                node_set=config.node_set,
                benchmark=True,
            )
            stored: Any
            if replay == "auto":
                stored = to_replay(
                    pinned, snapshot, NOW, shadow=mine if shadow else None
                )
            else:
                stored = replay
            out.append(
                EvalRecord(
                    campaign_id=the_group.campaign_id,
                    group_id=the_group.group_id,
                    config_id=config.config_id,
                    repetition=rep,
                    attempt_id=f"att_{job_id}",
                    job_id=job_id,
                    fingerprint=request.fingerprint,
                    replay=stored,
                    decision_id=n,
                )
            )
    return out
