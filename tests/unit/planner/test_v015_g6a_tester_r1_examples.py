"""V0.15 G6a tester round 1: the examples builder's pure helpers (D375)."""

from __future__ import annotations

import json
import math

import pytest

from meshforge.controller.planner_eval import classifier_examples as ce

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("label", "enumerated", "expected"),
    [
        ("local", ["mode3_2"], "local"),
        ("mode1", [], "mode1"),
        ("mode3_3", ["mode3_3"], "mode3_3"),
        ("mode3_3", ["mode3_2", "mode3_4"], "mode3_2"),  # tie -> smaller
        ("mode3_4", ["mode3_2", "mode3_3"], "mode3_3"),
        ("mode3_9", ["mode3_2", "local"], "mode3_2"),
        ("mode3_3", ["local", "mode1"], "mode3_3"),  # nothing to map to
        ("mode3_x", ["mode3_2"], "mode3_x"),
        ("mode3_3", ["mode3_bad", "mode3_5"], "mode3_5"),
    ],
)
def test_nearest_class(label: str, enumerated: list[str], expected: str) -> None:
    assert ce.nearest_class(label, enumerated) == expected


def test_nearest_class_is_order_independent() -> None:
    a = ce.nearest_class("mode3_3", ["mode3_4", "mode3_2"])
    b = ce.nearest_class("mode3_3", ["mode3_2", "mode3_4"])
    assert a == b == "mode3_2"


def test_examples_document_turns_nan_into_null_and_is_json_clean() -> None:
    ex = ce.Example(
        campaign_id="c",
        group_id="g",
        family="f",
        fingerprint=None,
        label="local",
        oracle_class="local",
        records_used=1,
        features=(1.0, math.nan, math.inf, -math.inf),
        classes={},
        cost_model={"config_id": None, "status": "unmeasured", "regret_pct": None},
    )
    doc = ce.examples_document(
        [ex], {"groups": 1}, campaign_ids=["b", "a", "a"], calibrations={}
    )
    text = json.dumps(doc, allow_nan=False)  # raises on NaN/inf
    assert json.loads(text)["examples"][0]["features"] == [1.0, None, None, None]
    assert doc["campaign_ids"] == ["a", "b"]


def test_empty_build_is_empty_not_a_crash() -> None:
    examples, counts = ce.build_examples([], [], {})
    assert examples == [] and counts == {"examples": 0, "groups": 0}


def test_feature_names_are_unique_and_match_width() -> None:
    names = ce.FEATURE_NAMES
    assert len(names) == len(set(names))
    assert len(names) == len(ce.JOB_FEATURES) + 3 * len(ce.NODE_FEATURES) + 1
