"""The sentences that explain a planner decision (V0.14 G5, D300, D320).

Pure text over the **stored decision JSON** (``plan_decision.candidates``, the
rows ``GET /v1/jobs/{id}/decisions`` answers): no database, no clock. It lives
in ``common`` because both sides need the *same* function - the controller
puts the results in the decisions answer (``margin_sentence`` per decision,
``note`` per term), and ``compute explain`` prints them (the CLI may not
import the controller); the dashboard shows the API's strings as they are.
One function, so the Plan tab and the CLI cannot say different things
(criterion 4).

* :func:`margin_sentence` - why a split job was or was not split (the D265
  margin test, D295's objective case, D294's pruned nodes); ``None`` for a job
  that runs whole.
* :func:`term_note` - what a predicted term's ``detail`` says that its seconds
  and source do not: where a transfer's bandwidth came from (V0.6 G4, D131),
  a cached environment, a bandwidth raised to the floor, stale probes ignored,
  and a ``risk`` term's failure probability (D296). The calibration factor is
  **not** in it - the API carries it as its own typed field.

Every value read here may be anything (stored JSON, a hostile or older
controller): nothing raises, an unreadable part is left out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = [
    "format_seconds",
    "margin_sentence",
    "term_note",
]

_DASH: Final = "-"
_MAX_ORIGINS: Final = 3
_MAX_LANES: Final = 3
_ORIGIN_CHARS: Final = 8
"""A probed holder is named by the last 8 characters of its node id."""


def _num(value: object) -> float | None:
    """A finite number or ``None`` (a JSON string, bool or null never is)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def format_seconds(value: object) -> str:
    """``12.00s``; anything that is not a finite number -> ``-``."""
    number = _num(value)
    return _DASH if number is None else f"{number:.2f}s"


def _age(value: object) -> str | None:
    seconds = _num(value)
    if seconds is None or seconds < 0:
        return None
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.0f}h"


# ------------------------------------------------------------------ term notes


def _origin(detail: Mapping[str, Any]) -> str | None:
    """Where one bandwidth came from: ``controller`` (with the age of its
    latest transfer when known - D131: that history has no staleness rule),
    the last 8 characters of a probed holder's node id, or ``default`` for an
    assumed bandwidth."""
    source = detail.get("bw_from")
    if source == "controller":
        age = _age(detail.get("last_transfer_age_s"))
        return "controller" + (f" (last transfer {age} ago)" if age else "")
    if isinstance(source, str) and source:
        return source[-_ORIGIN_CHARS:]
    return "default" if detail.get("bw_source") == "assumed" else None


def _origins(detail: Mapping[str, Any]) -> list[str]:
    """Every distinct source of a transfer term, in the job's tree order: the
    term's own when one source carried it all, else one per ``items`` entry
    (D131: each missing tree is costed by its own source)."""
    if isinstance(detail.get("bw_from"), str) and detail.get("bw_from"):
        one = _origin(detail)
        return [one] if one else []
    items = detail.get("items")
    found: list[str] = []
    for item in items if isinstance(items, list) else []:
        label = _origin(item) if isinstance(item, Mapping) else None
        if label and label not in found:
            found.append(label)
    return found


def _clamped(detail: Mapping[str, Any]) -> bool:
    items = detail.get("items")
    return detail.get("bw_clamped") is True or any(
        isinstance(i, Mapping) and i.get("bw_clamped") is True
        for i in (items if isinstance(items, list) else [])
    )


def _percent(value: object) -> str | None:
    number = _num(value)
    return None if number is None else f"{number * 100:.1f}%"


def _risk_note(detail: Mapping[str, Any]) -> str | None:
    """D296: ``p_fail 5.0% (2 of 10 ends in 7d)`` for one node (``prior`` when
    it has no counted end yet); per lane for a split candidate."""
    lanes = detail.get("lanes")
    if isinstance(lanes, list):
        parts = []
        for lane in lanes:
            if not isinstance(lane, Mapping):
                continue
            shown = _percent(lane.get("p_fail"))
            node = lane.get("node_id")
            if shown is not None and isinstance(node, str):
                parts.append(f"{node[-_ORIGIN_CHARS:]} {shown}")
        if not parts:
            return None
        more = len(parts) - _MAX_LANES
        return (
            "p_fail "
            + ", ".join(parts[:_MAX_LANES])
            + (f" + {more} more" if more > 0 else "")
        )
    shown = _percent(detail.get("p_fail"))
    if shown is None:
        return None
    failures, total = _num(detail.get("failures")), _num(detail.get("total"))
    window = _num(detail.get("window_days"))
    if failures is None or total is None or total <= 0:
        return f"p_fail {shown} (prior)"
    span = f" in {window:g}d" if window is not None else ""
    return f"p_fail {shown} ({failures:g} of {total:g} ends{span})"


