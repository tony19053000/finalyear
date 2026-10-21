"""Replayable decisions (V0.14 G1, D292; ADR-0020 decision 1).

Every ``plan_decision`` row stores a **replay snapshot**: the planner's exact
inputs - the :class:`~.types.PlanRequest`, the planner-relevant part of the
:class:`~.types.ClusterSnapshot` and ``now`` - as canonical JSON. Because
:func:`~.plan.plan` is pure, ``plan(*from_replay(replay), policy)`` gives the
stored decision again, field for field, for the policy that made it - and any
*other* policy's decision on the very situation the planner saw (the V0.14
evaluator's regret, V0.15's training rows).

**What is kept (:func:`project`).** Everything ``plan()`` reads (audited over
``plan.py``, ``feasibility.py``, ``estimator.py``, ``split.py``,
``split_plan.py``, ``policies.py``): the whole request (its retry exclusions,
history medians/rates and the ``random`` seed inputs ``job_id`` +
``attempt_no`` included); every node with all its fields, except that its
cache holdings are cut to the job's own trees (and the split items tree), its
known-bad envs to the job's env key and its built envs to the env hashes the
job's history names; network-matrix links **into** a snapshot node **from** a
node holding one of the job's trees (the only links the estimator looks up -
stale ones included, the estimator names them); controller links of the
snapshot's nodes; the environment-build history of the job's ``env.base``; the
``round_robin`` cursor and the planner configuration. Whatever is dropped is
never read for this request, so ``plan(request, project(request, snapshot))``
equals ``plan(request, snapshot)`` in every field but ``snapshot_digest`` (a
property test pins it). The dispatcher plans on the projection, so the
digest it stores is the digest of exactly what the replay holds.

**Format** (``replay_version`` 1, :data:`REPLAY_VERSION`): one JSON object
with ``replay_version`` (1), ``now`` (ISO 8601 or null), ``request`` and
``snapshot`` (the dataclasses' fields, :data:`REPLAY_FIELDS`; links as a
sorted list of ``{src, dst, ...}`` rows) and - only with a shadow plan (D215)
- ``shadow: {request}``.

Canonical: sorted keys, no whitespace, no ``NaN``/``Infinity`` literals. A
float that is not finite, or so large that PostgreSQL's ``jsonb`` (which keeps
numbers as ``numeric``) would hand it back as an integer, is written
``{"$f": "<repr>"}``; every other number is written as itself, so an integer
stays an integer and a float a float - the replayed decision's JSON is
byte-identical too. Datetimes are ISO 8601 strings; sets are sorted lists.

**Bounded.** :func:`bounded_replay` returns None for a replay larger than
``[controller.planner] replay_max_bytes`` (65 536 B by default): the caller
counts and logs it and stores ``replay: null`` - a replay never fails a
dispatch.

**The shadow plan** of a forced benchmark run (D215/D235) is the same request
unpinned, unforced, without the owner guard and with its own retry
exclusions, planned on :func:`idle_snapshot` of the same snapshot at the same
``now``: its replay stores only that request; :func:`shadow_from_replay`
rebuilds the rest.

Pure: no database, no clock, no logging (the planner's AST import test).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
from collections.abc import Mapping
from typing import Any, Final, cast

from meshforge.common.owner_policy import OwnerPolicy
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    ControllerLinkStat,
    EnvBuildStat,
    GpuNeed,
    History,
    LinkStat,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    SplitConfig,
    SplitInfo,
    TreeNeed,
)

__all__ = [
    "REPLAY_FIELDS",
    "REPLAY_VERSION",
    "ReplayError",
    "bounded_replay",
    "canonical_json",
    "from_replay",
    "idle_snapshot",
    "project",
    "replay_size",
    "shadow_from_replay",
    "to_replay",
]

REPLAY_VERSION: Final = 1
"""Bump on any change of the format; old versions stay readable."""

_BIG: Final = 1e15
"""Floats at least this large are tagged: ``jsonb`` prints them without a
decimal point and they would come back as integers."""

REPLAY_FIELDS: Final[Mapping[type, frozenset[str]]] = {
    PlanRequest: frozenset(
        {
            "job_id",
            "attempt_no",
            "gpu",
            "vram_bytes",
            "ram_bytes",
            "cpus",
            "base",
            "env_key",
            "trees",
            "pinned_node_id",
            "submitter_node_id",
            "seeded",
            "fingerprint",
            "history",
            "excluded",
            "owner_name",
            "network",
            "benchmark",
            "split",
            "node_set",
        }
    ),
    History: frozenset(
        {
            "by_node_s",
            "samples_by_node",
            "output_bytes",
            "output_samples",
            "env_hash_by_node",
            "item_rate_by_node",
            "item_rate_samples_by_node",
            "chunk_overhead_s",
            "chunk_overhead_samples",
        }
    ),
    SplitInfo: frozenset(
        {"items", "item_bytes", "items_tree", "chunk_items", "min_nodes", "max_nodes"}
    ),
    TreeNeed: frozenset({"key", "size"}),
    NodeSnapshot: frozenset(
        {
            "node_id",
            "state",
            "connected",
            "capabilities",
            "ram_bytes",
            "cpu_threads",
            "gpu_vram_bytes",
            "gpu_indices",
            "busy_gpus",
            "cpu_slots",
            "gpu_slots",
            "used_cpu_slots",
            "used_gpu_slots",
            "live_attempts",
            "cpu_percent",
            "ram_free_bytes",
            "disk_free_bytes",
            "cpu_score",
            "gpu_score",
            "cached",
            "known_bad_envs",
            "cached_envs",
            "policy",
            "owner_active",
        }
    ),
    ClusterSnapshot: frozenset(
        {
            "nodes",
            "round_robin_after",
            "config",
            "links",
            "controller_links",
            "env_builds",
        }
    ),
    PlannerConfig: frozenset(
        {
            "default_bw_bytes_s",
            "default_compute_s",
            "default_env_s",
            "max_attempts_per_node",
            "resource_weights",
            "link_stale_after_s",
            "min_bw_bytes_s",
            "split",
        }
    ),
    SplitConfig: frozenset(
        {
            "split_margin",
            "target_chunk_s",
            "max_overhead_frac",
            "min_chunks_per_node",
            "max_chunks",
            "chunk_overhead_s",
            "tail_frac",
        }
    ),
    LinkStat: frozenset({"bw_bytes_s", "rtt_ms", "samples", "measured_at"}),
    ControllerLinkStat: frozenset({"bw_bytes_s", "samples", "last_at"}),
    EnvBuildStat: frozenset({"median_s", "samples"}),
    OwnerPolicy: frozenset(
        {
            "accept_jobs",
            "accept_gpu_sessions",
            "max_ram_bytes",
            "max_cpu_percent",
            "max_vram_bytes",
            "allow_unsandboxed",
            "allow_network",
            "allowed_users",
            "not_enforced",
            "revision",
            "pause_benchmarks",
        }
    ),
}
"""The fields replay version 1 stores per type. A unit test compares them with
the dataclasses: a new planner input fails it until the replay carries it
(and the version is bumped if old replays cannot be read the same way)."""


class ReplayError(ValueError):
    """A stored replay that cannot be read (unknown version, malformed)."""


# ------------------------------------------------------------------ projection


def project(request: PlanRequest, snapshot: ClusterSnapshot) -> ClusterSnapshot:
    """``snapshot`` cut to what :func:`~.plan.plan` reads for ``request``
    (module docstring). Idempotent; nodes, their order and every field the
    planner reads are unchanged."""
    keys = {tree.key for tree in request.trees}
    if request.split is not None and request.split.items_tree is not None:
        keys.add(request.split.items_tree)
    wanted_envs = set(request.history.env_hash_by_node.values())
    bad = {request.env_key}
    nodes = tuple(
        dataclasses.replace(
            node,
            cached=frozenset(node.cached & keys),
            known_bad_envs=frozenset(node.known_bad_envs & bad),
            cached_envs={
                env: size
                for env, size in node.cached_envs.items()
                if env in wanted_envs
            },
        )
        for node in snapshot.nodes
    )
    ids = {node.node_id for node in nodes}
    holders = {node.node_id for node in nodes if node.cached}
    return dataclasses.replace(
        snapshot,
        nodes=nodes,
        links={
            pair: link
            for pair, link in snapshot.links.items()
            if pair[0] in ids and pair[1] in holders
        },
        controller_links={
            node_id: link
            for node_id, link in snapshot.controller_links.items()
            if node_id in ids
        },
        env_builds={
            base: built
            for base, built in snapshot.env_builds.items()
            if base == request.base
        },
    )


def idle_snapshot(snapshot: ClusterSnapshot) -> ClusterSnapshot:
    """``snapshot`` with no slot, GPU or attempt in use on any node: the
    shadow plan's view (D235 - a preference, not "what would run now")."""
    return dataclasses.replace(
        snapshot,
        nodes=tuple(
            dataclasses.replace(
                node,
                used_cpu_slots=0,
                used_gpu_slots=0,
                live_attempts=0,
                busy_gpus=frozenset(),
            )
            for node in snapshot.nodes
        ),
    )


