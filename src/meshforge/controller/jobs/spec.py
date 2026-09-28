"""What the controller sends with ``StartAttempt`` and records at dispatch (V0.4 G4).

* :func:`choose_gpus` - the NVML indices a job may use on its node: none for
  ``resources.gpu: none``, otherwise the node's GPU with the most VRAM (one GPU
  per job in V0.4; Mode 4 is where several GPUs serve one program).
* :func:`start_snapshot` - the **start snapshot** (roadmap 0.4.12, ARCHITECTURE
  §17): the node's latest live resource sample and stored hardware scores at
  dispatch, plus the GPU choice - what V0.9's telemetry and V0.15's model learn
  "under which conditions" from. JSON types only (it is a JSONB column).
* :func:`execution_spec` - the ``ExecutionSpec`` (D52) built from the job's
  canonical manifest and the attempt's snapshot, so a re-sent ``StartAttempt``
  (D66) carries exactly what the first one did (only the fetch token is new).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

from meshforge.common.manifest import Manifest
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.jobs.store import AttemptRecord, JobRecord
from meshforge.controller.resources import LiveSample

__all__ = ["SNAPSHOT_VERSION", "choose_gpus", "execution_spec", "start_snapshot"]

SNAPSHOT_VERSION: Final = 1
_GIB: Final = 1024**3


def _int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def choose_gpus(manifest: Manifest, hardware: Mapping[str, Any]) -> tuple[int, ...]:
    """``()`` or the index of the node's largest GPU (see module docstring)."""
    if manifest.resources.gpu == "none":
        return ()
    gpus = hardware.get("gpus")
    if not isinstance(gpus, list):
        return ()
    candidates = [
        (_int(g.get("vram_bytes")), -_int(g.get("index")), _int(g.get("index")))
        for g in gpus
        if isinstance(g, Mapping)
    ]
    if not candidates:
        return ()
    return (max(candidates)[2],)


def _sample(sample: LiveSample | None, now: float) -> dict[str, Any] | None:
    if sample is None:
        return None
    data = dataclasses.asdict(sample)
    data["observed_at"] = sample.observed_at.isoformat()
    data["age_s"] = round(max(0.0, now - sample.received_at), 3)
    del data["received_at"]  # monotonic: meaningless outside this process
    return data


def start_snapshot(
    *,
    hardware: Mapping[str, Any],
    hardware_scores: Mapping[str, Any] | None,
    node_state: str,
    rtt_ms: float | None,
    sample: LiveSample | None,
    monotonic_now: float,
    wall_now: dt.datetime,
    gpu_indices: tuple[int, ...],
) -> dict[str, Any]:
    """The JSON start snapshot (see module docstring)."""
    return {
        "version": SNAPSHOT_VERSION,
        "taken_at": wall_now.isoformat(),
        "node": {
            "state": node_state,
            "rtt_ms": None if rtt_ms is None else round(rtt_ms, 3),
            "capabilities": list(hardware.get("capabilities") or []),
        },
        "resources": _sample(sample, monotonic_now),
        "scores": None if hardware_scores is None else dict(hardware_scores),
        "gpu_indices": list(gpu_indices),
    }


def execution_spec(
    job: JobRecord,
    attempt: AttemptRecord,
    *,
    fetch_token: str,
    blob_url: str,
    blob_port: int,
) -> agent_pb2.ExecutionSpec:
    """The ``ExecutionSpec`` of ``attempt`` (see module docstring)."""
    manifest = Manifest.model_validate(dict(job.manifest))
    snapshot = attempt.start_snapshot or {}
    gpus = snapshot.get("gpu_indices") or []
    resources = manifest.resources
    return agent_pb2.ExecutionSpec(
        bundle_digest=job.bundle_digest,
        blob_url=blob_url,
        blob_port=blob_port,
        fetch_token=fetch_token,
        manifest_json=manifest.canonical_json(),
        runtime="",
        memory_bytes=0 if resources.ram_gb is None else int(resources.ram_gb * _GIB),
        cpus=0.0 if resources.cpus is None else float(resources.cpus),
        gpu_indices=[int(i) for i in gpus],
        timeout_s=int(manifest.limits.timeout),
        output_globs=list(manifest.outputs),
    )
