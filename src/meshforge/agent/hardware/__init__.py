"""Hardware discovery for the agent (roadmap 0.3.3).

Public API only; the probes live in the per-source modules (``cpu``, ``gpu``,
``disks``, ``software``) so no single file grows into the 800-line ``probes.py``
the 0.2.2 review complained about.

Typical use (the agent does exactly this once, at start-up)::

    facts = await asyncio.to_thread(HardwareProvider(timeout_s=5.0).get)
    hello_hardware = facts.to_summary()

Everything here is blocking and none of it raises: see
:mod:`meshforge.agent.hardware.summary`.
"""

from __future__ import annotations

from meshforge.agent.hardware.cpu import CpuFacts
from meshforge.agent.hardware.disks import DiskFacts
from meshforge.agent.hardware.gpu import GpuFacts, GpuUsage, NvmlSession
from meshforge.agent.hardware.limits import clip_capabilities, clip_hardware
from meshforge.agent.hardware.probe import ProbeOutcome, ProbeStatus
from meshforge.agent.hardware.summary import (
    DEFAULT_TIMEOUT_S,
    SHAPE_CAPABILITY,
    HardwareFacts,
    HardwareProvider,
    discover,
)

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "SHAPE_CAPABILITY",
    "CpuFacts",
    "DiskFacts",
    "GpuFacts",
    "GpuUsage",
    "HardwareFacts",
    "HardwareProvider",
    "NvmlSession",
    "ProbeOutcome",
    "ProbeStatus",
    "clip_capabilities",
    "clip_hardware",
    "discover",
]
