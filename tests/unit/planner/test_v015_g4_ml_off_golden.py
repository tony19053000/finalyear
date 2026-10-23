"""V0.15 G4 step 5 (D336, brief §5d: ``ml = "off"`` -> predictor None ->
identical decisions to V0.14).

The V0.14 golden corpus (``data/v014_g1_golden.json.gz``: 397 recorded
planner calls - whole and split jobs, every policy, pinned, waiting, failed,
forced sets) planned with ``ml=None`` **explicitly** gives the recorded
decision byte for byte (stored candidate JSON, split JSON, digest, reason
key, every Decision field) and a replay without an ``ml`` key - exactly what
the dispatcher writes when no model is configured.

And with a model that has **no** term (``has_model`` False everywhere), the
decision differs from V0.14 only by the ML records (``ml_model_version``,
``detail.ml``, the digest): same pick, same ranks, same times.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from mf_ml_stub import stub_ml
from mf_planner_golden import decision_view

from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json, split_json
from meshforge.controller.planner.replay import from_replay, to_replay

pytestmark = pytest.mark.unit

CORPUS = Path(__file__).parent / "data" / "v014_g1_golden.json.gz"


def _entries() -> list[dict[str, Any]]:
    with gzip.open(CORPUS, "rt", encoding="utf-8") as fh:
        entries: list[dict[str, Any]] = json.load(fh)["entries"]
    return [e for e in entries if e["error"] is None]


ENTRIES = _entries()


def test_the_corpus_is_there() -> None:
    assert len(ENTRIES) >= 380


@pytest.mark.parametrize(
    "entry", ENTRIES, ids=[f"{i:03d}" for i in range(len(ENTRIES))]
)
def test_ml_off_is_the_v014_decision_byte_for_byte(entry: dict[str, Any]) -> None:
    request, snapshot, now = from_replay(entry["replay"])
    decision = plan(request, snapshot, entry["policy"], now, ml=None)
    assert decision.ml is None
    assert decision_view(decision) == entry["expected"], entry["source"]
    assert "ml" not in to_replay(request, snapshot, now, ml=decision.ml)


def _strip(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        row = {k: v for k, v in row.items() if k != "ml_model_version"}
        estimate = row.get("estimate")
        if estimate is not None:
            terms = {
                name: {
                    **term,
                    "detail": {k: v for k, v in term["detail"].items() if k != "ml"},
                }
                for name, term in estimate["terms"].items()
            }
            row["estimate"] = {**estimate, "terms": terms}
        out.append(row)
    return out


@pytest.mark.parametrize("entry", ENTRIES[::7], ids=lambda e: e["source"][:40])
def test_a_model_with_no_terms_changes_only_the_ml_records(
    entry: dict[str, Any],
) -> None:
    request, snapshot, now = from_replay(entry["replay"])
    plain = plan(request, snapshot, entry["policy"], now)
    ml, stub = stub_ml({})
    with_ml = plan(request, snapshot, entry["policy"], now, ml=ml)
    assert stub.calls == 0
    assert _strip(candidates_json(with_ml)) == candidates_json(plain)
    assert (None if with_ml.split is None else split_json(with_ml.split)) == (
        None if plain.split is None else split_json(plain.split)
    )
    assert with_ml.reason_key == plain.reason_key
