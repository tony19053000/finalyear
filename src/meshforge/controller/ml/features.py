"""One candidate's ML features from what the planner saw (V0.15 G1, D334).

:func:`features` reads a decoded replay (``PlanRequest`` + projected
``ClusterSnapshot`` + ``now``, :func:`~meshforge.controller.planner.replay.
from_replay` - replay version 1 and 2 alike) and the analytical term the
planner predicted (:class:`Predicted`), and returns one value per
:data:`~.schema.NUMERIC_FEATURES` name, in that order. G4 calls the same
function with the live request and snapshot, so training and serving compute
features the same way.

**Missing = NaN, never 0** (D334): an input the replay does not hold (a node
that reported no RAM, a link nobody measured, a term without a ``samples``
detail, a candidate node absent from the snapshot) is ``math.nan``.

Pure: no database, no clock, no logging.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.planner.feasibility import missing_bytes
from meshforge.controller.planner.replay import from_replay
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    NodeSnapshot,
    PlanRequest,
    TermSource,
)

__all__ = ["NAN", "Predicted", "features", "features_from_replay"]

NAN: Final = math.nan
_SOURCES: Final = (
    ("an_src_measured", TermSource.MEASURED),
    ("an_src_history", TermSource.HISTORY),
    ("an_src_scaled", TermSource.SCALED),
    ("an_src_assumed", TermSource.ASSUMED),
)


@dataclass(frozen=True, slots=True)
class Predicted:
    """The analytical term as the planner predicted it."""

    raw_s: float
    """Uncalibrated seconds (``chunk_rate``: seconds per item)."""
    source: str | None
    """:class:`~meshforge.controller.planner.types.TermSource` value; None =
    unknown."""
    detail: Mapping[str, Any] = field(default_factory=dict)
    lanes: int = 1
    """Lanes of the chosen plan (1 = a whole job or a one-lane split)."""
    chunk_items: int | None = None
    """Items per chunk of the chosen split plan; None = not a split plan."""


def _num(value: Any) -> float:
    """A finite number as float, else NaN (None, bool, text, inf)."""
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return NAN
    number = float(value)
    return number if math.isfinite(number) else NAN


def _flag(value: bool) -> float:
    return 1.0 if value else 0.0


def _node(snapshot: ClusterSnapshot, node_id: str) -> NodeSnapshot | None:
    for node in snapshot.nodes:
        if node.node_id == node_id:
            return node
    return None


def _input_bytes(request: PlanRequest) -> int:
    seen: set[str] = set()
    total = 0
    for tree in request.trees:
        if tree.key not in seen:
            seen.add(tree.key)
            total += tree.size
    return total


def _hardware(node: NodeSnapshot | None) -> dict[str, float]:
    if node is None:
        return dict.fromkeys(
            (
                "hw_cpu_gflops",
                "hw_gpu_tflops",
                "hw_cpu_threads",
                "hw_ram_bytes",
                "hw_gpu_count",
                "hw_gpu_vram_max_bytes",
            ),
            NAN,
        )
    vram = [v for v in node.gpu_vram_bytes if v > 0]
    return {
        "hw_cpu_gflops": _num(node.cpu_score),
        "hw_gpu_tflops": _num(node.gpu_score),
        "hw_cpu_threads": _num(node.cpu_threads),
        "hw_ram_bytes": _num(node.ram_bytes),
        "hw_gpu_count": float(len(node.gpu_vram_bytes)),
        "hw_gpu_vram_max_bytes": float(max(vram)) if vram else NAN,
    }


def _load(node: NodeSnapshot | None) -> dict[str, float]:
    if node is None:
        return dict.fromkeys(
            (
                "load_cpu_percent",
                "load_ram_free_bytes",
                "load_live_attempts",
                "load_used_cpu_slots",
                "load_cpu_slots",
                "load_owner_active",
            ),
            NAN,
        )
    return {
        "load_cpu_percent": _num(node.cpu_percent),
        "load_ram_free_bytes": _num(node.ram_free_bytes),
        "load_live_attempts": float(node.live_attempts),
        "load_used_cpu_slots": float(node.used_cpu_slots),
        "load_cpu_slots": float(node.cpu_slots),
        "load_owner_active": _flag(node.owner_active),
    }


def _cache(
    request: PlanRequest, node: NodeSnapshot | None, *, local: bool
) -> dict[str, float]:
    if node is None:
        return dict.fromkeys(
            (
                "cache_missing_bytes",
                "cache_missing_frac",
                "cache_env_cached",
                "cache_items_tree_held",
            ),
            NAN,
        )
    missing = missing_bytes(request, node, local=local)
    total = _input_bytes(request)
    env_hash = request.history.env_hash_by_node.get(node.node_id)
    split = request.split
    items_tree = None if split is None else split.items_tree
    return {
        "cache_missing_bytes": float(missing),
        "cache_missing_frac": missing / total if total > 0 else NAN,
        "cache_env_cached": _flag(
            env_hash is not None and env_hash in node.cached_envs
        ),
        "cache_items_tree_held": NAN
        if items_tree is None
        else _flag(items_tree in node.cached),
    }


def _age_s(at: dt.datetime | None, now: dt.datetime | None) -> float:
    if at is None or now is None:
        return NAN
    try:
        return max(0.0, (now - at).total_seconds())
    except TypeError:  # naive vs aware: unknowable, not 0
        return NAN


def _network(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    node_id: str,
    detail: Mapping[str, Any],
    now: dt.datetime | None,
) -> dict[str, float]:
    ctl = snapshot.controller_links.get(node_id)
    keys = {tree.key for tree in request.trees}
    holders = {
        other.node_id
        for other in snapshot.nodes
        if other.node_id != node_id and other.cached & keys
    }
    best = None
    for (src, dst), link in sorted(snapshot.links.items()):
        if src != node_id or dst not in holders:
            continue
        bw = _num(link.bw_bytes_s)
        if math.isnan(bw):
            continue
        if best is None or bw > _num(best.bw_bytes_s):
            best = link
    return {
        # the planner's own (binary) view: no submitter = no Local candidate
        "net_local": _flag(node_id == request.submitter_node_id),
        "net_ctl_bw_bytes_s": NAN if ctl is None else _num(ctl.bw_bytes_s),
        "net_peer_bw_bytes_s": NAN if best is None else _num(best.bw_bytes_s),
        "net_peer_rtt_ms": NAN if best is None else _num(best.rtt_ms),
        "net_peer_link_age_s": NAN if best is None else _age_s(best.measured_at, now),
        "net_term_bw_bytes_s": _num(detail.get("bw_bytes_s")),
    }


def _job(request: PlanRequest, predicted: Predicted) -> dict[str, float]:
    split = request.split
    history = request.history
    return {
        "job_input_bytes": float(_input_bytes(request)),
        "job_items": NAN if split is None else float(split.items),
        "job_item_bytes": NAN if split is None else _num(split.item_bytes),
        "job_chunk_items": _num(predicted.chunk_items),
        "job_lanes": float(predicted.lanes),
        "job_cpus": _num(request.cpus),
        "job_ram_bytes": _num(request.ram_bytes),
        "job_vram_bytes": _num(request.vram_bytes),
        "job_gpu": _flag(request.is_gpu_job),
        "job_hist_output_bytes": _num(history.output_bytes),
    }


def _analytical(
    request: PlanRequest, node_id: str, predicted: Predicted
) -> dict[str, float]:
    history = request.history
    known = predicted.source in {s.value for _, s in _SOURCES}
    return {
        "an_raw_s": _num(predicted.raw_s),
        **{
            name: _flag(predicted.source == source.value) if known else NAN
            for name, source in _SOURCES
        },
        "an_term_samples": _num(predicted.detail.get("samples")),
        "an_hist_run_s": _num(history.by_node_s.get(node_id)),
        "an_hist_samples": float(history.samples_by_node.get(node_id, 0)),
    }


def features(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    now: dt.datetime | None,
    node_id: str,
    predicted: Predicted,
) -> dict[str, float]:
    """``node_id``'s features for one term of ``request`` (module docstring),
    keyed and ordered as :data:`~.schema.NUMERIC_FEATURES`."""
    node = _node(snapshot, node_id)
    local = request.submitter_node_id is not None and (
        node_id == request.submitter_node_id
    )
    values = {
        **_hardware(node),
        **_load(node),
        **_cache(request, node, local=local),
        **_network(request, snapshot, node_id, predicted.detail, now),
        **_job(request, predicted),
        **_analytical(request, node_id, predicted),
    }
    return {name: values[name] for name in NUMERIC_FEATURES}


def features_from_replay(
    replay: Mapping[str, Any], node_id: str, predicted: Predicted
) -> dict[str, float]:
    """:func:`features` on a stored replay (raises
    :class:`~meshforge.controller.planner.replay.ReplayError` when it cannot
    be read)."""
    request, snapshot, now = from_replay(replay)
    return features(request, snapshot, now, node_id, predicted)
