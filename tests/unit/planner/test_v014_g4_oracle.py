"""V0.14 G4 (D298): ``oracle`` is evaluation-only - nothing that places a real job
accepts it.

* not in :data:`~meshforge.common.manifest.POLICIES` (the one wire list), so
* a manifest ``policy: oracle`` is a validation error,
* ``POST /v1/jobs`` ``placement.policy: oracle`` is refused by the request
  model (the API answers ``400 invalid_input``, its convention for any refused
  body - component test ``test_v014_g4_oracle_api.py``),
* ``compute run --policy oracle`` is a usage error,
* the planner itself refuses it (``plan`` / ``rank`` raise),
* the ``plan_decision.policy`` CHECK cannot store it (the SQL list is built
  from ``POLICIES``);
* the evaluator still reports it as the reference row (regret 0).
"""

from __future__ import annotations

import pytest
from mf_eval_world import NOW, fabric
from pydantic import ValidationError

from meshforge.common.errors import MeshForgeError
from meshforge.common.manifest import POLICIES, parse_manifest_data
from meshforge.controller.planner import plan
from meshforge.controller.planner_eval.model import ORACLE, default_policies

pytestmark = pytest.mark.unit


def test_oracle_is_not_a_placement_policy() -> None:
    assert ORACLE == "oracle"
    assert ORACLE not in POLICIES
    assert ORACLE in {p.policy for p in default_policies()}


def test_a_manifest_naming_oracle_is_invalid() -> None:
    with pytest.raises(MeshForgeError) as caught:
        parse_manifest_data(
            {"name": "x", "entrypoint": ["python", "m.py"], "policy": "oracle"}
        )
    assert "policy" in str(caught.value)


def test_the_submit_request_refuses_oracle() -> None:
    pytest.importorskip("fastapi", reason="needs the controller extra")
    from meshforge.controller.api.job_models import PlacementRequest

    with pytest.raises(ValidationError, match="policy"):
        PlacementRequest.model_validate({"policy": "oracle"})
    assert PlacementRequest.model_validate({"policy": "cost_model"}).policy == (
        "cost_model"
    )


def test_the_planner_refuses_oracle() -> None:
    request, snapshot = fabric({"node_a": 1.0})
    with pytest.raises(ValueError, match="oracle"):
        plan(request, snapshot, ORACLE, NOW)


def test_the_decision_table_cannot_store_oracle() -> None:
    pytest.importorskip("sqlalchemy", reason="needs the controller extra")
    from meshforge.controller.store.models.plan import PlanDecision

    checks = [
        str(c.sqltext)
        for c in PlanDecision.__table__.constraints
        if getattr(c, "name", "") and "policy" in str(c.name)
    ]
    assert checks and all("'oracle'" not in c for c in checks)
    assert all("'cost_model'" in c for c in checks)


def test_compute_run_refuses_policy_oracle() -> None:
    from typer.testing import CliRunner

    from meshforge.cli import app as cli_app

    result = CliRunner().invoke(
        cli_app.app, ["run", ".", "--policy", "oracle"], catch_exceptions=False
    )
    assert result.exit_code == 2, result.output
    assert "--policy must be one of" in result.output
