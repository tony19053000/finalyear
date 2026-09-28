"""Manual placement validation for ``POST /v1/jobs`` (brief D50; the planner is V0.6).

Pure: given the manifest, the node's registry record and whether this controller
holds a live stream to it, either return nothing (the job may go there) or raise
:class:`PlacementError` with a typed ``failure_type`` and a short, safe reason:

* ``not_found`` - no such node;
* ``no_feasible_plan`` - the node is not ONLINE, has no live session, does not
  accept jobs (no ``exec`` capability: its owner has not enabled execution, D45),
  cannot give a GPU to a job that requires one (no GPU or no ``exec-gpu``), or
  reports less VRAM / RAM / CPU threads than the manifest asks for.

Hardware numbers come from the node's last ``Hello.hardware`` (proto field names,
64-bit values as JSON strings); a value the node did not report is not held
against it. The check runs **before** any row is written, so a refused request
leaves no job behind.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from meshforge.common.capabilities import EXEC_CAPABILITY, EXEC_GPU_CAPABILITY
from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.manifest import Manifest
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import NodeRecord

__all__ = ["PlacementError", "check_placement"]

_GIB: Final = 1024**3


class PlacementError(MeshForgeError):
    default_failure_type = FailureType.NO_FEASIBLE_PLAN


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _gpus(hardware: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    gpus = hardware.get("gpus")
    return [g for g in gpus if isinstance(g, Mapping)] if isinstance(gpus, list) else []


def check_placement(
    manifest: Manifest, node: NodeRecord | None, *, connected: bool
) -> None:
    """Raise :class:`PlacementError` unless ``node`` can take this job now."""
    if node is None:
        raise PlacementError("no such node", failure_type=FailureType.NOT_FOUND)
    if node.state is not NodeState.ONLINE:
        raise PlacementError(f"the node is {node.state.value}, not online")
    if not connected:
        raise PlacementError("the node has no live session with this controller")
    hardware = node.hardware
    raw_caps = hardware.get("capabilities")
    caps = (
        {c for c in raw_caps if isinstance(c, str)}
        if isinstance(raw_caps, list)
        else set()
    )
    if EXEC_CAPABILITY not in caps:
        raise PlacementError(
            "the node does not accept jobs (its owner has not enabled "
            "[agent.execution])"
        )
    resources = manifest.resources
    gpus = _gpus(hardware)
    if resources.gpu == "required":
        if not gpus:
            raise PlacementError("the job requires a GPU and the node has none")
        if EXEC_GPU_CAPABILITY not in caps:
            raise PlacementError(
                "the job requires a GPU and the node cannot pass one to a job "
                "(no GPU-capable runtime)"
            )
        if resources.vram_gb is not None:
            vram = max((_int(g.get("vram_bytes")) or 0) for g in gpus)
            if vram and vram < resources.vram_gb * _GIB:
                raise PlacementError(
                    f"the job needs {resources.vram_gb:g} GB of VRAM; the node's "
                    f"largest GPU has {vram / _GIB:.1f} GB"
                )
    ram = _int(hardware.get("ram_bytes"))
    if (
        resources.ram_gb is not None
        and ram is not None
        and ram < resources.ram_gb * _GIB
    ):
        raise PlacementError(
            f"the job needs {resources.ram_gb:g} GB of RAM; the node has "
            f"{ram / _GIB:.1f} GB"
        )
    threads = _int(hardware.get("cpu_threads")) or _int(hardware.get("cpu_cores"))
    if resources.cpus is not None and threads is not None and threads < resources.cpus:
        raise PlacementError(
            f"the job needs {resources.cpus:g} CPUs; the node has {threads}"
        )
