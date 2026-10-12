"""Stage 2: can this node run this job - ever, and now? (V0.6 G2, D102/D104/D105).

The V0.4 manual-placement rules (``controller/jobs/placement.py``) live here
now, extended with the planner's: slots, the node cap, owner settings, the
runtime a base needs, recent env failures and free disk.

* :func:`static_rejection` - what the node **is**: execution enabled, a GPU and
  a runtime that can pass it on, enough VRAM / RAM / CPU threads, a runtime for
  the job's base, owner slot settings that allow the job's kind at all, not
  revoked. A job that every known node rejects statically can never run: it is
  refused at submit (``409 no_feasible_plan``) or failed in the queue (D104).
* :func:`live_rejection` - the **moment**: ONLINE with a live session, not
  excluded for this retry (V0.6 G3, D110), the environment not known-bad here
  (D116), a free slot of a kind the job can use, under the node cap, enough
  free disk for what it would fetch.
* :func:`choose_slot` - which slot a job takes (D105): ``gpu: required`` a GPU
  slot; ``preferred`` a GPU slot when one is free and usable, else a CPU slot;
  ``none`` a CPU slot.

Every rejection is one :class:`~.types.Rejection` with a stable code and a
short text of our own (checklist 18). A value the node did not report is never
held against it (unknown RAM is not "too little RAM").
"""

from __future__ import annotations

from typing import Final

from meshforge.common import owner_policy
from meshforge.common.capabilities import (
    EXEC_CAPABILITY,
    EXEC_DOCKER_CAPABILITY,
    EXEC_GPU_CAPABILITY,
    EXEC_SUBPROCESS_CAPABILITY,
)
from meshforge.controller.planner.types import (
    NodeSnapshot,
    PlanRequest,
    RejectCode,
    Rejection,
    Slot,
    TreeNeed,
)

__all__ = [
    "EXEC_VENV_CAPABILITY",
    "choose_slot",
    "live_rejection",
    "missing_bytes",
    "missing_trees",
    "policy_rejection",
    "rejection",
    "static_rejection",
]

_GIB: Final = 1024**3
EXEC_VENV_CAPABILITY: Final = "exec-venv"
"""The agent's venv runtime tag (``agent/execution/selection.py``; the wire
spelling - the controller only reads it here)."""
_BASE_NEEDS_FULL_RUNTIME: Final = frozenset({"cuda-pytorch"})
"""Bases the venv runtime cannot provide (selection.py ``VENV_BASES``)."""
_REVOKED: Final = "revoked"
_ONLINE: Final = "online"


def _gpu_usable(node: NodeSnapshot) -> bool:
    return (
        bool(node.gpu_vram_bytes)
        and EXEC_GPU_CAPABILITY in node.capabilities
        and node.gpu_slots > 0
    )


def static_rejection(request: PlanRequest, node: NodeSnapshot) -> Rejection | None:
    """The first static reason ``node`` can never run ``request`` (or None)."""
    caps = node.capabilities
    if node.state == _REVOKED:
        return Rejection(RejectCode.REVOKED, "the node was revoked from the fabric")
    if EXEC_CAPABILITY not in caps:
        return Rejection(
            RejectCode.NOT_EXEC,
            "the node does not accept jobs (its owner has not enabled "
            "[agent.policy] accept_jobs)",
        )
    refused = policy_rejection(request, node)
    if refused is not None:
        return refused
    if request.gpu == "required":
        if not node.gpu_vram_bytes:
            return Rejection(
                RejectCode.NO_GPU, "the job requires a GPU and the node has none"
            )
        if EXEC_GPU_CAPABILITY not in caps:
            return Rejection(
                RejectCode.NO_GPU_RUNTIME,
                "the job requires a GPU and the node cannot pass one to a job "
                "(no GPU-capable runtime)",
            )
        if node.gpu_slots <= 0:
            return Rejection(
                RejectCode.NO_GPU_SLOTS,
                "the node's owner allows no GPU jobs ([agent.execution] "
                "max_gpu_jobs = 0)",
            )
        if request.vram_bytes is not None:
            vram = max(node.gpu_vram_bytes)
            if vram and vram < request.vram_bytes:
                return Rejection(
                    RejectCode.VRAM_SHORT,
                    f"the job needs {request.vram_bytes / _GIB:g} GB of VRAM; the "
                    f"node's largest GPU has {vram / _GIB:.1f} GB",
                )
    elif request.gpu == "preferred":
        if node.cpu_slots <= 0 and not _gpu_usable(node):
            return Rejection(
                RejectCode.NO_CPU_SLOTS,
                "the node's owner allows no CPU jobs and it cannot give this job a GPU",
            )
    elif node.cpu_slots <= 0:
        return Rejection(
            RejectCode.NO_CPU_SLOTS,
            "the node's owner allows no CPU jobs ([agent.execution] max_cpu_jobs = 0)",
        )
    if (
        request.ram_bytes is not None
        and node.ram_bytes is not None
        and node.ram_bytes < request.ram_bytes
    ):
        return Rejection(
            RejectCode.RAM_SHORT,
            f"the job needs {request.ram_bytes / _GIB:g} GB of RAM; the node has "
            f"{node.ram_bytes / _GIB:.1f} GB",
        )
    if (
        request.cpus is not None
        and node.cpu_threads is not None
        and node.cpu_threads < request.cpus
    ):
        return Rejection(
            RejectCode.CPUS_SHORT,
            f"the job needs {request.cpus:g} CPUs; the node has {node.cpu_threads}",
        )
    if (
        request.base in _BASE_NEEDS_FULL_RUNTIME
        and EXEC_VENV_CAPABILITY in caps
        and EXEC_DOCKER_CAPABILITY not in caps
        and EXEC_SUBPROCESS_CAPABILITY not in caps
    ):
        return Rejection(
            RejectCode.NO_RUNTIME_FOR_BASE,
            f"env.base {request.base} needs Docker or the subprocess runtime; the "
            "node only offers the venv runtime",
        )
    return None


