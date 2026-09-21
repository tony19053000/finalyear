"""Agent-side bounds on everything this node puts on the wire (V0.3 G4, D12).

The rule is "clip before you send": the agent never builds a message its own peer
would have to reject, so a laptop with a pathological driver string cannot turn its
own Hello into an ``INVALID_ARGUMENT`` and strand the node. The controller re-checks
the same caps (:mod:`meshforge.controller.session.limits`) because a peer is a peer -
the two copies are deliberate duplication (the agent never imports the controller),
and ``test_the_agent_and_controller_caps_are_the_same_numbers`` in
``tests/unit/controller/test_resource_limits.py`` pins that they agree.

Two different failure policies, and they are not symmetric (D12):

* ``Hello.hardware`` is the enrolment contract - over the cap the controller
  refuses the Hello and persists nothing, so the agent must clip.
* ``ResourceUpdate`` is a periodic report - over the cap the controller *clamps*
  and counts, never closes a live session for it.
"""

from __future__ import annotations

from typing import Final

from meshforge.agent.hardware.probe import clip
from meshforge.agent.session.hello import CAPABILITIES_V03
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = [
    "CAPABILITY_MAX_LENGTH",
    "CPU_MODEL_MAX_LENGTH",
    "GPU_COMPUTE_CAPABILITY_MAX_LENGTH",
    "GPU_DRIVER_MAX_LENGTH",
    "GPU_MODEL_MAX_LENGTH",
    "HARDWARE_SUMMARY_MAX_BYTES",
    "MAX_CAPABILITIES",
    "MAX_DISKS",
    "MAX_GPUS",
    "MOUNT_MAX_LENGTH",
    "clip_capabilities",
    "clip_hardware",
]

CPU_MODEL_MAX_LENGTH: Final = 128
"""``HardwareSummary.cpu_model``."""

GPU_MODEL_MAX_LENGTH: Final = 128
"""``HardwareSummary.Gpu.model``."""

GPU_DRIVER_MAX_LENGTH: Final = 64
"""``HardwareSummary.Gpu.driver_version``."""

GPU_COMPUTE_CAPABILITY_MAX_LENGTH: Final = 16
"""``HardwareSummary.Gpu.compute_capability`` (``"8.6"`` with room to spare)."""

MAX_GPUS: Final = 16
"""Devices reported per node; also the cap on ``ResourceUpdate.gpus``."""

MAX_CAPABILITIES: Final = 32
"""Tags in ``HardwareSummary.capabilities``."""

CAPABILITY_MAX_LENGTH: Final = 64
"""One capability tag."""

MAX_DISKS: Final = 16
"""Entries in ``ResourceUpdate.disks`` (and in ``HardwareFacts.disks``)."""

MOUNT_MAX_LENGTH: Final = 128
"""``ResourceUpdate.DiskUsage.mount``."""

HARDWARE_SUMMARY_MAX_BYTES: Final = 16 * 1024
"""Backstop on the serialized ``HardwareSummary``: the per-field caps already
bound it far below this, so hitting it means a field grew without its cap."""


def clip_capabilities(tags: list[str]) -> list[str]:
    """Sorted, de-duplicated, clipped tags - at most :data:`MAX_CAPABILITIES`.

    The shape marker (:data:`CAPABILITIES_V03` ``[0]``, ``"v0.3"``) keeps the first
    position when present: G3 documented it as the marker of the Hello *shape*, so
    it must survive the truncation of a node that somehow reported 40 tags. It is
    read from ``session.hello`` - one wire value, one constant (L1).
    """
    cleaned = {clip(tag.strip(), CAPABILITY_MAX_LENGTH) for tag in tags if tag.strip()}
    marker = [tag for tag in CAPABILITIES_V03[:1] if tag in cleaned]
    rest = sorted(cleaned - set(marker))
    return [*marker, *rest][:MAX_CAPABILITIES]


def clip_hardware(summary: agent_pb2.HardwareSummary) -> agent_pb2.HardwareSummary:
    """A copy of ``summary`` inside every cap above (never mutates the input).

    The byte backstop is applied last and, if it ever engages, drops GPU entries
    from the end until the message fits - a truncated device list is a degraded
    field, a refused Hello is a node that cannot join.
    """
    out = agent_pb2.HardwareSummary(
        cpu_model=clip(summary.cpu_model, CPU_MODEL_MAX_LENGTH),
        cpu_cores=summary.cpu_cores,
        cpu_threads=summary.cpu_threads,
        ram_bytes=summary.ram_bytes,
        capabilities=clip_capabilities(list(summary.capabilities)),
    )
    for gpu in list(summary.gpus)[:MAX_GPUS]:
        out.gpus.append(
            agent_pb2.HardwareSummary.Gpu(
                index=gpu.index,
                model=clip(gpu.model, GPU_MODEL_MAX_LENGTH),
                vram_bytes=gpu.vram_bytes,
                driver_version=clip(gpu.driver_version, GPU_DRIVER_MAX_LENGTH),
                compute_capability=clip(
                    gpu.compute_capability, GPU_COMPUTE_CAPABILITY_MAX_LENGTH
                ),
            )
        )
    while out.ByteSize() > HARDWARE_SUMMARY_MAX_BYTES and out.gpus:
        del out.gpus[-1]
    while out.ByteSize() > HARDWARE_SUMMARY_MAX_BYTES and out.capabilities:
        del out.capabilities[-1]
    return out