# ------------------------------------------------------------------ encoding


def _num(value: float | None) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    number = float(value)
    if not math.isfinite(number) or abs(number) >= _BIG:
        return {"$f": repr(number)}
    return number


def _nums(values: Mapping[str, float]) -> dict[str, Any]:
    return {str(k): _num(v) for k, v in values.items()}


def _when(value: dt.datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _history(history: History) -> dict[str, Any]:
    return {
        "by_node_s": _nums(history.by_node_s),
        "samples_by_node": _nums(history.samples_by_node),
        "output_bytes": _num(history.output_bytes),
        "output_samples": history.output_samples,
        "env_hash_by_node": dict(history.env_hash_by_node),
        "item_rate_by_node": _nums(history.item_rate_by_node),
        "item_rate_samples_by_node": _nums(history.item_rate_samples_by_node),
        "chunk_overhead_s": _num(history.chunk_overhead_s),
        "chunk_overhead_samples": history.chunk_overhead_samples,
    }


def _split_info(split: SplitInfo | None) -> dict[str, Any] | None:
    if split is None:
        return None
    return {
        "items": split.items,
        "item_bytes": split.item_bytes,
        "items_tree": split.items_tree,
        "chunk_items": split.chunk_items,
        "min_nodes": split.min_nodes,
        "max_nodes": split.max_nodes,
    }


def _request(request: PlanRequest) -> dict[str, Any]:
    return {
        "job_id": request.job_id,
        "attempt_no": request.attempt_no,
        "gpu": request.gpu,
        "vram_bytes": request.vram_bytes,
        "ram_bytes": request.ram_bytes,
        "cpus": _num(request.cpus),
        "base": request.base,
        "env_key": request.env_key,
        "trees": [{"key": t.key, "size": t.size} for t in request.trees],
        "pinned_node_id": request.pinned_node_id,
        "submitter_node_id": request.submitter_node_id,
        "seeded": request.seeded,
        "fingerprint": request.fingerprint,
        "history": _history(request.history),
        "excluded": dict(request.excluded),
        "owner_name": request.owner_name,
        "network": request.network,
        "benchmark": request.benchmark,
        "split": _split_info(request.split),
        "node_set": list(request.node_set),
    }


def _policy(policy: OwnerPolicy | None) -> dict[str, Any] | None:
    return None if policy is None else policy.as_dict()


def _node(node: NodeSnapshot) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "state": node.state,
        "connected": node.connected,
        "capabilities": sorted(node.capabilities),
        "ram_bytes": node.ram_bytes,
        "cpu_threads": node.cpu_threads,
        "gpu_vram_bytes": list(node.gpu_vram_bytes),
        "gpu_indices": list(node.gpu_indices),
        "busy_gpus": sorted(node.busy_gpus),
        "cpu_slots": node.cpu_slots,
        "gpu_slots": node.gpu_slots,
        "used_cpu_slots": node.used_cpu_slots,
        "used_gpu_slots": node.used_gpu_slots,
        "live_attempts": node.live_attempts,
        "cpu_percent": _num(node.cpu_percent),
        "ram_free_bytes": node.ram_free_bytes,
        "disk_free_bytes": node.disk_free_bytes,
        "cpu_score": _num(node.cpu_score),
        "gpu_score": _num(node.gpu_score),
        "cached": sorted(node.cached),
        "known_bad_envs": sorted(node.known_bad_envs),
        "cached_envs": dict(node.cached_envs),
        "policy": _policy(node.policy),
        "owner_active": node.owner_active,
    }


