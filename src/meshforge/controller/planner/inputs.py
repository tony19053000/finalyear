"""Planner inputs from what the controller stores (pure; V0.6 G2, D102/D105).

* :func:`effective_slots` - D105: the node's CPU/GPU slot counts from its Hello
  (``max_cpu_jobs`` / ``max_gpu_jobs``), or the defaults for an agent that did
  not say: 1 CPU slot, and 1 GPU slot only with ``exec-gpu``. A GPU slot needs
  a GPU the node can pass on (``exec-gpu`` and at least one GPU), whatever the
  Hello said.
* :func:`node_snapshot` - a :class:`~.types.NodeSnapshot` from the node row's
  ``hardware`` JSON (proto field names; 64-bit values as JSON strings), its
  stored hardware scores and the live numbers the caller passes in.
* :func:`plan_request` - a :class:`~.types.PlanRequest` from a validated
  manifest and the job's planner fields.
* :func:`workload_fingerprint` - D108: same project, entrypoint and env request
  -> same 64-hex fingerprint (the env *request* key - the controller cannot
  know the agent's env hash).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from meshforge.common.capabilities import EXEC_GPU_CAPABILITY
from meshforge.common.manifest import Manifest
from meshforge.controller.planner.types import (
    History,
    NodeSnapshot,
    PlanRequest,
    TreeNeed,
)

__all__ = [
    "capabilities_of",
    "effective_slots",
    "gpu_vram_of",
    "node_snapshot",
    "plan_request",
    "scores_of",
    "workload_fingerprint",
]

_GIB: Final = 1024**3


def _positive(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def capabilities_of(hardware: Mapping[str, Any]) -> frozenset[str]:
    raw = hardware.get("capabilities")
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(c for c in raw if isinstance(c, str))


def gpu_vram_of(hardware: Mapping[str, Any]) -> tuple[int, ...]:
    """VRAM bytes of each GPU in ``hardware`` (0 = not reported)."""
    gpus = hardware.get("gpus")
    if not isinstance(gpus, list):
        return ()
    return tuple(
        _positive(g.get("vram_bytes")) or 0 for g in gpus if isinstance(g, Mapping)
    )


def effective_slots(
    advertised_cpu: int | None,
    advertised_gpu: int | None,
    *,
    capabilities: frozenset[str],
    gpu_count: int,
) -> tuple[int, int]:
    """``(cpu_slots, gpu_slots)`` (module docstring, D105)."""
    cpu = 1 if advertised_cpu is None else max(0, advertised_cpu)
    can_gpu = EXEC_GPU_CAPABILITY in capabilities and gpu_count > 0
    if not can_gpu:
        return cpu, 0
    gpu = 1 if advertised_gpu is None else max(0, advertised_gpu)
    return cpu, gpu


def scores_of(
    hardware_scores: Mapping[str, Any] | None,
) -> tuple[float | None, float | None]:
    """``(cpu, gpu)``: ``cpu_fp32_gflops_all`` and the best ``gpu_fp32_tflops``
    measured ``ok`` (None when not measured - never 0)."""
    if not hardware_scores:
        return None, None
    metrics = hardware_scores.get("metrics")
    if not isinstance(metrics, list):
        return None, None
    cpu: float | None = None
    gpu: float | None = None
    for metric in metrics:
        if not isinstance(metric, Mapping) or metric.get("status") != "ok":
            continue
        mean = metric.get("mean")
        if not isinstance(mean, int | float) or isinstance(mean, bool) or mean <= 0:
            continue
        if metric.get("name") == "cpu_fp32_gflops_all":
            cpu = float(mean)
        elif metric.get("name") == "gpu_fp32_tflops":
            gpu = float(mean) if gpu is None else max(gpu, float(mean))
    return cpu, gpu


def node_snapshot(
    node_id: str,
    *,
    state: str,
    connected: bool,
    hardware: Mapping[str, Any],
    hardware_scores: Mapping[str, Any] | None = None,
    advertised_cpu_slots: int | None = None,
    advertised_gpu_slots: int | None = None,
    used_cpu_slots: int = 0,
    used_gpu_slots: int = 0,
    live_attempts: int = 0,
    cpu_percent: float | None = None,
    ram_free_bytes: int | None = None,
    disk_free_bytes: int | None = None,
    cached: Iterable[str] = (),
    known_bad_envs: Iterable[str] = (),
) -> NodeSnapshot:
    caps = capabilities_of(hardware)
    vram = gpu_vram_of(hardware)
    cpu_slots, gpu_slots = effective_slots(
        advertised_cpu_slots,
        advertised_gpu_slots,
        capabilities=caps,
        gpu_count=len(vram),
    )
    cpu_score, gpu_score = scores_of(hardware_scores)
    return NodeSnapshot(
        node_id=node_id,
        state=state,
        connected=connected,
        capabilities=caps,
        ram_bytes=_positive(hardware.get("ram_bytes")),
        cpu_threads=_positive(hardware.get("cpu_threads"))
        or _positive(hardware.get("cpu_cores")),
        gpu_vram_bytes=vram,
        cpu_slots=cpu_slots,
        gpu_slots=gpu_slots,
        used_cpu_slots=used_cpu_slots,
        used_gpu_slots=used_gpu_slots,
        live_attempts=live_attempts,
        cpu_percent=cpu_percent,
        ram_free_bytes=ram_free_bytes,
        disk_free_bytes=disk_free_bytes,
        cpu_score=cpu_score,
        gpu_score=gpu_score,
        cached=frozenset(cached),
        known_bad_envs=frozenset(known_bad_envs),
    )


def workload_fingerprint(
    manifest: Manifest, *, project: str | None, env_key: str
) -> str:
    """D108 (module docstring); ``project`` = tree id or bundle digest."""
    data = json.dumps(
        {
            "project": project or "",
            "entrypoint": list(manifest.entrypoint),
            "env": env_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def plan_request(
    manifest: Manifest,
    *,
    job_id: str,
    attempt_no: int,
    env_key: str,
    trees: Sequence[TreeNeed] = (),
    pinned_node_id: str | None = None,
    submitter_node_id: str | None = None,
    seeded: bool = False,
    fingerprint: str | None = None,
    history: History | None = None,
) -> PlanRequest:
    resources = manifest.resources
    return PlanRequest(
        job_id=job_id,
        attempt_no=attempt_no,
        gpu=resources.gpu,
        # ceil: "at least this much" never rounds down below what was asked
        vram_bytes=None
        if resources.vram_gb is None
        else math.ceil(resources.vram_gb * _GIB),
        ram_bytes=None
        if resources.ram_gb is None
        else math.ceil(resources.ram_gb * _GIB),
        cpus=resources.cpus,
        base=manifest.env.base,
        env_key=env_key,
        trees=tuple(trees),
        pinned_node_id=pinned_node_id,
        submitter_node_id=submitter_node_id,
        seeded=seeded,
        fingerprint=fingerprint,
        history=history or History(),
    )
