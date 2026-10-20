"""What the controller sends with ``StartAttempt`` and records at dispatch (V0.4 G4).

* :func:`choose_gpus` - the NVML indices a job may use on its node: none for
  ``resources.gpu: none``, otherwise the node's GPU with the most VRAM among
  those no live attempt holds (one GPU per job in V0.4; one job per GPU since
  the V0.6 G2 fix D-2; Mode 4 is where several GPUs serve one program).
* :func:`start_snapshot` - the **start snapshot** (roadmap 0.4.12, ARCHITECTURE
  §17): the node's latest live resource sample and stored hardware scores at
  dispatch, plus the GPU choice - what V0.9's telemetry and V0.15's model learn
  "under which conditions" from. JSON types only (it is a JSONB column).
* :func:`execution_spec` - the ``ExecutionSpec`` (D52) built from the job's
  canonical manifest and the attempt's snapshot, so a re-sent ``StartAttempt``
  (D66) carries exactly what the first one did (only the fetch token is new).
  A tree job (V0.5 D83) carries ``tree_id`` (and an empty ``bundle_digest``)
  and its blob ``sources``: the controller first, with the attempt's fetch token
  (scoped to the tree); the dispatcher appends the peers holding the tree with
  their own signed tokens (V0.5 G4, D89/D98/D100). V0.7 G5 (D142): it names
  the job owner (``owner_name``, recorded in the start snapshot) so the agent
  can apply its owner's ``allowed_users``.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final

from meshforge.common.clock_offset import ClockOffset, round_up
from meshforge.common.manifest import Manifest
from meshforge.common.owner_policy import OwnerPolicy
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import AttemptRole
from meshforge.controller.jobs.store import AttemptRecord, JobRecord
from meshforge.controller.planner.inputs import gpu_indices_of, gpu_vram_of
from meshforge.controller.planner.types import (
    ControllerLinkStat,
    LinkStat,
    NodeSnapshot,
    TreeNeed,
)
from meshforge.controller.resources import LiveSample

__all__ = [
    "SNAPSHOT_SAMPLE_EXCLUDED",
    "SNAPSHOT_SAMPLE_FIELDS",
    "SNAPSHOT_VERSION",
    "TELEMETRY_VERSION",
    "choose_gpus",
    "combine_manifest",
    "execution_spec",
    "start_snapshot",
    "start_telemetry",
]

SNAPSHOT_VERSION: Final = 1
_GIB: Final = 1024**3


def choose_gpus(
    manifest: Manifest,
    hardware: Mapping[str, Any],
    busy: Collection[int] = (),
) -> tuple[int, ...]:
    """``()`` or the index of the node's largest GPU that no live attempt
    holds (``busy``); ``()`` too when every GPU is held (see module docstring)."""
    if manifest.resources.gpu == "none":
        return ()
    candidates = [
        (vram, -index, index)
        for index, vram in zip(
            gpu_indices_of(hardware), gpu_vram_of(hardware), strict=True
        )
        if index not in busy
    ]
    if not candidates:
        return ()
    return (max(candidates)[2],)


#: The :class:`LiveSample` fields the start snapshot copies (V0.9 G1, D226
#: item 0.1): an explicit ALLOWLIST, never ``dataclasses.asdict``, so a field
#: added to ``LiveSample`` later (G0's ``received_wall`` datetime, f6fe6f9) stays
#: out of this JSON column until someone decides how to serialise it.
SNAPSHOT_SAMPLE_FIELDS: Final = (
    "session_epoch",
    "sequence",
    "observed_at",
    "cpu_percent",
    "ram_used_bytes",
    "ram_total_bytes",
    "gpus",
    "disks",
)
#: Left out on purpose: ``received_at`` is the controller's monotonic clock
#: (meaningless outside this process; ``age_s`` replaces it), ``received_wall``
#: is the live event's ``recv_at`` (a datetime; ``age_s`` already says how old
#: the sample is). A unit test fails when ``LiveSample`` gains a field that is
#: in neither tuple.
SNAPSHOT_SAMPLE_EXCLUDED: Final = ("received_at", "received_wall")


def _sample(sample: LiveSample | None, now: float) -> dict[str, Any] | None:
    if sample is None:
        return None
    return {
        "session_epoch": int(sample.session_epoch),
        "sequence": int(sample.sequence),
        "observed_at": sample.observed_at.isoformat(),
        "cpu_percent": float(sample.cpu_percent),
        "ram_used_bytes": int(sample.ram_used_bytes),
        "ram_total_bytes": int(sample.ram_total_bytes),
        "gpus": [
            {
                "index": int(gpu.index),
                "util_percent": float(gpu.util_percent),
                "vram_used_bytes": int(gpu.vram_used_bytes),
                "vram_total_bytes": int(gpu.vram_total_bytes),
            }
            for gpu in sample.gpus
        ],
        "disks": [
            {
                "mount": str(disk.mount),
                "free_bytes": int(disk.free_bytes),
                "total_bytes": int(disk.total_bytes),
            }
            for disk in sample.disks
        ],
        "age_s": round(max(0.0, now - sample.received_at), 3),
    }


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
    owner_name: str | None = None,
    policy: OwnerPolicy | None = None,
    telemetry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON start snapshot (see module docstring). V0.7 G5 (D142):
    ``owner_name`` is the job owner's user name the ``StartAttempt`` carries
    (the agent checks ``allowed_users``), ``owner_policy`` the node's
    advertised policy at dispatch (conditions, like the sample)."""
    return {
        "owner_name": owner_name,
        "owner_policy": None if policy is None else policy.as_dict(),
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
        "telemetry": None if telemetry is None else dict(telemetry),
    }