def policy_rejection(request: PlanRequest, node: NodeSnapshot) -> Rejection | None:
    """V0.7 G5 (D142): the node owner's policy, by the rule the agent re-checks
    with (:func:`meshforge.common.owner_policy.refusal`). A node offering
    Docker can run the job sandboxed; without it only the unsandboxed runtimes
    remain (``policy_unsandboxed`` when the owner disallows those now)."""
    if node.policy is None:
        return None
    refused = owner_policy.refusal(
        node.policy,
        owner_policy.JobAsk(
            owner_name=request.owner_name,
            gpu_required=request.gpu == "required",
            vram_bytes=request.vram_bytes,
            ram_bytes=request.ram_bytes,
            cpus=request.cpus,
            network=request.network,
            sandboxed_runtime=EXEC_DOCKER_CAPABILITY in node.capabilities,
            node_cpu_threads=node.cpu_threads,
        ),
    )
    if refused is None:
        return None
    return Rejection(RejectCode(refused.code.value), refused.text)


def _gpu_free(request: PlanRequest, node: NodeSnapshot) -> bool:
    """A GPU slot is free and a GPU no live attempt holds can take the job
    (its VRAM is enough, or not reported - the static rule's reading)."""
    if node.free_gpu_slots <= 0:
        return False
    need = request.vram_bytes
    return need is None or any(
        vram == 0 or vram >= need for vram in node.free_gpu_vram_bytes
    )


def choose_slot(request: PlanRequest, node: NodeSnapshot) -> Slot | None:
    """The slot ``request`` takes on ``node`` now (D105), or None = none free."""
    if request.gpu == "required":
        return "gpu" if _gpu_free(request, node) else None
    if request.gpu == "preferred" and _gpu_usable(node) and _gpu_free(request, node):
        return "gpu"
    return "cpu" if node.free_cpu_slots > 0 else None


def missing_trees(
    request: PlanRequest, node: NodeSnapshot, *, local: bool
) -> list[TreeNeed]:
    """The job's **distinct** trees ``node`` does not hold whole, in request
    order. A tree named twice (two mounts of one dataset, an input that is the
    project tree) is moved once, so it is listed once - by tree id, the first
    naming wins (one id is one content, so one size). The project tree counts as
    held on a seeded Local candidate (D109), also where an input names it
    again. D131's per-tree transfer is per distinct tree."""
    seen: set[str] = set()
    if local and request.seeded and request.trees:
        seen.add(request.trees[0].key)
    out: list[TreeNeed] = []
    for tree in request.trees:
        if tree.key in seen:
            continue
        seen.add(tree.key)
        if tree.key not in node.cached:
            out.append(tree)
    return out


def missing_bytes(request: PlanRequest, node: NodeSnapshot, *, local: bool) -> int:
    """Bytes of :func:`missing_trees` - each distinct tree once."""
    return sum(tree.size for tree in missing_trees(request, node, local=local))


def live_rejection(
    request: PlanRequest,
    node: NodeSnapshot,
    *,
    max_attempts_per_node: int,
    local: bool,
) -> Rejection | None:
    """The first reason ``node`` cannot take ``request`` right now (or None).
    Call only for a node :func:`static_rejection` passed."""
    if node.state != _ONLINE:
        return Rejection(RejectCode.NOT_ONLINE, f"the node is {node.state}, not online")
    if not node.connected:
        return Rejection(
            RejectCode.NO_SESSION, "the node has no live session with this controller"
        )
    excluded = request.excluded.get(node.node_id)
    if excluded is not None and request.pinned_node_id is None:
        return Rejection(
            RejectCode.RETRY_EXCLUDED,
            f"an earlier attempt of this job failed here ({excluded}); other "
            "nodes are tried first",
        )
    if request.env_key in node.known_bad_envs:
        return Rejection(
            RejectCode.ENV_KNOWN_BAD,
            "this job's environment failed to build on the node recently",
        )
    if node.live_attempts >= max_attempts_per_node:
        return Rejection(
            RejectCode.NODE_CAP,
            f"the node already runs {node.live_attempts} attempts (the "
            "controller's max_attempts_per_node)",
        )
    if choose_slot(request, node) is None:
        kind = "GPU" if request.gpu == "required" else "CPU"
        if request.gpu == "preferred":
            kind = "CPU or GPU"
        return Rejection(
            RejectCode.SLOTS_FULL, f"every {kind} slot of the node is in use"
        )
    if node.disk_free_bytes is not None:
        needed = missing_bytes(request, node, local=local)
        if needed > node.disk_free_bytes:
            return Rejection(
                RejectCode.DISK_SHORT,
                f"the job would fetch {needed / _GIB:.2f} GB; the node's largest "
                f"disk has {node.disk_free_bytes / _GIB:.2f} GB free",
            )
    return None


def rejection(
    request: PlanRequest,
    node: NodeSnapshot,
    *,
    max_attempts_per_node: int,
    local: bool,
) -> Rejection | None:
    """Static first, then live."""
    return static_rejection(request, node) or live_rejection(
        request, node, max_attempts_per_node=max_attempts_per_node, local=local
    )
