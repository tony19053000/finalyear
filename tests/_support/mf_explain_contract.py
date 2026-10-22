"""V0.14 G5 criterion 4: the explain contract fixture (one file, three readers).

``frontend/src/test/fixtures/explainContract.json`` holds

* ``payload`` - a ``GET /v1/jobs/{id}/decisions`` answer **produced by the
  controller's own answer code** (``decision_routes._out`` on the records
  below), with every field of the answer non-null somewhere: a calibrated,
  risk-carrying whole job that ran, and a Mode 3 split decision whose lane
  was lost;
* ``fields`` - every leaf the CLI and the Plan tab must both show, as a
  concrete path into ``payload`` and a changed value (``to``);
* ``not_shown`` - the leaves neither shows, each with its reason.

``tests/unit/test_v014_g5_explain_contract.py`` proves: the fixture is what
this module generates (so it is the API's answer), ``fields`` + ``not_shown``
is exactly the answer model's leaf set, and the CLI's text changes when any
listed field changes. ``frontend/src/components/jobs/planContract.test.tsx``
proves the same of the Plan tab on the same file - so both show one field
set (identical fields, criterion 4).

Regenerate after an answer-model change::

    uv run python tests/_support/mf_explain_contract.py
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import types
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel

from meshforge.controller.api.decision_routes import DecisionList, _out
from meshforge.controller.jobs.decision_store import ActualOutcome, DecisionRecordOut

__all__ = [
    "FIXTURE",
    "NOT_SHOWN",
    "contract",
    "fixture_leaves",
    "model_leaves",
    "mutate",
    "normalise",
]

FIXTURE: Final = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "test"
    / "fixtures"
    / "explainContract.json"
)
_T0: Final = dt.datetime(2026, 10, 22, 9, 30, tzinfo=dt.UTC)
_DICT_KEYED: Final = ("terms", "phases_ms")
"""Answer fields that are maps keyed by name (a term, a phase)."""

NOT_SHOWN: Final[dict[str, str]] = {
    "job_id": "the page and the command are already the job's",
    "count": "the number of decisions listed - every one is shown",
    "decisions.[].attempt_id": "the same id as actual.attempt_id, which both show",
    "decisions.[].candidates.[].estimate.terms.{}.detail": (
        "free-form estimator diagnostics; both show them through the term's "
        "server-computed note (common/explain_text.term_note)"
    ),
    "decisions.[].candidates.[].pruned.[].node_id": (
        "named in margin_sentence (the server's sentence), which both show"
    ),
    "decisions.[].candidates.[].pruned.[].by": (
        "named in margin_sentence (the server's sentence), which both show"
    ),
    "decisions.[].candidates.[].estimate.terms.{}.calibration.version": (
        "always the decision's calibration_version, which both show in the header"
    ),
    "decisions.[].candidates.[].estimate.terms.{}.calibration.raw_s": (
        "seconds / factor - both show the calibrated seconds and the factor (D300)"
    ),
}

_OVERRIDES: Final[dict[str, Any]] = {
    "decisions.[].created_at": "2027-03-04T05:06:07Z",
}
"""Leaves whose generic change would not be a valid value (a timestamp)."""


def _term(seconds: float, source: str, **detail: Any) -> dict[str, Any]:
    return {"seconds": seconds, "source": source, "detail": detail}


def _calibration(factor: float, n: int, level: str, raw: float) -> dict[str, Any]:
    return {"factor": factor, "n": n, "key_level": level, "version": 7, "raw_s": raw}


def _whole() -> DecisionRecordOut:
    terms = {
        "queue": _term(0.0, "measured"),
        "transfer": _term(
            4.0,
            "history",
            bw_from="controller",
            last_transfer_age_s=7200.0,
            calibration=_calibration(2.0, 14, "node", 2.0),
        ),
        "env": _term(1.0, "assumed", env_cached=True),
        "compute": _term(
            30.0, "history", calibration=_calibration(1.25, 9, "global", 24.0)
        ),
        "output": _term(2.0, "scaled"),
        "risk": _term(
            1.48, "history", p_fail=0.04, failures=1, total=20, window_days=7.0
        ),
    }
    chosen = {
        "node_id": "node_bbbbbbbb",
        "mode": "offload",
        "feasible": True,
        "slot": "cpu",
        "reject_code": None,
        "reject_text": None,
        "estimate": {"total_s": 38.48, "terms": terms},
        "score": 38.48,
        "rank": 1,
    }
    local = copy.deepcopy(chosen) | {
        "node_id": "node_aaaaaaaa",
        "mode": "local",
        "score": 52.25,
        "rank": 2,
    }
    local["estimate"]["total_s"] = 52.25
    rejected = {
        "node_id": "node_cccccccc",
        "mode": "offload",
        "feasible": False,
        "slot": None,
        "reject_code": "slots_full",
        "reject_text": "every CPU slot of the node is in use",
        "estimate": None,
        "score": None,
        "rank": None,
    }
    return DecisionRecordOut(
        id=21,
        attempt_no=1,
        attempt_id="att_000000000000000000000021",
        outcome="dispatched",
        policy="cost_model",
        chosen_node_id="node_bbbbbbbb",
        created_at=_T0,
        snapshot_digest="1f" * 32,
        reason_key="00" * 32,
        candidates=[chosen, local, rejected],
        actual=ActualOutcome(
            attempt_id="att_000000000000000000000021",
            node_id="node_bbbbbbbb",
            state="done",
            failure_type=None,
            phases_ms={
                "queue": 40,
                "stage": 2_600,
                "env": 900,
                "run": 36_000,
                "collect": 2_500,
            },
            role="run",
            ended_at=_T0 + dt.timedelta(minutes=1),
        ),
        shadow_chosen_node_id="node_aaaaaaaa",
        shadow_estimate_s=52.25,
        objective="min_time",
        calibration_version=7,
        replay_version=2,
        decision_ms=4.25,  # V0.14 G6 (D324)
    )


def _split() -> DecisionRecordOut:
    nodes = ["node_aaaaaaaa", "node_bbbbbbbb", "node_cccccccc"]
    pruned = [{"node_id": "node_dddddddd", "by": "node_aaaaaaaa"}]
    margin = {
        "set_s": 41.0,
        "best_single_s": 52.0,
        "best_single_node_id": "node_aaaaaaaa",
        "margin": 0.1,
        "threshold_s": 46.8,
        "passed": True,
        "forced": "node_set",
    }
    set_terms = {
        "queue": _term(0.0, "measured"),
        "transfer": _term(3.0, "assumed", bw_source="assumed"),
        "env": _term(0.5, "history"),
        "compute": _term(30.0, "history"),
        "output": _term(1.0, "assumed"),
        "items_fetch": _term(2.0, "assumed"),
        "chunk_overhead": _term(1.5, "history"),
        "tail": _term(1.2, "assumed"),
        "combine": _term(0.8, "assumed"),
        "risk": _term(
            1.0,
            "assumed",
            lanes=[
                {"node_id": n, "p_fail": 0.05, "failures": 0, "total": 0}
                for n in nodes[:2]
            ],
        ),
    }
    split_row = {
        "node_id": nodes[0],
        "mode": "split",
        "feasible": True,
        "slot": "cpu",
        "reject_code": None,
        "reject_text": None,
        "estimate": {"total_s": 41.0, "terms": set_terms},
        "score": 41.0,
        "rank": 1,
        "node_set": nodes,
        "chunk_items": 10,
        "chunks": 30,
        "inactive": [nodes[2]],
        "margin_test": margin,
        "pruned": pruned,
    }
    lane_terms = {
        name: set_terms[name] for name in ("queue", "transfer", "env", "compute")
    }
    lane_row = {
        "node_id": nodes[0],
        "mode": "offload",
        "feasible": True,
        "slot": "cpu",
        "reject_code": None,
        "reject_text": None,
        "estimate": {"total_s": 52.0, "terms": lane_terms},
        "score": 52.0,
        "rank": 2,
        "node_set": [nodes[0]],
        "chunk_items": 10,
        "chunks": 30,
        "rate_items_s": 5.75,
        "rate_source": "history",
        "start_s": 3.5,
        "pruned": pruned,
    }
    return DecisionRecordOut(
        id=22,
        attempt_no=2,
        attempt_id="att_000000000000000000000022",
        outcome="dispatched",
        policy="cost_model",
        chosen_node_id=nodes[0],
        created_at=_T0 + dt.timedelta(minutes=5),
        snapshot_digest="2e" * 32,
        reason_key="01" * 32,
        candidates=[split_row, lane_row],
        actual=ActualOutcome(
            attempt_id="att_000000000000000000000022",
            node_id=nodes[0],
            state="failed",
            failure_type="node_lost",
            phases_ms={"queue": 15, "stage": 1_000},
            role="lane",
            ended_at=_T0 + dt.timedelta(minutes=6),
        ),
        objective="min_network",
        decision_ms=37.5,  # V0.14 G6 (D324)
    )


def payload() -> dict[str, Any]:
    """The decisions answer for the fixture's two records (the API's code)."""
    decisions = tuple(_out(r) for r in (_whole(), _split()))
    answer = DecisionList(
        job_id="job_" + "0" * 26, decisions=decisions, count=len(decisions)
    )
    data: dict[str, Any] = answer.model_dump(mode="json")
    return data


# ------------------------------------------------------------------ leaves


def normalise(path: tuple[str | int, ...]) -> str:
    """``("decisions", 0, "candidates", 1, "estimate", "terms", "env",
    "seconds")`` -> ``decisions.[].candidates.[].estimate.terms.{}.seconds``."""
    out: list[str] = []
    for index, token in enumerate(path):
        if isinstance(token, int):
            out.append("[]")
        elif index > 0 and path[index - 1] in _DICT_KEYED:
            out.append("{}")
        else:
            out.append(token)
    return ".".join(out)


def fixture_leaves(
    data: Any, path: tuple[str | int, ...] = ()
) -> Iterator[tuple[tuple[str | int, ...], Any]]:
    """Every leaf ``(path, value)`` of an answer, in document order (``detail``
    is one leaf: a free-form map)."""
    if isinstance(data, dict) and (not path or path[-1] != "detail"):
        for key, value in data.items():
            yield from fixture_leaves(value, (*path, key))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from fixture_leaves(value, (*path, index))
    else:
        yield path, data


def _strip_none(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return args[0] if len(args) == 1 else annotation
    return annotation


def _type_leaves(annotation: Any, path: str) -> Iterator[str]:
    annotation = _strip_none(annotation)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield from model_leaves(annotation, path + ".")
    elif origin in (tuple, list):
        yield from _type_leaves(args[0], path + ".[]")
    elif origin is dict:
        if args[1] is Any:
            yield path  # a free-form map is one leaf (``detail``)
        else:
            yield from _type_leaves(args[1], path + ".{}")
    else:
        yield path


def model_leaves(model: type[BaseModel], prefix: str = "") -> Iterator[str]:
    """Every leaf of the answer model, normalised like :func:`normalise`."""
    for name, field in model.model_fields.items():
        yield from _type_leaves(field.annotation, prefix + name)


# ------------------------------------------------------------------ changes


def mutate(value: Any) -> Any:
    """A different value of the same JSON type (strings get a ``Q`` in front,
    so a truncated id still differs; the first item of a list changes)."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value * 3 + 7
    if isinstance(value, float):
        return round(value * 3.0 + 7.25, 6)
    if isinstance(value, str):
        return "Q" + value
    raise TypeError(f"no change rule for {value!r}")


def _fields(data: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    fields: list[dict[str, Any]] = []
    for path, value in fixture_leaves(data):
        key = normalise(path)
        if key in seen or key in NOT_SHOWN or value is None:
            continue
        seen.add(key)
        fields.append(
            {
                "field": key,
                "path": list(path),
                "to": _OVERRIDES.get(key, mutate(value)),
            }
        )
    return fields


def contract() -> dict[str, Any]:
    data = payload()
    return {
        "_comment": (
            "GENERATED by tests/_support/mf_explain_contract.py - do not edit. "
            "V0.14 G5 criterion 4: the CLI and the Plan tab show exactly `fields`."
        ),
        "payload": data,
        "fields": _fields(data),
        "not_shown": [{"field": k, "reason": v} for k, v in sorted(NOT_SHOWN.items())],
    }


def text() -> str:
    return json.dumps(contract(), indent=2, sort_keys=False) + "\n"


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(text(), encoding="utf-8")
    sys.stdout.write(f"wrote {FIXTURE}\n")
