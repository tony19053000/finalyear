"""V0.14 criterion 4 (D300, D320): every decision is explainable in the UI and
in ``compute explain`` **with identical fields** - the contract test.

One fixture, ``frontend/src/test/fixtures/explainContract.json``
(``tests/_support/mf_explain_contract.py``), read by three checks:

1. **API** - the fixture's ``payload`` is byte-for-byte what the controller's
   answer code produces for its records, and validates as the answer model.
2. **Field set** - ``fields`` (shown) + ``not_shown`` (with reasons) is exactly
   the answer model's leaf set; every model leaf is non-null somewhere in the
   payload (so no nested part of the answer goes untested).
3. **CLI** - for every listed field, changing that one value in the payload
   changes what ``compute explain`` prints (so the CLI shows it).

The Plan tab runs check 3 on the same file
(``frontend/src/components/jobs/planContract.test.tsx``). A new answer field
fails check 2 until it is added to the fixture and shown by both (or listed
in ``not_shown`` with a reason, for both).
"""

from __future__ import annotations

import copy
import io
import json
from typing import Any

import pytest
from mf_explain_contract import (
    FIXTURE,
    NOT_SHOWN,
    fixture_leaves,
    model_leaves,
    normalise,
    text,
)
from rich.console import Console

from meshforge.cli.explain_commands import decision_tables
from meshforge.controller.api.decision_routes import DecisionList

pytestmark = pytest.mark.unit

CONTRACT: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
FIELDS: list[dict[str, Any]] = CONTRACT["fields"]


def _cli_text(payload: dict[str, Any]) -> str:
    out = io.StringIO()
    console = Console(file=out, width=2000, color_system=None)
    for table in decision_tables(payload):
        console.print(table)
    return out.getvalue()


def _set(data: Any, path: list[Any], value: Any) -> None:
    for token in path[:-1]:
        data = data[token]
    data[path[-1]] = value


def test_the_fixture_is_what_the_controller_answers() -> None:
    assert FIXTURE.read_text(encoding="utf-8") == text(), (
        "stale fixture: run `uv run python tests/_support/mf_explain_contract.py`"
    )
    payload = CONTRACT["payload"]
    assert DecisionList.model_validate(payload).model_dump(mode="json") == payload


def test_shown_plus_not_shown_is_exactly_the_answer_model() -> None:
    model = set(model_leaves(DecisionList))
    shown = {f["field"] for f in FIELDS}
    hidden = {item["field"] for item in CONTRACT["not_shown"]}
    assert hidden == set(NOT_SHOWN)
    assert not shown & hidden
    assert shown | hidden == model, {
        "in the model, not in the contract": sorted(model - shown - hidden),
        "in the contract, not in the model": sorted((shown | hidden) - model),
    }
    assert all(reason for reason in NOT_SHOWN.values())


def test_every_model_leaf_is_filled_somewhere_in_the_payload() -> None:
    filled = {
        normalise(tuple(path))
        for path, value in fixture_leaves(CONTRACT["payload"])
        if value is not None
    }
    assert set(model_leaves(DecisionList)) <= filled


def test_every_field_path_points_at_that_field() -> None:
    for field in FIELDS:
        assert normalise(tuple(field["path"])) == field["field"]


@pytest.mark.parametrize("field", FIELDS, ids=[f["field"] for f in FIELDS])
def test_compute_explain_shows_the_field(field: dict[str, Any]) -> None:
    base = _cli_text(CONTRACT["payload"])
    changed = copy.deepcopy(CONTRACT["payload"])
    _set(changed, field["path"], field["to"])
    assert _cli_text(changed) != base, f"compute explain does not show {field}"


def test_the_cli_prints_the_servers_sentences_verbatim() -> None:
    payload = copy.deepcopy(CONTRACT["payload"])
    payload["decisions"][1]["margin_sentence"] = "SENTENCE-FROM-THE-SERVER"
    terms = payload["decisions"][0]["candidates"][0]["estimate"]["terms"]
    terms["transfer"]["note"] = "NOTE-FROM-THE-SERVER"
    printed = _cli_text(payload)
    assert "SENTENCE-FROM-THE-SERVER" in printed
    assert "NOTE-FROM-THE-SERVER" in printed
    assert "split over 3 nodes" not in printed  # nothing recomputed beside it
