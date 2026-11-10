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
  planner rejects it statically: no ``exec`` capability (its agent has no
  runtime, D45/D428), cannot give a GPU to a job that requires one (no
  GPU, no ``exec-gpu``, or the owner allows no GPU jobs), reports less VRAM /
  RAM / CPU threads than the manifest asks for, has no runtime for the job's
  base, or allows no jobs of its kind (D105 slots), or (V0.7 G5, D142) its
  owner's live policy refuses the job (``policy_*`` codes: not accepting, the
  submitter not in ``allowed_users``, RAM/CPU/VRAM over the per-job limit, a
  network or an unsandboxed runtime the owner does not allow).

The slot limits are the node's live ones when a session gives them (``slots``:
``LiveSession.policy_slots``, set by every ``PolicyUpdate`` - V1.0 G8 F5: the
owner turning the GPU switch on or off after the Hello counts at submit, as it
does in the planner), else the Hello's.

Hardware numbers come from the node's last ``Hello.hardware`` (proto field names,
64-bit values as JSON strings); a value the node did not report is not held
against it. The check runs **before** any row is written, so a refused request
leaves no job behind. Busy slots are not checked here: a pinned job waits for
its node in the queue (D65/D105).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.manifest import Manifest
from meshforge.common.owner_policy import OwnerPolicy
from meshforge.controller.jobs.env_health import env_key
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner.feasibility import static_rejection
from meshforge.controller.planner.inputs import node_snapshot, plan_request
from meshforge.controller.registry import NodeRecord

__all__ = ["MAX_REJECTIONS_SHOWN", "PlacementError", "check_auto", "check_placement"]

MAX_REJECTIONS_SHOWN: Final = 64

Slots = tuple[int | None, int | None]
"""``(max_cpu_jobs, max_gpu_jobs)`` as a session holds them (None = not sent)."""


def _slots(node: NodeRecord, live: Slots | None) -> Slots:
    return (node.cpu_slots, node.gpu_slots) if live is None else live


class PlacementError(MeshForgeError):
    default_failure_type = FailureType.NO_FEASIBLE_PLAN


def check_placement(
    manifest: Manifest,
    node: NodeRecord | None,
    *,
    connected: bool,
    policy: OwnerPolicy | None = None,
    owner_name: str | None = None,
    slots: Slots | None = None,
) -> None:
    """Raise :class:`PlacementError` unless ``node`` can take this job now.
    ``slots``: the session's live slot limits (module docstring)."""
    if node is None:
        raise PlacementError("no such node", failure_type=FailureType.NOT_FOUND)
    if node.state is not NodeState.ONLINE:
        raise PlacementError(f"the node is {node.state.value}, not online")
    if not connected:
        raise PlacementError("the node has no live session with this controller")
    cpu_slots, gpu_slots = _slots(node, slots)
    snapshot = node_snapshot(
        node.node_id,
        state=node.state.value,
        connected=connected,
        hardware=node.hardware,
        hardware_scores=node.hardware_scores,
        advertised_cpu_slots=cpu_slots,
        advertised_gpu_slots=gpu_slots,
        policy=policy,
    )
    request = plan_request(
        manifest,
        job_id="",
        attempt_no=1,
        env_key=env_key(manifest.canonical_dict(), tree_id=None, bundle_digest=None),
        pinned_node_id=node.node_id,
        owner_name=owner_name,
    )
    refused = static_rejection(request, snapshot)
    if refused is not None:
        raise PlacementError(refused.text, details={"reject_code": refused.code.value})


def check_auto(
    manifest: Manifest,
    nodes: Sequence[NodeRecord],
    *,
    policies: Mapping[str, OwnerPolicy | None] | None = None,
    owner_name: str | None = None,
    slots: Mapping[str, Slots | None] | None = None,
    exclude_nodes: Sequence[str] = (),
) -> None:
    """D104: an auto-placed job must be runnable on at least one enrolled node
    **some day** - the static rules only (capabilities, hardware, owner slot
    settings, not revoked; liveness and load are ignored: a busy or offline
    fabric makes the job wait in the queue, it does not refuse it). Raises
    :class:`PlacementError` whose ``details["rejections"]`` lists every node's
    reason (at most :data:`MAX_REJECTIONS_SHOWN`)."""
    request = plan_request(
        manifest,
        job_id="",
        attempt_no=1,
        env_key=env_key(manifest.canonical_dict(), tree_id=None, bundle_digest=None),
        owner_name=owner_name,
        exclude_nodes=exclude_nodes,  # V1.0 G9-B: excluded_by_user
    )
    policies = policies or {}
    live_slots = slots or {}
    rejections: list[dict[str, str]] = []
    for node in sorted(nodes, key=lambda n: n.node_id):
        cpu_slots, gpu_slots = _slots(node, live_slots.get(node.node_id))
        snapshot = node_snapshot(
            node.node_id,
            state=node.state.value,
            connected=False,
            hardware=node.hardware,
            hardware_scores=node.hardware_scores,
            advertised_cpu_slots=cpu_slots,
            advertised_gpu_slots=gpu_slots,
            policy=policies.get(node.node_id),
        )
        refused = static_rejection(request, snapshot)
        if refused is None:
            return
        rejections.append(
            {
                "node_id": node.node_id,
                "reject_code": refused.code.value,
                "reject_text": refused.text,
            }
        )
    if not rejections:
        message = "no node has joined the fabric yet; start an agent first"
    else:
        message = (
            f"none of the {len(rejections)} node(s) of the fabric can ever run this "
            "job; see rejections"
        )
    raise PlacementError(
        message, details={"rejections": rejections[:MAX_REJECTIONS_SHOWN]}
    )
