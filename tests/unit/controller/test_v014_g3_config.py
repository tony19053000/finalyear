"""V0.14 G3 (D302 subset): the new ``[controller.planner]`` settings validate;
the manifest's ``objective:`` and ``placement.objective`` accept exactly the
three objectives and leave older jobs' canonical form / request hash as they
were."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.common.manifest import (
    OBJECTIVES,
    Manifest,
    ManifestError,
    parse_manifest_data,
)
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit


def test_defaults_are_the_brief_values() -> None:
    s = PlannerSettings()
    assert s.default_objective == "min_time"
    assert s.prefer_local_margin == 0.25
    assert s.risk_window_days == 7.0
    assert s.risk_prior == (1.0, 19.0)
    assert s.min_useful_bw_bytes_s == 1e6
    assert s.min_useful_bytes == 64 * 1024 * 1024
    assert s.decision_latency_budget_ms == 100.0


@pytest.mark.parametrize(
    "bad",
    [
        {"default_objective": "fastest"},
        {"prefer_local_margin": -0.1},
        {"prefer_local_margin": 0.96},
        {"risk_window_days": -1.0},
        {"risk_window_days": 400.0},
        {"risk_prior": (0.0, 19.0)},
        {"risk_prior": (1.0, 1001.0)},
        {"risk_prior": (1.0,)},
        {"min_useful_bw_bytes_s": -1.0},
        {"min_useful_bytes": -1},
        {"decision_latency_budget_ms": 0.0},
        {"decision_latency_budget_ms": 60_001.0},
    ],
    ids=repr,
)
def test_bad_values_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PlannerSettings(**bad)


def test_every_objective_is_a_valid_default() -> None:
    for objective in OBJECTIVES:
        assert (
            PlannerSettings(default_objective=objective).default_objective == objective
        )


def test_the_manifest_objective_is_validated_and_optional_in_the_hash() -> None:
    base = {"name": "demo", "entrypoint": ["python", "m.py"]}
    plain = parse_manifest_data(base)
    assert plain.objective is None
    assert "objective" not in plain.canonical_dict()
    for objective in OBJECTIVES:
        named = parse_manifest_data({**base, "objective": objective})
        assert named.canonical_dict()["objective"] == objective
    with pytest.raises(ManifestError):
        parse_manifest_data({**base, "objective": "fastest"})
    assert Manifest.model_validate(plain.canonical_dict()) == plain


def test_placement_objective_is_validated_and_left_out_when_absent() -> None:
    pytest.importorskip("fastapi", reason="the API models need the controller extra")
    from meshforge.controller.api.job_models import Placement, PlacementRequest

    assert PlacementRequest().objective is None
    assert PlacementRequest(objective="min_network").objective == "min_network"
    with pytest.raises(ValidationError):
        PlacementRequest(objective="fastest")  # type: ignore[arg-type]
    old = Placement(mode="auto", policy="cost_model")
    dumped = old.model_dump(mode="json", exclude_none=True)
    assert "objective" not in dumped and "objective" not in old.model_dump(mode="json")
    new = Placement(mode="auto", objective="prefer_local")
    assert new.model_dump(mode="json")["objective"] == "prefer_local"