def _config(config: PlannerConfig) -> dict[str, Any]:
    split = config.split
    return {
        "default_bw_bytes_s": _num(config.default_bw_bytes_s),
        "default_compute_s": _num(config.default_compute_s),
        "default_env_s": _num(config.default_env_s),
        "max_attempts_per_node": config.max_attempts_per_node,
        "resource_weights": [_num(w) for w in config.resource_weights],
        "link_stale_after_s": _num(config.link_stale_after_s),
        "min_bw_bytes_s": _num(config.min_bw_bytes_s),
        "split": {
            "split_margin": _num(split.split_margin),
            "target_chunk_s": _num(split.target_chunk_s),
            "max_overhead_frac": _num(split.max_overhead_frac),
            "min_chunks_per_node": split.min_chunks_per_node,
            "max_chunks": split.max_chunks,
            "chunk_overhead_s": _num(split.chunk_overhead_s),
            "tail_frac": _num(split.tail_frac),
        },
    }


def _snapshot(snapshot: ClusterSnapshot) -> dict[str, Any]:
    return {
        "nodes": [_node(n) for n in snapshot.nodes],
        "round_robin_after": snapshot.round_robin_after,
        "config": _config(snapshot.config),
        # (src, dst) keys are not JSON keys: a sorted list of rows
        "links": [
            {
                "src": src,
                "dst": dst,
                "bw_bytes_s": _num(link.bw_bytes_s),
                "rtt_ms": _num(link.rtt_ms),
                "samples": link.samples,
                "measured_at": _when(link.measured_at),
            }
            for (src, dst), link in sorted(snapshot.links.items())
        ],
        "controller_links": {
            node_id: {
                "bw_bytes_s": _num(link.bw_bytes_s),
                "samples": link.samples,
                "last_at": _when(link.last_at),
            }
            for node_id, link in snapshot.controller_links.items()
        },
        "env_builds": {
            base: {"median_s": _num(built.median_s), "samples": built.samples}
            for base, built in snapshot.env_builds.items()
        },
    }


