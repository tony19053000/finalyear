"""``ResourceSample`` -> ``ResourceUpdate``, with the D12 clipping applied.

The monitor never builds the proto message inline: a report leaves the agent
through exactly one function, so the bounds the controller re-checks
(:func:`meshforge.controller.session.limits.check_resource_update`) are applied in
one place. ``sequence`` is **not** set here - the session client stamps it when the
report enters the replay ring (G3 contract).

Clipping, not rejecting: an over-long mount path or a 17th GPU costs that entry,
never the report. The controller applies the same rule for the same reason (D12).
"""

from __future__ import annotations

from meshforge.agent.hardware.limits import (
    MAX_DISKS,
    MAX_GPUS,
    MOUNT_MAX_LENGTH,
)
from meshforge.agent.hardware.probe import clip
from meshforge.agent.resources.sampler import ResourceSample
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = ["to_resource_update"]

_PERCENT_MAX = 100.0


def _percent(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    return min(max(value, 0.0), _PERCENT_MAX)


def to_resource_update(
    sample: ResourceSample, *, max_gpus: int = MAX_GPUS, max_disks: int = MAX_DISKS
) -> agent_pb2.ResourceUpdate:
    """The wire message for ``sample``; ``sequence`` is left at 0 for the client."""
    update = agent_pb2.ResourceUpdate(
        cpu_percent=_percent(sample.cpu_percent),
        ram_used_bytes=max(sample.ram_used_bytes, 0),
        ram_total_bytes=max(sample.ram_total_bytes, 0),
    )
    update.observed_at.FromDatetime(sample.observed_at)
    for gpu in sample.gpus[: min(max_gpus, MAX_GPUS)]:
        update.gpus.append(
            agent_pb2.ResourceUpdate.GpuUsage(
                index=gpu.index,
                util_percent=_percent(gpu.util_percent),
                vram_used_bytes=max(gpu.vram_used_bytes, 0),
                vram_total_bytes=max(gpu.vram_total_bytes, 0),
            )
        )
    for disk in sample.disks[: min(max_disks, MAX_DISKS)]:
        update.disks.append(
            agent_pb2.ResourceUpdate.DiskUsage(
                mount=clip(disk.mount, MOUNT_MAX_LENGTH),
                free_bytes=max(disk.free_bytes, 0),
                total_bytes=max(disk.total_bytes, 0),
            )
        )
    return update
