"""Pinned placement check for ``POST /v1/jobs`` (brief D50; V0.6 D102/D103).

Since V0.6 the rules live in the planner's feasibility filter
(:mod:`meshforge.controller.planner.feasibility`); this module is the thin
wrapper the *pinned* submit path keeps (``placement.node_id`` given = a
candidate set of one, with exactly its V0.4 behaviour). Pure: given the
manifest, the node's registry record and whether this controller holds a live
stream to it, either return nothing (the job may go there) or raise
:class:`PlacementError` with a typed ``failure_type`` and a short, safe reason:

* ``not_found`` - no such node;
* ``no_feasible_plan`` - the node is not ONLINE, has no live session, or the
  planner rejects it statically: no ``exec`` capability (its owner has not
  enabled execution, D45), cannot give a GPU to a job that requires one (no
  GPU, no ``exec-gpu``, or the owner allows no GPU jobs), reports less VRAM /
  RAM / CPU threads than the manifest asks for, has no runtime for the job's
  base, or allows no jobs of its kind (D105 slots).

Hardware numbers come from the node's last ``Hello.hardware`` (proto field names,
64-bit values as JSON strings); a value the node did not report is not held
against it. The check runs **before** any row is written, so a refused request
leaves no job behind. Busy slots are not checked here: a pinned job waits for
its node in the queue (D65/D105).
"""

from __future__ import annotations

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.manifest import Manifest
from meshforge.controller.jobs.env_health import env_key
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner.feasibility import static_rejection
from meshforge.controller.planner.inputs import node_snapshot, plan_request
from meshforge.controller.registry import NodeRecord

__all__ = ["PlacementError", "check_placement"]


class PlacementError(MeshForgeError):
    default_failure_type = FailureType.NO_FEASIBLE_PLAN


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
    snapshot = node_snapshot(
        node.node_id,
        state=node.state.value,
        connected=connected,
        hardware=node.hardware,
        hardware_scores=node.hardware_scores,
        advertised_cpu_slots=node.cpu_slots,
        advertised_gpu_slots=node.gpu_slots,
    )
    request = plan_request(
        manifest,
        job_id="",
        attempt_no=1,
        env_key=env_key(manifest.canonical_dict(), tree_id=None, bundle_digest=None),
        pinned_node_id=node.node_id,
    )
    refused = static_rejection(request, snapshot)
    if refused is not None:
        raise PlacementError(refused.text, details={"reject_code": refused.code.value})
