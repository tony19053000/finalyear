"""Stage 4: rank the feasible candidates (D107).

A policy maps each feasible candidate to a **sort key** (ascending = better)
and a **score** (the number ``compute explain`` shows next to the rank). Every
key ends with the node id, so equal candidates are ordered the same way every
time (checklist 16).

* ``random`` - a shuffle seeded from ``job_id`` + attempt number: reproducible,
  and a retry draws again.
* ``round_robin`` - the first node id after the last chosen one (the cursor is
  in the dispatcher's memory and passed in the snapshot; a restart resets it).
* ``least_loaded`` - fewest used slots, then lowest live CPU % (unknown last).
* ``strongest_suitable`` - highest hardware score for the slot the job takes
  (GPU score for a GPU slot, CPU score otherwise); unmeasured ranks last.
* ``resource_score`` - weighted free RAM (GiB) + free slots + the score
  normalised to the best feasible one (weights ``[controller.planner]
  resource_weights``); missing numbers count 0.
* ``cache_affinity`` - most bytes of the job's trees already on the node (a
  seeded Local candidate holds the project tree) plus the size of the job's
  built environment when the node still holds it (V0.6 G4: the env hash the
  workload's last attempt there reported, in the cache index); ties ->
  ``least_loaded``.
* ``cost_model`` - lowest estimated completion time (:mod:`.estimator`).
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from meshforge.common.manifest import POLICIES
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    Estimate,
    NodeSnapshot,
    PlanRequest,
    Slot,
)

__all__ = ["POLICIES", "Ranked", "Scored", "rank"]

_GIB: Final = 1024**3
type Key = tuple[float | str, ...]


@dataclass(frozen=True, slots=True)
class Scored:
    """One feasible candidate, as the policies see it."""

    node: NodeSnapshot
    slot: Slot
    local: bool
    estimate: Estimate


@dataclass(frozen=True, slots=True)
class Ranked:
    node_id: str
    score: float
    rank: int


def _used(node: NodeSnapshot) -> int:
    return node.used_cpu_slots + node.used_gpu_slots


def _load_key(item: Scored) -> Key:
    cpu = item.node.cpu_percent
    return (float(_used(item.node)), 101.0 if cpu is None else cpu)


def _hardware_score(item: Scored) -> float | None:
    node = item.node
    return node.gpu_score if item.slot == "gpu" else node.cpu_score


def _cached_bytes(request: PlanRequest, item: Scored) -> int:
    held = 0
    for index, tree in enumerate(request.trees):
        if (index == 0 and item.local and request.seeded) or (
            tree.key in item.node.cached
        ):
            held += tree.size
    env_hash = request.history.env_hash_by_node.get(item.node.node_id)
    if env_hash is not None:
        held += item.node.cached_envs.get(env_hash, 0)
    return held


def _random(request: PlanRequest, items: Sequence[Scored]) -> list[tuple[Key, float]]:
    seed = hashlib.sha256(f"{request.job_id}:{request.attempt_no}".encode()).digest()
    order = sorted(item.node.node_id for item in items)
    random.Random(int.from_bytes(seed[:8], "big")).shuffle(order)  # noqa: S311 - placement, not security
    position = {node_id: i for i, node_id in enumerate(order)}
    return [
        ((float(position[i.node.node_id]),), float(position[i.node.node_id]))
        for i in items
    ]


def _round_robin(
    cursor: str | None, items: Sequence[Scored]
) -> list[tuple[Key, float]]:
    out: list[tuple[Key, float]] = []
    for item in items:
        after = cursor is None or item.node.node_id > cursor
        out.append(((0.0 if after else 1.0,), 0.0 if after else 1.0))
    return out


def _least_loaded(items: Sequence[Scored]) -> list[tuple[Key, float]]:
    out: list[tuple[Key, float]] = []
    for item in items:
        key = _load_key(item)
        out.append((key, float(key[0]) + float(key[1]) / 1000.0))
    return out


def _strongest(items: Sequence[Scored]) -> list[tuple[Key, float]]:
    out: list[tuple[Key, float]] = []
    for item in items:
        score = _hardware_score(item)
        if score is None:
            out.append(((1.0, 0.0), 0.0))
        else:
            out.append(((0.0, -score), score))
    return out


def _resource_score(
    weights: tuple[float, float, float], items: Sequence[Scored]
) -> list[tuple[Key, float]]:
    best = max((s for i in items if (s := _hardware_score(i))), default=None)
    w_ram, w_slots, w_score = weights
    out: list[tuple[Key, float]] = []
    for item in items:
        node = item.node
        free_ram = (node.ram_free_bytes or 0) / _GIB
        free_slots = node.free_cpu_slots + node.free_gpu_slots
        score = _hardware_score(item)
        normalised = score / best if score and best else 0.0
        total = w_ram * free_ram + w_slots * free_slots + w_score * normalised
        out.append(((-total,), round(total, 6)))
    return out


def _cache_affinity(
    request: PlanRequest, items: Sequence[Scored]
) -> list[tuple[Key, float]]:
    out: list[tuple[Key, float]] = []
    for item in items:
        held = _cached_bytes(request, item)
        out.append(((float(-held), *_load_key(item)), float(held)))
    return out


def _cost_model(items: Sequence[Scored]) -> list[tuple[Key, float]]:
    return [((i.estimate.total_s,), round(i.estimate.total_s, 6)) for i in items]


def _keys(
    policy: str,
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    items: Sequence[Scored],
) -> list[tuple[Key, float]]:
    table: dict[str, Callable[[], list[tuple[Key, float]]]] = {
        "random": lambda: _random(request, items),
        "round_robin": lambda: _round_robin(snapshot.round_robin_after, items),
        "least_loaded": lambda: _least_loaded(items),
        "strongest_suitable": lambda: _strongest(items),
        "resource_score": lambda: _resource_score(
            snapshot.config.resource_weights, items
        ),
        "cache_affinity": lambda: _cache_affinity(request, items),
        "cost_model": lambda: _cost_model(items),
    }
    try:
        return table[policy]()
    except KeyError:
        raise ValueError(f"unknown placement policy {policy!r}") from None


def rank(
    policy: str,
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    items: Sequence[Scored],
) -> list[Ranked]:
    """``items`` best first (rank 1 = chosen)."""
    if policy not in POLICIES:
        raise ValueError(f"unknown placement policy {policy!r}")
    keyed = _keys(policy, request, snapshot, items)
    order = sorted(
        zip(items, keyed, strict=True),
        key=lambda pair: (*pair[1][0], pair[0].node.node_id),
    )
    return [
        Ranked(node_id=item.node.node_id, score=score, rank=index + 1)
        for index, (item, (_, score)) in enumerate(order)
    ]
