"""Stage 3: predicted completion time per feasible candidate (D108, **v0 stub**).

``T = T_queue + missing_bytes / bw + T_env + T_compute + out_bytes / bw``, every
term carrying where its number came from (:class:`~.types.TermSource`,
checklist 18). What G2 implements and what V0.6 G4 replaces:

* ``queue`` - 0: a feasible candidate has a free slot now (``measured``).
* ``transfer`` - the bytes of the job's trees the node does not hold whole
  (cache index; the project tree is held on a *seeded* Local candidate, D109)
  over ``default_bw_bytes_s`` (``assumed`` - G4 measures links). Nothing to
  move is 0 s and ``measured``.
* ``env`` - ``default_env_s`` (``assumed``; G4: 0 if the env is cached, else
  build-time history).
* ``compute`` - median ``run`` time of the same workload fingerprint on this
  node (``history``); else on the fastest-known other node, scaled by the
  hardware-score ratio when both scores are known (``scaled``) or as is
  (``history``, ``scaled: false``); else ``default_compute_s`` (``assumed``).
* ``output`` - 0 (``assumed``; G4: the fingerprint's past output size).

Pure and deterministic like the rest of the planner.
"""

from __future__ import annotations

from meshforge.controller.planner.feasibility import missing_bytes
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    Estimate,
    EstimateTerm,
    NodeSnapshot,
    PlanRequest,
    Slot,
    TermSource,
)

__all__ = ["estimate"]


def _score(node: NodeSnapshot | None, slot: Slot) -> float | None:
    if node is None:
        return None
    return node.gpu_score if slot == "gpu" else node.cpu_score


def _compute(
    request: PlanRequest, node: NodeSnapshot, slot: Slot, snapshot: ClusterSnapshot
) -> EstimateTerm:
    history = request.history
    own = history.by_node_s.get(node.node_id)
    if own is not None:
        return EstimateTerm(
            own,
            TermSource.HISTORY,
            {"samples": history.samples_by_node.get(node.node_id, 0)},
        )
    others = sorted(
        (seconds, node_id)
        for node_id, seconds in history.by_node_s.items()
        if node_id != node.node_id
    )
    if others:
        seconds, other_id = others[0]
        mine, theirs = _score(node, slot), _score(snapshot.node(other_id), slot)
        detail = {
            "from_node_id": other_id,
            "samples": history.samples_by_node.get(other_id, 0),
        }
        if mine and theirs:
            ratio = theirs / mine
            return EstimateTerm(
                seconds * ratio,
                TermSource.SCALED,
                {**detail, "scaled": True, "score_ratio": round(ratio, 4)},
            )
        return EstimateTerm(seconds, TermSource.HISTORY, {**detail, "scaled": False})
    return EstimateTerm(snapshot.config.default_compute_s, TermSource.ASSUMED)


def estimate(
    request: PlanRequest,
    node: NodeSnapshot,
    slot: Slot,
    snapshot: ClusterSnapshot,
    *,
    local: bool,
) -> Estimate:
    """See module docstring."""
    config = snapshot.config
    needed = missing_bytes(request, node, local=local)
    if needed == 0:
        transfer = EstimateTerm(0.0, TermSource.MEASURED, {"missing_bytes": 0})
    else:
        bw = config.default_bw_bytes_s
        transfer = EstimateTerm(
            needed / bw,
            TermSource.ASSUMED,
            {"missing_bytes": needed, "bw_bytes_s": bw, "bw_source": "assumed"},
        )
    return Estimate(
        queue=EstimateTerm(
            0.0,
            TermSource.MEASURED,
            {
                "free_slots": node.free_gpu_slots
                if slot == "gpu"
                else node.free_cpu_slots
            },
        ),
        transfer=transfer,
        env=EstimateTerm(config.default_env_s, TermSource.ASSUMED),
        compute=_compute(request, node, slot, snapshot),
        output=EstimateTerm(0.0, TermSource.ASSUMED, {"out_bytes": 0}),
    )
