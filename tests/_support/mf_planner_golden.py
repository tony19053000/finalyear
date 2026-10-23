"""V0.14 G1: what a planner :class:`Decision` looks like, byte for byte.

:func:`decision_view` is the comparison the golden and replay tests use:

* ``candidates`` / ``split`` - exactly what ``plan_decision`` stores
  (``candidates_json``, ``split_json``), as canonical JSON text;
* ``policy``, ``chosen``, ``snapshot_digest``, ``reason_key``,
  ``all_static_rejections``;
* ``fields_sha256`` - SHA-256 of **every** field of the Decision (a
  ``dataclasses.asdict`` walk, floats by ``repr``), so a field that is not in
  the stored JSON cannot change unnoticed either.

The golden corpus (``tests/unit/planner/data/v014_g1_golden.json``) was
recorded with the planner *before* V0.14 G1's candidate-generator refactor
(``scripts`` in the progress note); the golden test replays every entry
through today's planner and compares these views.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
from enum import Enum
from typing import Any

from meshforge.controller.planner.decision_json import candidates_json, split_json
from meshforge.controller.planner.types import Decision

__all__ = ["canonical", "decision_view", "fields_text"]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=True)


def _walk(value: Any) -> Any:
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.value}"
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return f"f:{value!r}" if not math.isfinite(value) else value
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, frozenset | set):
        return sorted(_walk(v) for v in value)
    if isinstance(value, dict):
        return {repr(k): _walk(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_walk(v) for v in value]
    return repr(value)


def fields_text(decision: Decision) -> str:
    """Every field of ``decision`` as canonical text (types kept: an int and
    a float of the same value differ)."""

    def typed(value: Any) -> Any:
        if isinstance(value, float) and math.isfinite(value):
            return f"f:{value!r}"
        if isinstance(value, dict):
            return {k: typed(v) for k, v in value.items()}
        if isinstance(value, list):
            return [typed(v) for v in value]
        return value

    fields = dataclasses.asdict(decision)
    # V0.14 G3: fields added after the corpus was recorded count only when
    # set - at their default they are the pre-G3 decision exactly
    # (V0.15 G4: ``ml`` likewise)
    for name, unset in (("objective", None), ("pruned", ()), ("ml", None)):
        if fields.get(name) == unset:
            fields.pop(name, None)
    return canonical(typed(_walk(fields)))


def decision_view(decision: Decision) -> dict[str, Any]:
    return {
        "policy": decision.policy,
        "chosen": None if decision.chosen is None else decision.chosen.node_id,
        "snapshot_digest": decision.snapshot_digest,
        "reason_key": decision.reason_key,
        "all_static_rejections": decision.all_static_rejections,
        "candidates": canonical(candidates_json(decision)),
        "split": None
        if decision.split is None
        else canonical(split_json(decision.split)),
        "fields_sha256": hashlib.sha256(
            fields_text(decision).encode("utf-8")
        ).hexdigest(),
    }
