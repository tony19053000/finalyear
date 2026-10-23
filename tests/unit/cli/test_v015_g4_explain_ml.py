"""V0.15 G4 (D336, D342 CLI part): ``compute explain`` prints what the ML
model did for each term, from the stored ``detail.ml`` of the decisions
answer (the shared explain-contract fixture carries one term of each kind):

* corrected: ``ML <raw> -> <seconds> (<version>, x<factor>, +-<spread> %)``;
* not used: ``ML not used: <reason>`` (+ the OOD test's reason);
* a decision without a model prints nothing new; odd values never crash.
"""

from __future__ import annotations

import copy
import io
import json
from typing import Any

import pytest
from mf_explain_contract import FIXTURE
from rich.console import Console

from meshforge.cli.explain_commands import _ml_note, decision_tables

pytestmark = pytest.mark.unit


def _text(payload: dict[str, Any]) -> str:
    out = io.StringIO()
    console = Console(file=out, width=2000, color_system=None)
    for table in decision_tables(payload):
        console.print(table)
    return out.getvalue()


def _payload() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))["payload"]
    return data


def test_explain_prints_every_kind_of_ml_line() -> None:
    text = _text(_payload())
    assert "ML 2.53s -> 2.00s (mem-3a1f00aa, x0.79, +-18 %)" in text
    assert "ML not used: ood (range:hw_cpu_gflops)" in text
    assert "ML not used: exact_by_construction" in text


def test_without_ml_nothing_is_printed() -> None:
    payload = copy.deepcopy(_payload())
    for decision in payload["decisions"]:
        for row in decision["candidates"]:
            for term in ((row.get("estimate") or {}).get("terms") or {}).values():
                term["detail"].pop("ml", None)
    assert "ML" not in _text(payload)


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("not a map", ""),
        ({"used": True, "model_version": "v", "y_hat": "x"}, ", ML v"),
        (
            {"used": True, "model_version": 5, "y_hat": 1e308, "lo": 0, "hi": 1},
            ", ML ?",
        ),
        ({"used": False}, ", ML not used: ?"),
        ({"used": False, "reason": "no_model"}, ", ML not used: no_model"),
    ],
)
def test_odd_ml_blocks_never_crash(block: Any, expected: str) -> None:
    assert _ml_note({"seconds": 1.0, "detail": {"ml": block}}) == expected
    assert _ml_note({"seconds": 1.0, "detail": "x"}) == ""
