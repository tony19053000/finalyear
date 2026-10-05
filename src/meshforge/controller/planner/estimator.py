"""Stage 3: predicted completion time per feasible candidate - **estimator v0**
(V0.6 G4, D108; replaces G2's stub).

``T = T_queue + missing_bytes / bw(src -> n) + T_env + T_compute + out_bytes /
bw(n -> ctl)``. Every term carries where its number came from
(:class:`~.types.TermSource`, checklist 18) and the numbers behind it
(``detail``). A term's source is the **weakest** of its inputs (``assumed`` <
``scaled`` < ``history`` < ``measured``): a measured byte count over an
assumed bandwidth is ``assumed``.

* ``queue`` - 0 s, ``measured``: a feasible candidate has a free slot now.
* ``transfer`` - the bytes of the job's trees the node does not hold whole
  (cache index; the project tree is held on a *seeded* Local candidate, D109)
  over the best inbound bandwidth the planner knows for the node:

  - a **fresh network-matrix link** (D111) ``n <- h`` from a node ``h`` that
    holds one of the missing trees whole (``measured``; a link older than
    ``link_stale_after_s`` is ignored and named in ``detail``);
  - the **controller -> n** bandwidth from transfer history (``history``) -
    the controller holds every job's trees;
  - else ``default_bw_bytes_s`` (``assumed``).

  The faster of the first two wins (the agent fetches from all sources at once
  and the faster one carries most of it). Nothing to move: 0 s ``measured``.
* ``env`` - 0 s ``measured`` when the env hash this workload's last attempt on
  the node reported is still in the node's cache; else the median build time
  of environments for the job's ``env.base`` (``history``); else
  ``default_env_s`` (``assumed``).
* ``compute`` - median ``run`` time of the same workload fingerprint on this
  node (``history``); else the fastest other node's median scaled by the
  CPU/GPU hardware-score ratio (``scaled``) or as is (``history``, ``scaled:
  false``); else ``default_compute_s`` (``assumed``).
* ``output`` - the fingerprint's median uploaded bytes (``history``; none
  recorded -> 0 s ``assumed``) over the controller link of the node, read as
  symmetric (``history``, ``symmetric: true``), else ``default_bw_bytes_s``
  (``assumed``).

Pure and deterministic like the rest of the planner. Staleness is judged
against ``now`` (the :func:`~.plan.plan` argument); with no ``now`` every
link counts as stale - nothing unverifiable is presented as measured.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final

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

__all__ = ["estimate", "weakest"]

_STRENGTH: Final = {
    TermSource.ASSUMED: 0,
    TermSource.SCALED: 1,
    TermSource.HISTORY: 2,
    TermSource.MEASURED: 3,
}


def weakest(*sources: TermSource) -> TermSource:
    """The least trustworthy of ``sources`` (a term is no better than its
    weakest input)."""
    return min(sources, key=_STRENGTH.__getitem__)


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


def _fresh(measured_at: dt.datetime, now: dt.datetime | None, limit: float) -> bool:
    if now is None:
        return False
    return (now - measured_at).total_seconds() <= limit


def _inbound(
    request: PlanRequest,
    node: NodeSnapshot,
    snapshot: ClusterSnapshot,
    *,
    local: bool,
    now: dt.datetime | None,
) -> tuple[float, TermSource, dict[str, Any]]:
    """The best known bandwidth into ``node`` for this job's missing trees."""
    config = snapshot.config
    missing = {
        tree.key
        for index, tree in enumerate(request.trees)
        if not (index == 0 and local and request.seeded) and tree.key not in node.cached
    }
    best: tuple[float, TermSource, dict[str, Any]] | None = None
    stale: list[str] = []
    for holder in snapshot.nodes:
        if holder.node_id == node.node_id or not (missing & holder.cached):
            continue
        link = snapshot.links.get((node.node_id, holder.node_id))
        if link is None:
            continue
        if not _fresh(link.measured_at, now, config.link_stale_after_s):
            stale.append(holder.node_id)
            continue
        age = (now - link.measured_at).total_seconds() if now else None
        candidate = (
            link.bw_bytes_s,
            TermSource.MEASURED,
            {
                "bw_source": "probe",
                "bw_from": holder.node_id,
                "link_samples": link.samples,
                "link_age_s": None if age is None else round(age, 1),
                "rtt_ms": round(link.rtt_ms, 3),
            },
        )
        if best is None or candidate[0] > best[0]:
            best = candidate
    ctl = snapshot.controller_links.get(node.node_id)
    if ctl is not None and (best is None or ctl.bw_bytes_s > best[0]):
        best = (
            ctl.bw_bytes_s,
            TermSource.HISTORY,
            {
                "bw_source": "transfer_history",
                "bw_from": "controller",
                "samples": ctl.samples,
            },
        )
    if best is None:
        best = (config.default_bw_bytes_s, TermSource.ASSUMED, {"bw_source": "assumed"})
    if stale:
        best[2]["stale_links_ignored"] = sorted(stale)
    return best