TELEMETRY_VERSION: Final = 1


def start_telemetry(
    *,
    node: NodeSnapshot | None,
    trees: Sequence[TreeNeed],
    submitter_node_id: str | None,
    node_id: str,
    links: Mapping[tuple[str, str], LinkStat],
    controller_links: Mapping[str, ControllerLinkStat],
    clock: ClockOffset | None,
) -> dict[str, Any]:
    """V0.9 G1 (D212): what the run record needs to know **at start** and the
    tables will have forgotten by the end - the node's queue depth, which of
    the job's trees it held whole (warm/cold), the network row to it (the
    submitter -> node peer link, else the controller -> node history) and its
    clock offset. Plain JSON (numbers rounded, no datetimes)."""
    cached = frozenset() if node is None else node.cached
    network: dict[str, Any] | None = None
    peer = (
        links.get((node_id, submitter_node_id))
        if submitter_node_id and submitter_node_id != node_id
        else None
    )
    if peer is not None:
        network = {
            "source": "peer",
            "rtt_ms": round(peer.rtt_ms, 3),
            "bw_bytes_s": round(peer.bw_bytes_s, 1),
            "samples": int(peer.samples),
        }
    elif (ctl := controller_links.get(node_id)) is not None:
        network = {
            "source": "controller",
            "rtt_ms": None,
            "bw_bytes_s": round(ctl.bw_bytes_s, 1),
            "samples": int(ctl.samples),
        }
    return {
        "version": TELEMETRY_VERSION,
        "node_live_attempts": None if node is None else int(node.live_attempts),
        "cache": [
            {"tree_id": t.key, "bytes": int(t.size), "warm": t.key in cached}
            for t in trees
            if t.key
        ],
        "network": network,
        "clock": None
        if clock is None
        else {
            "clock_offset_ms": round(clock.offset_ms, 3),
            "clock_offset_bound_ms": round_up(clock.bound_ms, 3),
            "consistent": clock.consistent,
            "exchanges": clock.exchanges,
        },
    }


def combine_manifest(manifest: Manifest) -> Manifest:
    """V0.10 G5 (D268): what a split job's **combine** asks of a node - the
    job's manifest without its GPU need (combining files needs no GPU; the
    lanes had it). Everything else (owner, env base, network, limits) holds."""
    resources = manifest.resources.model_copy(update={"gpu": "none", "vram_gb": None})
    return manifest.model_copy(update={"resources": resources})


def execution_spec(
    job: JobRecord,
    attempt: AttemptRecord,
    *,
    fetch_token: str,
    blob_url: str,
    blob_port: int,
) -> agent_pb2.ExecutionSpec:
    """The ``ExecutionSpec`` of ``attempt`` (see module docstring; a
    combine attempt gets :func:`combine_manifest`)."""
    manifest = Manifest.model_validate(dict(job.manifest))
    if attempt.role is AttemptRole.COMBINE:
        manifest = combine_manifest(manifest)
    snapshot = attempt.start_snapshot or {}
    gpus = snapshot.get("gpu_indices") or []
    resources = manifest.resources
    return agent_pb2.ExecutionSpec(
        bundle_digest=job.bundle_digest or "",
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
        tree_id=job.tree_id or "",
        owner_name=str(snapshot.get("owner_name") or ""),
        sources=[agent_pb2.BlobSource(url=blob_url, token=fetch_token, node_id="")]
        if job.tree_id
        else [],
    )
