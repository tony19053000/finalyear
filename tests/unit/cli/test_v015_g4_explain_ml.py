"""V0.15 G4 (D336, D342 CLI part), text per G5b (D373): ``compute explain``
prints what the ML model did for each term (the shared explain-contract
fixture carries one term of each kind):

* corrected: ``ML <raw> -> <seconds> (<version>, x<factor>, +-<spread> %)``;
* not used: ``ML not used: <reason in words> (<ood reason>) (<version>...)``;
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
    assert "ML 2.53 s -> 2.00 s (mem-3a1f00aa, x0.79, +-18 %)" in text
    assert (
        "ML not used: out of distribution (range:hw_cpu_gflops) "
        "(mem-3a1f00aa, x0.82, +-11 %)"
    ) in text
    assert "ML not used: exact by construction (mem-3a1f00aa)" in text


def test_without_ml_nothing_is_printed() -> None:
    payload = copy.deepcopy(_payload())
    for decision in payload["decisions"]:
        decision["ml_model_version"] = decision["ml_fallback"] = None
        for row in decision["candidates"]:
            row.pop("ml_model_version", None)
            row.pop("ml_fallback", None)
            for term in ((row.get("estimate") or {}).get("terms") or {}).values():
                term["detail"].pop("ml", None)
                term["ml"] = None
    assert "ML" not in _text(payload)


def test_a_stored_row_without_the_typed_key_reads_detail_ml() -> None:
    block = {"used": False, "reason": "no_model", "model_version": "v"}
    assert _ml_note({"seconds": 1.0, "detail": {"ml": block}}) == (
        ", ML not used: no model for this term (v)"
    )
    # the typed key wins when present (even null: the answer said "none")
    assert _ml_note({"seconds": 1.0, "ml": None, "detail": {"ml": block}}) == ""


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        ("not a map", ""),
        ({"used": True, "model_version": "v", "y_hat": "x"}, ", ML - s -> 1.00 s (v)"),
        (
            {"used": True, "model_version": 5, "y_hat": 1e308, "lo": 0, "hi": 1},
            ", ML - s -> 1.00 s (?, +-65 %)",
        ),
        ({"used": False}, ", ML not used: ? (?)"),
        (
            {"used": False, "reason": "no_model"},
            ", ML not used: no model for this term (?)",
        ),
        ({"used": "yes", "reason": "brand_new"}, ", ML not used: brand_new (?)"),
    ],
)
def test_odd_ml_blocks_never_crash(block: Any, expected: str) -> None:
    assert _ml_note({"seconds": 1.0, "detail": {"ml": block}}) == expected
    assert _ml_note({"seconds": 1.0, "detail": "x"}) == ""