def _transfer(
    request: PlanRequest,
    node: NodeSnapshot,
    snapshot: ClusterSnapshot,
    *,
    local: bool,
    now: dt.datetime | None,
) -> EstimateTerm:
    needed = missing_bytes(request, node, local=local)
    if needed == 0:
        return EstimateTerm(0.0, TermSource.MEASURED, {"missing_bytes": 0})
    bw, source, detail = _inbound(request, node, snapshot, local=local, now=now)
    return EstimateTerm(
        needed / bw,
        weakest(TermSource.MEASURED, source),
        {"missing_bytes": needed, "bw_bytes_s": round(bw, 1), **detail},
    )


def _env(
    request: PlanRequest, node: NodeSnapshot, snapshot: ClusterSnapshot
) -> EstimateTerm:
    env_hash = request.history.env_hash_by_node.get(node.node_id)
    if env_hash is not None and env_hash in node.cached_envs:
        return EstimateTerm(
            0.0, TermSource.MEASURED, {"env_cached": True, "env_hash": env_hash}
        )
    built = snapshot.env_builds.get(request.base)
    if built is not None:
        return EstimateTerm(
            built.median_s,
            TermSource.HISTORY,
            {"env_cached": False, "base": request.base, "samples": built.samples},
        )
    return EstimateTerm(
        snapshot.config.default_env_s, TermSource.ASSUMED, {"env_cached": False}
    )


def _output(
    request: PlanRequest, node: NodeSnapshot, snapshot: ClusterSnapshot
) -> EstimateTerm:
    history = request.history
    if history.output_bytes is None:
        return EstimateTerm(0.0, TermSource.ASSUMED, {"out_bytes": 0})
    out = history.output_bytes
    ctl = snapshot.controller_links.get(node.node_id)
    detail: Mapping[str, Any]
    if ctl is not None:
        bw, bw_source = ctl.bw_bytes_s, TermSource.HISTORY
        detail = {"bw_source": "transfer_history", "symmetric": True}
    else:
        bw, bw_source = snapshot.config.default_bw_bytes_s, TermSource.ASSUMED
        detail = {"bw_source": "assumed"}
    return EstimateTerm(
        out / bw,
        weakest(TermSource.HISTORY, bw_source),
        {
            "out_bytes": round(out),
            "samples": history.output_samples,
            "bw_bytes_s": round(bw, 1),
            **detail,
        },
    )


def estimate(
    request: PlanRequest,
    node: NodeSnapshot,
    slot: Slot,
    snapshot: ClusterSnapshot,
    *,
    local: bool,
    now: dt.datetime | None = None,
) -> Estimate:
    """See module docstring."""
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
        transfer=_transfer(request, node, snapshot, local=local, now=now),
        env=_env(request, node, snapshot),
        compute=_compute(request, node, slot, snapshot),
        output=_output(request, node, snapshot),
    )
