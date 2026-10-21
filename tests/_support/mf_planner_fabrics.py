"""V0.14 G1: seeded random planner inputs (requests + snapshots) for the
replay property test and the decision-latency benchmark.

:func:`random_case` draws everything the planner reads, deliberately
including what it must survive: nodes that are offline, revoked, without a
session or slots; GPU and CPU jobs; pinned, forced-set, retried (excluded)
and benchmark jobs; split jobs too small to split or with too few nodes;
fresh, stale and future-dated links; non-finite scores, CPU loads and
history; cache entries of *other* jobs' trees and links between nodes that do
not hold the job's trees (what :func:`~meshforge.controller.planner.replay.
project` drops). :func:`synthetic_fabric` is a tidy N-node fabric (every node
online and able) for timing.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

from meshforge.common.owner_policy import OPEN_POLICY, OwnerPolicy
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    ControllerLinkStat,
    EnvBuildStat,
    History,
    LinkStat,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    SplitConfig,
    SplitInfo,
    TreeNeed,
)

__all__ = ["NOW", "random_case", "synthetic_fabric"]

GIB = 1024**3
NOW = dt.datetime(2026, 10, 21, 12, 0, 0, 123456, tzinfo=dt.UTC)
_BASES = ("python-cpu", "cuda-pytorch")
_CAPS = (
    "exec",
    "exec-subprocess",
    "exec-docker",
    "exec-gpu",
    "exec-split",
    "exec-venv",
)
_STATES = ("online",) * 6 + ("suspect", "offline", "draining", "revoked")
_ODD = (float("nan"), float("inf"), -1.0, 0.0)


def _tree(n: int) -> str:
    return "sha256:" + f"{n:02x}" * 32


def _maybe_odd(rng: random.Random, value: float) -> float:
    return rng.choice(_ODD) if rng.random() < 0.05 else value


def _policy(rng: random.Random) -> OwnerPolicy | None:
    roll = rng.random()
    if roll < 0.5:
        return None
    if roll < 0.8:
        return OPEN_POLICY
    return OwnerPolicy(
        accept_jobs=rng.random() < 0.8,
        max_ram_bytes=rng.choice((None, 4 * GIB, 64 * GIB)),
        max_cpu_percent=rng.choice((None, 50, 100)),
        allow_unsandboxed=rng.random() < 0.7,
        allow_network=rng.random() < 0.5,
        allowed_users=rng.choice(((), ("alice",), ("bob",))),
        pause_benchmarks=rng.random() < 0.2,
    )


def _node(
    rng: random.Random, node_id: str, trees: list[str], envs: list[str]
) -> NodeSnapshot:
    caps = {c for c in _CAPS if rng.random() < 0.7}
    if rng.random() < 0.9:
        caps.add("exec")
    gpus = tuple(
        rng.choice((4, 8, 12, 0)) * GIB for _ in range(rng.choice((0, 0, 1, 2)))
    )
    cpu_slots = rng.choice((0, 1, 2, 4))
    gpu_slots = rng.choice((0, 1, 2)) if gpus else 0
    return NodeSnapshot(
        node_id=node_id,
        state=rng.choice(_STATES),
        connected=rng.random() < 0.9,
        capabilities=frozenset(caps),
        ram_bytes=rng.choice((None, 8 * GIB, 16 * GIB, 64 * GIB)),
        cpu_threads=rng.choice((None, 4, 8, 16)),
        gpu_vram_bytes=gpus,
        gpu_indices=tuple(range(len(gpus))) if rng.random() < 0.5 else (),
        busy_gpus=frozenset(i for i in range(len(gpus)) if rng.random() < 0.3),
        cpu_slots=cpu_slots,
        gpu_slots=gpu_slots,
        used_cpu_slots=rng.randint(0, cpu_slots) if cpu_slots else 0,
        used_gpu_slots=rng.randint(0, gpu_slots) if gpu_slots else 0,
        live_attempts=rng.randint(0, 9),
        cpu_percent=None
        if rng.random() < 0.3
        else _maybe_odd(rng, rng.uniform(0, 100)),
        ram_free_bytes=None if rng.random() < 0.3 else rng.randint(0, 32) * GIB,
        disk_free_bytes=None if rng.random() < 0.3 else rng.randint(0, 200) * 2**28,
        cpu_score=None if rng.random() < 0.2 else _maybe_odd(rng, rng.uniform(10, 900)),
        gpu_score=None if rng.random() < 0.6 else _maybe_odd(rng, rng.uniform(1, 40)),
        cached=frozenset(t for t in trees if rng.random() < 0.4),
        known_bad_envs=frozenset(e for e in envs if rng.random() < 0.1),
        cached_envs={e: rng.randint(1, 10**9) for e in envs if rng.random() < 0.3},
        policy=_policy(rng),
        owner_active=rng.random() < 0.1,
    )


def _history(rng: random.Random, ids: list[str], envs: list[str]) -> History:
    if rng.random() < 0.3:
        return History()
    who = [n for n in ids if rng.random() < 0.5]
    return History(
        by_node_s={n: _maybe_odd(rng, rng.uniform(0.5, 900)) for n in who},
        samples_by_node={n: rng.randint(1, 20) for n in who},
        output_bytes=rng.choice((None, 0.0, 1e6, 5e8, float("nan"))),
        output_samples=rng.randint(0, 9),
        env_hash_by_node={n: rng.choice(envs) for n in ids if rng.random() < 0.4},
        item_rate_by_node={
            n: _maybe_odd(rng, rng.uniform(0.1, 500)) for n in ids if rng.random() < 0.3
        },
        item_rate_samples_by_node={
            n: rng.randint(1, 9) for n in ids if rng.random() < 0.3
        },
        chunk_overhead_s=rng.choice((None, 0.4, 2.5, float("inf"))),
        chunk_overhead_samples=rng.randint(0, 9),
    )


def _split(rng: random.Random, trees: tuple[TreeNeed, ...]) -> SplitInfo | None:
    if rng.random() < 0.55:
        return None
    items = rng.choice((1, 3, 4, 7, 40, 500, 5000))
    with_bytes = rng.random() < 0.6
    return SplitInfo(
        items=items,
        item_bytes=rng.randint(1, 10**9) if with_bytes else None,
        items_tree=rng.choice(trees).key if with_bytes else None,
        chunk_items=rng.choice((None, None, 1, 10, 100)),
        min_nodes=rng.choice((2, 2, 3)),
        max_nodes=rng.choice((None, None, 2, 4)),
    )


def random_case(seed: int) -> tuple[PlanRequest, ClusterSnapshot, dt.datetime | None]:
    """One random ``(request, snapshot, now)`` (module docstring)."""
    rng = random.Random(seed)  # noqa: S311 - test fabrics, not security
    count = rng.randint(1, 6)
    ids = sorted(f"node_{seed:05d}_{i}" for i in range(count))
    job_trees = [_tree(rng.randint(0, 9)) for _ in range(rng.randint(1, 3))]
    other_trees = [_tree(rng.randint(10, 30)) for _ in range(rng.randint(0, 3))]
    envs = ["e" * 64, "f" * 64, "0" * 64]
    nodes = tuple(_node(rng, n, job_trees + other_trees, envs) for n in ids)
    trees = tuple(TreeNeed(t, rng.randint(0, 5 * 10**8)) for t in job_trees)
    now = None if rng.random() < 0.1 else NOW
    split = _split(rng, trees)
    roll = rng.random()
    pinned = rng.choice([*ids, "node_unknown"]) if roll < 0.12 else None
    node_set: tuple[str, ...] = ()
    if pinned is None and split is not None and roll < 0.25:
        node_set = tuple(sorted(rng.sample(ids, k=min(len(ids), rng.randint(1, 3)))))
    request = PlanRequest(
        job_id=f"job_{seed:026d}",
        attempt_no=rng.randint(1, 4),
        gpu=rng.choice(("none", "none", "preferred", "required")),
        vram_bytes=rng.choice((None, None, 2 * GIB, 10 * GIB)),
        ram_bytes=rng.choice((None, 4 * GIB, 32 * GIB)),
        cpus=rng.choice((None, 1.0, 2, 12.5)),
        base=rng.choice(_BASES),
        env_key=rng.choice(envs),
        trees=trees,
        pinned_node_id=pinned,
        submitter_node_id=rng.choice([*ids, None, "node_elsewhere"]),
        seeded=rng.random() < 0.5,
        fingerprint=None if rng.random() < 0.2 else "1" * 64,
        history=_history(rng, [*ids, "node_gone"], envs),
        excluded={
            n: rng.choice(
                ("node_lost", "timeout", "transfer_error", "vram_oom", "disk_full")
            )
            for n in ids
            if rng.random() < 0.15
        },
        owner_name=rng.choice((None, "alice", "carol")),
        network=rng.random() < 0.2,
        benchmark=rng.random() < 0.2,
        split=split,
        node_set=node_set,
    )
    every = [*ids, "node_gone"]
    links: dict[tuple[str, str], LinkStat] = {}
    for src in every:
        for dst in every:
            if src != dst and rng.random() < 0.5:
                age = rng.choice((10.0, 600.0, 7200.0, -60.0, 1e6))
                links[(src, dst)] = LinkStat(
                    bw_bytes_s=_maybe_odd(rng, rng.uniform(1e3, 1e9)),
                    rtt_ms=_maybe_odd(rng, rng.uniform(0.1, 50)),
                    samples=rng.randint(1, 9),
                    measured_at=NOW - dt.timedelta(seconds=age),
                )
    controller_links = {
        n: ControllerLinkStat(
            bw_bytes_s=_maybe_odd(rng, rng.uniform(1e4, 1e9)),
            samples=rng.randint(1, 20),
            last_at=rng.choice((None, NOW - dt.timedelta(hours=2))),
        )
        for n in every
        if rng.random() < 0.5
    }
    env_builds = {
        b: EnvBuildStat(median_s=rng.uniform(1, 400), samples=rng.randint(1, 9))
        for b in _BASES
        if rng.random() < 0.5
    }
    config = PlannerConfig(
        default_bw_bytes_s=rng.choice((50e6, 1e3)),
        default_compute_s=rng.choice((60.0, 5.0)),
        default_env_s=rng.choice((0.0, 30.0)),
        max_attempts_per_node=rng.choice((8, 2)),
        resource_weights=(1.0, rng.choice((1.0, 0.0)), 2.5),
        link_stale_after_s=3600.0,
        min_bw_bytes_s=1024.0,
        split=SplitConfig(split_margin=rng.choice((0.10, 0.0, 0.5))),
    )
    snapshot = ClusterSnapshot(
        nodes=nodes,
        round_robin_after=rng.choice([None, *ids]),
        config=config,
        links=links,
        controller_links=controller_links,
        env_builds=env_builds,
    )
    return request, snapshot, now


def synthetic_fabric(
    n: int, *, split: bool, seed: int = 0
) -> tuple[PlanRequest, ClusterSnapshot, dt.datetime]:
    """``n`` online, able nodes with history, a fresh full network matrix and
    a job (whole or split over 2000 items) - what a latency budget is about."""
    rng = random.Random(seed)  # noqa: S311 - test fabrics, not security
    ids = [f"node_{i:03d}" for i in range(n)]
    tree = _tree(1)
    nodes = tuple(
        NodeSnapshot(
            node_id=node_id,
            state="online",
            connected=True,
            capabilities=frozenset({"exec", "exec-subprocess", "exec-split"}),
            ram_bytes=16 * GIB,
            cpu_threads=8,
            cpu_slots=2,
            cpu_percent=rng.uniform(0, 60),
            ram_free_bytes=8 * GIB,
            disk_free_bytes=100 * GIB,
            cpu_score=rng.uniform(50, 500),
            cached=frozenset({tree}) if i % 3 == 0 else frozenset(),
        )
        for i, node_id in enumerate(ids)
    )
    links = {
        (src, dst): LinkStat(
            bw_bytes_s=rng.uniform(5e6, 1e8),
            rtt_ms=rng.uniform(0.5, 5),
            samples=5,
            measured_at=NOW - dt.timedelta(seconds=60),
        )
        for src in ids
        for dst in ids
        if src != dst
    }
    history: dict[str, Any] = {
        "by_node_s": {node_id: rng.uniform(20, 200) for node_id in ids[: n // 2]},
        "samples_by_node": dict.fromkeys(ids[: n // 2], 5),
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
        trees=(TreeNeed(tree, 200 * 10**6),),
        submitter_node_id=ids[0],
        seeded=True,
        fingerprint="1" * 64,
        history=History(**history),
        split=SplitInfo(items=2000, item_bytes=10**8, items_tree=tree)
        if split
        else None,
    )
    snapshot = ClusterSnapshot(
        nodes=nodes,
        links=links,
        controller_links={
            node_id: ControllerLinkStat(bw_bytes_s=2e7, samples=5) for node_id in ids
        },
    )
    return request, snapshot, NOW