def to_replay(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    now: dt.datetime | None,
    *,
    shadow: PlanRequest | None = None,
    projected: bool = True,
) -> dict[str, Any]:
    """The replay of ``plan(request, snapshot, policy, now)`` (module
    docstring). ``projected=False`` keeps the whole snapshot (tests that
    compare digests with an unprojected plan). ``shadow`` = the shadow
    plan's request (planned on :func:`idle_snapshot` at the same ``now``).
    :class:`ReplayError` for a naive (timezone-less) ``now``."""
    # V0.14 G3 (G1 review N4): a naive ``now`` would be stored without its
    # zone and replay against links measured in UTC - never write one
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ReplayError("a replay's now must be timezone-aware")
    view = project(request, snapshot) if projected else snapshot
    out: dict[str, Any] = {
        "replay_version": REPLAY_VERSION,
        "now": _when(now),
        "request": _request(request),
        "snapshot": _snapshot(view),
    }
    if shadow is not None:
        out["shadow"] = {"request": _request(shadow)}
    return out


def canonical_json(replay: Mapping[str, Any]) -> str:
    """The canonical text of a replay (sorted keys, no whitespace, no NaN)."""
    return json.dumps(
        replay,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )


def replay_size(replay: Mapping[str, Any]) -> int:
    """Bytes of :func:`canonical_json` in UTF-8."""
    return len(canonical_json(replay).encode("utf-8"))


def bounded_replay(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    now: dt.datetime | None,
    *,
    max_bytes: int,
    shadow: PlanRequest | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """``(replay, size)``; the replay is None when it is larger than
    ``max_bytes`` (the caller counts and logs it, never fails on it)."""
    replay = to_replay(request, snapshot, now, shadow=shadow)
    size = replay_size(replay)
    if size > max_bytes:
        return None, size
    return replay, size


# ------------------------------------------------------------------ decoding


def _dnum(value: Any) -> Any:
    if isinstance(value, Mapping):
        tagged = value.get("$f")
        if not isinstance(tagged, str) or len(value) != 1:
            raise ReplayError("a number in the replay is malformed")
        return float(tagged)
    # V0.14 G3 (G1 review N2): ``True`` is an ``int`` to Python, never a number here
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int | float)
    ):
        raise ReplayError("a number in the replay is not a number")
    return value


# --- V0.14 G3 (G1 review N2): strict integer / boolean fields ---------------


