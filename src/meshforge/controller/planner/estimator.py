"""Stage 3: predicted completion time per feasible candidate - **estimator v0**
(V0.6 G4, D108; replaces G2's stub).

``T = T_queue + missing_bytes / bw(src -> n) + T_env + T_compute + out_bytes /
bw(n -> ctl)``. Every term carries where its number came from
(:class:`~.types.TermSource`, checklist 18) and the numbers behind it
(``detail``). A term's source is the **weakest** of its inputs (``assumed`` <
``scaled`` < ``history`` < ``measured``): a measured byte count over an
assumed bandwidth is ``assumed``.

* ``queue`` - 0 s, ``measured``: a feasible candidate has a free slot now.
* ``transfer`` - **per missing tree** (D131): every distinct tree of the job
  the node does not hold whole (cache index; the project tree is held on a
  *seeded* Local candidate, D109; a tree named twice is moved and billed once -
  :func:`~.feasibility.missing_trees`) is costed over **its own** best source
  and the times are summed:

  - a **fresh network-matrix link** (D111) ``n <- h`` from a node ``h`` that
    holds *that* tree whole (``measured``; a link older than
    ``link_stale_after_s`` is ignored and named in ``detail``; equal links
    break on the holder's node id);
  - the **controller -> n** bandwidth from transfer history (``history``,
    with the age of its latest transfer) - the controller holds every job's
    trees;
  - else ``default_bw_bytes_s`` (``assumed``).

  The faster of the first two wins per tree (the agent fetches from all
  sources at once and the faster one carries most of it). The term's source
  is the weakest one used. ``detail`` names the source of the whole term when
  every tree shares it (``bw_source`` + ``bw_from``); several probed holders
  give ``bw_source: "probe"``, ``bw_holders`` and the oldest link's age;
  anything else is ``bw_source: "mixed"``. With more than one missing tree
  there is one ``items`` entry per tree (bytes, seconds, source, where from;
  at most :data:`MAX_TRANSFER_ITEMS`), so ``compute explain`` never credits a
  holder with bytes it does not hold. Nothing to move: 0 s ``measured``.
* ``env`` - 0 s ``measured`` when the env hash this workload's last attempt on
  the node reported is still in the node's cache; else the median build time
  of environments for the job's ``env.base`` (``history``); else
  ``default_env_s`` (``assumed``).
* ``compute`` - median ``run`` time of the same workload fingerprint on this
  node (``history``); else another node's median scaled by the CPU/GPU
  hardware-score ratio (``scaled``) - the node whose median **normalised by
  its score** (seconds x score, the work it did) is smallest (D131: 11 s on
  a score-10 node beats 10 s on a score-100 node, although its raw seconds
  are more) - or, when this node or every
  other one has no score, the fastest other node's median as is
  (``history``, ``scaled: false``); else ``default_compute_s`` (``assumed``).
* ``output`` - the fingerprint's median uploaded bytes (``history``; none
  recorded -> 0 s ``assumed``) over the controller link of the node, read as
  symmetric (``history``, ``symmetric: true``), else ``default_bw_bytes_s``
  (``assumed``).

**Always finite (D131).** No bandwidth below ``min_bw_bytes_s`` is divided
by (a slower or non-finite one is raised to it, ``bw_clamped: true``) and
every term is capped at :data:`MAX_TERM_S`, so one absurd link or score can
neither produce ``Infinity`` nor win by a negative time.

Pure and deterministic like the rest of the planner. Staleness is judged
against ``now`` (the :func:`~.plan.plan` argument); with no ``now`` every
link counts as stale - nothing unverifiable is presented as measured.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from typing import Any, Final

from meshforge.controller.planner.feasibility import missing_trees
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    Estimate,
    EstimateTerm,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    Slot,
    TermSource,
    TreeNeed,
)

__all__ = ["MAX_TERM_S", "MAX_TRANSFER_ITEMS", "estimate", "weakest"]

MAX_TERM_S: Final = 1e9
"""No term is predicted longer than this (~31 years): a bound, not a guess -
it keeps every estimate finite and JSON-safe (D131)."""
MAX_TRANSFER_ITEMS: Final = 16
"""Per-tree entries kept in a transfer term's ``detail`` (the rest are
counted in ``items_omitted``; the seconds always cover every tree)."""

_LEAST_FLOOR: Final = 1.0
"""The lowest bandwidth floor there is - the lower bound of ``[controller.
network] min_bw_bytes_s``. A :class:`PlannerConfig` built by hand with a zero,
negative or NaN floor is read as this, so nothing ever divides by zero."""

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


def _bounded(seconds: float) -> float:
    """``seconds`` inside ``[0, MAX_TERM_S]`` (NaN and infinities too)."""
    if not seconds >= 0.0:  # negative or NaN
        return 0.0 if seconds < 0.0 else MAX_TERM_S
    return min(seconds, MAX_TERM_S)


def _floored(bw: float, config: PlannerConfig) -> tuple[float, bool]:
    """``bw`` never below ``min_bw_bytes_s`` (D131); True = it was raised."""
    floor = config.min_bw_bytes_s
    if not floor >= _LEAST_FLOOR:  # a hand-built config (the settings refuse it)
        floor = _LEAST_FLOOR
    if bw >= floor:  # False for NaN too
        return bw, False
    return floor, True


def _score(node: NodeSnapshot | None, slot: Slot) -> float | None:
    """The node's hardware score for ``slot`` when it can scale a runtime
    (finite and positive), else None."""
    if node is None:
        return None
    score = node.gpu_score if slot == "gpu" else node.cpu_score
    if score is None or not math.isfinite(score) or score <= 0.0:
        return None
    return score


def _compute(
    request: PlanRequest, node: NodeSnapshot, slot: Slot, snapshot: ClusterSnapshot
) -> EstimateTerm:
    history = request.history
    own = history.by_node_s.get(node.node_id)
    if own is not None:
        return EstimateTerm(
            _bounded(own),
            TermSource.HISTORY,
            {"samples": history.samples_by_node.get(node.node_id, 0)},
        )
    others = sorted(
        (seconds, node_id)
        for node_id, seconds in history.by_node_s.items()
        if node_id != node.node_id
    )
    if not others:
        return EstimateTerm(
            _bounded(snapshot.config.default_compute_s), TermSource.ASSUMED
        )
    mine = _score(node, slot)
    # D131: the best other node is the one with the least work (seconds x its
    # score), not the least seconds - a slow machine's time says less
    scored = sorted(
        (seconds * theirs, other_id, seconds, theirs)
        for seconds, other_id in others
        if (theirs := _score(snapshot.node(other_id), slot)) is not None
    )
    if mine is not None and scored:
        work, other_id, _seconds, theirs = scored[0]
        return EstimateTerm(
            _bounded(work / mine),
            TermSource.SCALED,
            {
                "from_node_id": other_id,
                "samples": history.samples_by_node.get(other_id, 0),
                "scaled": True,
                "score_ratio": round(theirs / mine, 4),
            },
        )
    seconds, other_id = others[0]
    return EstimateTerm(
        _bounded(seconds),
        TermSource.HISTORY,
        {
            "from_node_id": other_id,
            "samples": history.samples_by_node.get(other_id, 0),
            "scaled": False,
        },
    )


# A link row dated further than this in the future (the controller clock
# stepped back) is stale, not fresh forever until the next probe.
_FUTURE_SKEW_S = 5.0


def _fresh(measured_at: dt.datetime, now: dt.datetime | None, limit: float) -> bool:
    if now is None:
        return False
    age = (now - measured_at).total_seconds()
    return -_FUTURE_SKEW_S <= age <= limit


def _age(at: dt.datetime | None, now: dt.datetime | None) -> float | None:
    if at is None or now is None:
        return None
    return round(max(0.0, (now - at).total_seconds()), 1)


def _controller_source(
    node: NodeSnapshot, snapshot: ClusterSnapshot, now: dt.datetime | None
) -> tuple[float, TermSource, dict[str, Any]]:
    """What the controller can serve ``node`` at: its transfer history, else
    the configured default. The same for every tree (it holds them all)."""
    config = snapshot.config
    ctl = snapshot.controller_links.get(node.node_id)
    if ctl is None:
        bw, clamped = _floored(config.default_bw_bytes_s, config)
        detail: dict[str, Any] = {"bw_source": "assumed"}
        source = TermSource.ASSUMED
    else:
        bw, clamped = _floored(ctl.bw_bytes_s, config)
        detail = {
            "bw_source": "transfer_history",
            "bw_from": "controller",
            "samples": ctl.samples,
            "last_transfer_age_s": _age(ctl.last_at, now),
        }
        source = TermSource.HISTORY
    if clamped:
        detail["bw_clamped"] = True
    return bw, source, detail


def _tree_source(
    tree: TreeNeed,
    node: NodeSnapshot,
    snapshot: ClusterSnapshot,
    controller: tuple[float, TermSource, dict[str, Any]],
    *,
    now: dt.datetime | None,
    stale: set[str],
) -> tuple[float, TermSource, dict[str, Any]]:
    """The best known bandwidth into ``node`` for **this** tree (D131)."""
    config = snapshot.config
    best: tuple[float, str, dict[str, Any]] | None = None
    for holder in snapshot.nodes:
        if holder.node_id == node.node_id or tree.key not in holder.cached:
            continue
        link = snapshot.links.get((node.node_id, holder.node_id))
        if link is None:
            continue
        if not _fresh(link.measured_at, now, config.link_stale_after_s):
            stale.add(holder.node_id)
            continue
        bw, clamped = _floored(link.bw_bytes_s, config)
        # the faster link wins; equal links break on the node id (checklist 16)
        if best is not None and (
            bw < best[0] or (bw == best[0] and holder.node_id > best[1])
        ):
            continue
        detail: dict[str, Any] = {
            "bw_source": "probe",
            "bw_from": holder.node_id,
            "link_samples": link.samples,
            "link_age_s": _age(link.measured_at, now),
            "rtt_ms": round(link.rtt_ms, 3) if math.isfinite(link.rtt_ms) else None,
        }
        if clamped:
            detail["bw_clamped"] = True
        best = (bw, holder.node_id, detail)
    if best is None:
        return controller
    if controller[1] is TermSource.HISTORY and controller[0] > best[0]:
        return controller  # the faster source carries most of it
    return best[0], TermSource.MEASURED, best[2]  # a probe beats an assumption


def _transfer(
    request: PlanRequest,
    node: NodeSnapshot,
    snapshot: ClusterSnapshot,
    *,
    local: bool,
    now: dt.datetime | None,
) -> EstimateTerm:
    missing = missing_trees(request, node, local=local)
    needed = sum(tree.size for tree in missing)
    if needed == 0:
        return EstimateTerm(0.0, TermSource.MEASURED, {"missing_bytes": 0})
    controller = _controller_source(node, snapshot, now)
    stale: set[str] = set()
    seconds = 0.0
    source = TermSource.MEASURED  # the byte counts are the cache index
    items: list[dict[str, Any]] = []
    for tree in missing:
        bw, bw_source, bw_detail = _tree_source(
            tree, node, snapshot, controller, now=now, stale=stale
        )
        took = _bounded(tree.size / bw)
        seconds += took
        source = weakest(source, bw_source)
        items.append(
            {
                "tree": tree.key,
                "bytes": tree.size,
                "seconds": round(took, 6),
                "bw_bytes_s": round(bw, 1),
                "source": bw_source.value,
                **bw_detail,
            }
        )
    seconds = _bounded(seconds)
    detail: dict[str, Any] = {"missing_bytes": needed}
    origins = {(i["bw_source"], i.get("bw_from")) for i in items}
    if len(origins) == 1:  # one source carries it all: say so at the top
        shared = items[0]
        detail["bw_bytes_s"] = shared["bw_bytes_s"]
        detail.update(
            {
                k: v
                for k, v in shared.items()
                if k not in ("tree", "bytes", "seconds", "source", "bw_bytes_s")
            }
        )
    else:
        detail["bw_bytes_s"] = round(needed / seconds, 1) if seconds > 0 else None
        kinds = {kind for kind, _origin in origins}
        if kinds == {"probe"}:  # several holders, every link probed and fresh
            ages = [i["link_age_s"] for i in items if i["link_age_s"] is not None]
            detail["bw_source"] = "probe"
            detail["bw_holders"] = sorted({i["bw_from"] for i in items})
            detail["link_age_s"] = max(ages) if ages else None
        else:
            detail["bw_source"] = "mixed"
    if len(items) > 1:
        detail["items"] = items[:MAX_TRANSFER_ITEMS]
        if len(items) > MAX_TRANSFER_ITEMS:
            detail["items_omitted"] = len(items) - MAX_TRANSFER_ITEMS
    if stale:
        detail["stale_links_ignored"] = sorted(stale)
    return EstimateTerm(seconds, source, detail)


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
            _bounded(built.median_s),
            TermSource.HISTORY,
            {"env_cached": False, "base": request.base, "samples": built.samples},
        )
    return EstimateTerm(
        _bounded(snapshot.config.default_env_s),
        TermSource.ASSUMED,
        {"env_cached": False},
    )


def _output(
    request: PlanRequest, node: NodeSnapshot, snapshot: ClusterSnapshot
) -> EstimateTerm:
    history = request.history
    if history.output_bytes is None:
        return EstimateTerm(0.0, TermSource.ASSUMED, {"out_bytes": 0})
    out = history.output_bytes
    if not math.isfinite(out) or out < 0.0:
        return EstimateTerm(0.0, TermSource.ASSUMED, {"out_bytes": 0})
    bw, bw_source, link = _controller_source(node, snapshot, None)
    detail: Mapping[str, Any]
    if bw_source is TermSource.HISTORY:
        detail = {"bw_source": "transfer_history", "symmetric": True}
    else:
        detail = {"bw_source": "assumed"}
    if link.get("bw_clamped"):
        detail = {**detail, "bw_clamped": True}
    return EstimateTerm(
        _bounded(out / bw),
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