def term_note(term: object, *, name: str | None = None) -> str | None:
    """The term's note (module docstring), parts joined by ``", "``; ``None``
    when its detail says nothing more. ``name`` = the term's name (``risk``
    reads the failure probability)."""
    if not isinstance(term, Mapping):
        return None
    detail = term.get("detail")
    detail = detail if isinstance(detail, Mapping) else {}
    parts: list[str] = []
    origins = _origins(detail)
    if origins:
        shown = origins[:_MAX_ORIGINS]
        more = len(origins) - len(shown)
        parts.append("from " + " + ".join(shown) + (f" + {more} more" if more else ""))
    elif detail.get("env_cached") is True:
        parts.append("cached")
    if _clamped(detail):
        parts.append("at the bandwidth floor")
    stale = detail.get("stale_links_ignored")
    if isinstance(stale, list) and stale:
        parts.append(f"{len(stale)} stale probe(s) ignored")
    if name == "risk":
        risk = _risk_note(detail)
        if risk is not None:
            parts.append(risk)
    return ", ".join(parts) if parts else None


# ------------------------------------------------------------------ margin test


def _node_set(candidate: Mapping[str, Any]) -> list[str]:
    nodes = candidate.get("node_set")
    return [str(n) for n in nodes] if isinstance(nodes, list | tuple) else []


def _margin(test: Mapping[str, Any]) -> str:
    number = _num(test.get("margin"))
    return _DASH if number is None else f"{number:g}"


def _verdict(rows: Sequence[Mapping[str, Any]]) -> str:
    """One sentence: why the job was (not) split (D265 margin test)."""
    chosen = next((c for c in rows if c.get("rank") == 1), None)
    tests = [
        c.get("margin_test")
        for c in rows
        if c.get("mode") == "split" and isinstance(c.get("margin_test"), Mapping)
    ]
    refused = next(
        (c for c in rows if c.get("mode") == "split" and c.get("reject_code")), None
    )
    if chosen is not None and chosen.get("mode") == "split":
        test = chosen.get("margin_test")
        test = test if isinstance(test, Mapping) else {}
        return (
            f"split over {len(_node_set(chosen))} nodes: "
            f"{format_seconds(test.get('set_s'))} <= (1 - {_margin(test)}) x "
            f"{format_seconds(test.get('best_single_s'))} "
            f"(best single node {test.get('best_single_node_id')}), needs <= "
            f"{format_seconds(test.get('threshold_s'))}"
        )
    if refused is not None:
        return f"not split: {refused.get('reject_text')}"
    best = tests[0] if tests else None
    if isinstance(best, Mapping) and best.get("passed") is True:
        # V0.14 G3 (D295): the margin test passed; the job's objective did not
        return (
            f"not split: the best set {format_seconds(best.get('set_s'))} passed "
            "the margin test, but the job's objective ranked one node first"
        )
    if isinstance(best, Mapping):
        return (
            f"not split: the best set {format_seconds(best.get('set_s'))} is above "
            f"{format_seconds(best.get('threshold_s'))} = (1 - {_margin(best)}) x "
            f"{format_seconds(best.get('best_single_s'))} "
            f"(best single node {best.get('best_single_node_id')})"
        )
    return "not split: the job is pinned to one node"


def _pruned(rows: Sequence[Mapping[str, Any]]) -> str:
    """V0.14 G3 (D294): which idle nodes sets were skipped for, and why."""
    pruned = next((c.get("pruned") for c in rows if c.get("pruned")), None)
    if not isinstance(pruned, list | tuple) or not pruned:
        return ""
    parts = [
        f"{p.get('node_id')} (by {p.get('by')})"
        for p in pruned
        if isinstance(p, Mapping)
    ]
    if not parts:
        return ""
    return (
        "; sets that only add an idle node were not costed: "
        + ", ".join(parts)
        + " - the faster node would finish every item before it could start"
    )


def margin_sentence(candidates: object) -> str | None:
    """Why the split job was (not) split, plus the pruned nodes; ``None`` when
    no candidate is a split job's row (a job that runs whole)."""
    rows = [
        c
        for c in (candidates if isinstance(candidates, list | tuple) else [])
        if isinstance(c, Mapping) and _node_set(c)
    ]
    if not rows:
        return None
    return _verdict(rows) + _pruned(rows)