def _int(value: Any) -> int:
    """An integer field: an ``int`` that is not a ``bool`` (``int("5")`` and
    ``int(True)`` would silently accept a corrupt replay)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayError("an integer in the replay is not an integer")
    return value


def _int_or_none(value: Any) -> int | None:
    return None if value is None else _int(value)


def _ints(values: Any) -> tuple[int, ...]:
    if not isinstance(values, list):
        raise ReplayError("a list of integers in the replay is not a list")
    return tuple(_int(v) for v in values)


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ReplayError("a boolean in the replay is not a boolean")
    return value


# --- end V0.14 G3 (N2) -------------------------------------------------------


def _dnums(values: Any) -> dict[str, Any]:
    return {str(k): _dnum(v) for k, v in dict(values).items()}


def _dwhen(value: Any) -> dt.datetime | None:
    return None if value is None else dt.datetime.fromisoformat(str(value))


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _from_history(data: Mapping[str, Any]) -> History:
    return History(
        by_node_s=_dnums(data["by_node_s"]),
        samples_by_node=_dnums(data["samples_by_node"]),
        output_bytes=_dnum(data["output_bytes"]),
        output_samples=_int(data["output_samples"]),
        env_hash_by_node={str(k): str(v) for k, v in data["env_hash_by_node"].items()},
        item_rate_by_node=_dnums(data["item_rate_by_node"]),
        item_rate_samples_by_node=_dnums(data["item_rate_samples_by_node"]),
        chunk_overhead_s=_dnum(data["chunk_overhead_s"]),
        chunk_overhead_samples=_int(data["chunk_overhead_samples"]),
    )


def _from_split(data: Mapping[str, Any] | None) -> SplitInfo | None:
    if data is None:
        return None
    return SplitInfo(
        items=_int(data["items"]),
        item_bytes=_int_or_none(data["item_bytes"]),
        items_tree=_str_or_none(data["items_tree"]),
        chunk_items=_int_or_none(data["chunk_items"]),
        min_nodes=_int(data["min_nodes"]),
        max_nodes=_int_or_none(data["max_nodes"]),
    )


def _from_request(data: Mapping[str, Any]) -> PlanRequest:
    return PlanRequest(
        job_id=str(data["job_id"]),
        attempt_no=_int(data["attempt_no"]),
        gpu=cast("GpuNeed", str(data["gpu"])),
        vram_bytes=_int_or_none(data["vram_bytes"]),
        ram_bytes=_int_or_none(data["ram_bytes"]),
        cpus=_dnum(data["cpus"]),
        base=str(data["base"]),
        env_key=str(data["env_key"]),
        trees=tuple(TreeNeed(str(t["key"]), _int(t["size"])) for t in data["trees"]),
        pinned_node_id=_str_or_none(data["pinned_node_id"]),
        submitter_node_id=_str_or_none(data["submitter_node_id"]),
        seeded=_bool(data["seeded"]),
        fingerprint=_str_or_none(data["fingerprint"]),
        history=_from_history(data["history"]),
        excluded={str(k): str(v) for k, v in data["excluded"].items()},
        owner_name=_str_or_none(data["owner_name"]),
        network=_bool(data["network"]),
        benchmark=_bool(data["benchmark"]),
        split=_from_split(data["split"]),
        node_set=tuple(str(n) for n in data["node_set"]),
    )


def _from_policy(data: Mapping[str, Any] | None) -> OwnerPolicy | None:
    if data is None:
        return None
    return OwnerPolicy(
        accept_jobs=_bool(data["accept_jobs"]),
        accept_gpu_sessions=_bool(data["accept_gpu_sessions"]),
        max_ram_bytes=_int_or_none(data["max_ram_bytes"]),
        max_cpu_percent=_int_or_none(data["max_cpu_percent"]),
        max_vram_bytes=_int_or_none(data["max_vram_bytes"]),
        allow_unsandboxed=_bool(data["allow_unsandboxed"]),
        allow_network=_bool(data["allow_network"]),
        allowed_users=tuple(str(u) for u in data["allowed_users"]),
        not_enforced=tuple(str(u) for u in data["not_enforced"]),
        revision=_int(data["revision"]),
        pause_benchmarks=_bool(data["pause_benchmarks"]),
    )


def _from_node(data: Mapping[str, Any]) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=str(data["node_id"]),
        state=str(data["state"]),
        connected=_bool(data["connected"]),
        capabilities=frozenset(str(c) for c in data["capabilities"]),
        ram_bytes=_int_or_none(data["ram_bytes"]),
        cpu_threads=_int_or_none(data["cpu_threads"]),
        gpu_vram_bytes=_ints(data["gpu_vram_bytes"]),
        gpu_indices=_ints(data["gpu_indices"]),
        busy_gpus=frozenset(_ints(data["busy_gpus"])),
        cpu_slots=_int(data["cpu_slots"]),
        gpu_slots=_int(data["gpu_slots"]),
        used_cpu_slots=_int(data["used_cpu_slots"]),
        used_gpu_slots=_int(data["used_gpu_slots"]),
        live_attempts=_int(data["live_attempts"]),
        cpu_percent=_dnum(data["cpu_percent"]),
        ram_free_bytes=_int_or_none(data["ram_free_bytes"]),
        disk_free_bytes=_int_or_none(data["disk_free_bytes"]),
        cpu_score=_dnum(data["cpu_score"]),
        gpu_score=_dnum(data["gpu_score"]),
        cached=frozenset(str(k) for k in data["cached"]),
        known_bad_envs=frozenset(str(k) for k in data["known_bad_envs"]),
        cached_envs={str(k): _int(v) for k, v in data["cached_envs"].items()},
        policy=_from_policy(data["policy"]),
        owner_active=_bool(data["owner_active"]),
    )


def _from_config(data: Mapping[str, Any]) -> PlannerConfig:
    split = data["split"]
    w_ram, w_slots, w_score = (_dnum(w) for w in data["resource_weights"])
    return PlannerConfig(
        default_bw_bytes_s=_dnum(data["default_bw_bytes_s"]),
        default_compute_s=_dnum(data["default_compute_s"]),
        default_env_s=_dnum(data["default_env_s"]),
        max_attempts_per_node=_int(data["max_attempts_per_node"]),
        resource_weights=(w_ram, w_slots, w_score),
        link_stale_after_s=_dnum(data["link_stale_after_s"]),
        min_bw_bytes_s=_dnum(data["min_bw_bytes_s"]),
        split=SplitConfig(
            split_margin=_dnum(split["split_margin"]),
            target_chunk_s=_dnum(split["target_chunk_s"]),
            max_overhead_frac=_dnum(split["max_overhead_frac"]),
            min_chunks_per_node=_int(split["min_chunks_per_node"]),
            max_chunks=_int(split["max_chunks"]),
            chunk_overhead_s=_dnum(split["chunk_overhead_s"]),
            tail_frac=_dnum(split["tail_frac"]),
        ),
    )


def _from_snapshot(data: Mapping[str, Any]) -> ClusterSnapshot:
    return ClusterSnapshot(
        nodes=tuple(_from_node(n) for n in data["nodes"]),
        round_robin_after=_str_or_none(data["round_robin_after"]),
        config=_from_config(data["config"]),
        links={
            (str(row["src"]), str(row["dst"])): LinkStat(
                bw_bytes_s=_dnum(row["bw_bytes_s"]),
                rtt_ms=_dnum(row["rtt_ms"]),
                samples=_int(row["samples"]),
                measured_at=cast("dt.datetime", _dwhen(row["measured_at"])),
            )
            for row in data["links"]
        },
        controller_links={
            str(node_id): ControllerLinkStat(
                bw_bytes_s=_dnum(row["bw_bytes_s"]),
                samples=_int(row["samples"]),
                last_at=_dwhen(row["last_at"]),
            )
            for node_id, row in data["controller_links"].items()
        },
        env_builds={
            str(base): EnvBuildStat(
                median_s=_dnum(row["median_s"]), samples=_int(row["samples"])
            )
            for base, row in data["env_builds"].items()
        },
    )


def _version(replay: Mapping[str, Any]) -> None:
    if not isinstance(replay, Mapping):
        raise ReplayError("a replay is a JSON object")
    version = replay.get("replay_version")
    if version != REPLAY_VERSION:
        raise ReplayError(f"unsupported replay_version {version!r}")


def from_replay(
    replay: Mapping[str, Any],
) -> tuple[PlanRequest, ClusterSnapshot, dt.datetime | None]:
    """``(request, snapshot, now)`` of a stored replay; :class:`ReplayError`
    when it cannot be read."""
    _version(replay)
    try:
        return (
            _from_request(replay["request"]),
            _from_snapshot(replay["snapshot"]),
            _dwhen(replay["now"]),
        )
    except ReplayError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ReplayError(f"malformed replay ({type(exc).__name__})") from None


def shadow_from_replay(
    replay: Mapping[str, Any],
) -> tuple[PlanRequest, ClusterSnapshot, dt.datetime | None] | None:
    """The shadow plan's ``(request, idle snapshot, now)``; None = the
    decision had no shadow plan."""
    _version(replay)
    shadow = replay.get("shadow")
    if shadow is None:
        return None
    _request_unused, snapshot, now = from_replay(replay)
    try:
        request = _from_request(shadow["request"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ReplayError(f"malformed shadow replay ({type(exc).__name__})") from None
    return request, idle_snapshot(snapshot), now
