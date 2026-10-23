"""V0.15 G5a: a controller without the ``ml`` extra evaluates exactly as
V0.14 (brief §5f G5a step 2).

In a fresh process where scikit-learn, LightGBM and XGBoost cannot be
imported, the evaluator service's ML input answers ``ml_unavailable``
(nothing trained, no child started), the report has no ML rows, both ML
verdicts are ``ml_unavailable`` (never ``FAIL``) and every V0.14 part of the
report - verdicts, policy rows, groups - equals the report made without any
ML input; and no ML library was imported on the way.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

pytestmark = pytest.mark.unit

SUPPORT = Path(__file__).resolve().parents[2] / "_support"

_CHILD = """
import asyncio, json, sys
for name in ("sklearn", "lightgbm", "xgboost"):
    sys.modules[name] = None  # any import fails
from mf_eval_world import fabric, group, records
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.ml_inputs import FoldMlInputs
from meshforge.controller.planner_eval.model import EvalConfig, default_policies
from meshforge.controller.settings import PlannerSettings

class Models:
    mode = "auto"

async def never(*_a, **_k):
    raise AssertionError("nothing may be built or trained")

inputs = FoldMlInputs(PlannerSettings(), Models(), None, never, child=never)
ml = asyncio.run(inputs.prepare({"cpu.matmul": frozenset()}, allow_leak=False))
request, snapshot = fabric({"node_a": 100.0, "node_b": 10.0, "node_c": 50.0})
groups = [group(f"g{i:02d}", {"node_a": 300.0, "node_b": 200.0, "node_c": 100.0})
          for i in range(12)]
recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
config = EvalConfig(bootstrap_resamples=200)
with_ml = evaluate(groups, recs, {}, default_policies(ml=True), config,
                   campaign_ids=["c"], ml=ml)
plain = evaluate(groups, recs, {}, default_policies(), config, campaign_ids=["c"])
loaded = sorted(m for m in sys.modules if sys.modules[m] is not None
                and m.split(".")[0] in ("sklearn", "lightgbm", "xgboost"))
print(json.dumps({
    "status": ml.status, "reason": ml.reason,
    "ml_verdicts": [v["status"] for v in with_ml["ml"]["verdicts"]],
    "same_verdicts": with_ml["verdicts"] == plain["verdicts"],
    "same_policies": with_ml["policies"] == plain["policies"],
    "same_groups": with_ml["groups"] == plain["groups"],
    "ml_rows": [p["key"] for p in with_ml["policies"] if ":ml:" in p["key"]],
    "loaded": loaded,
}))
"""


@pytest.mark.timeout(120)
def test_without_the_ml_extra_the_report_is_v014_plus_ml_unavailable() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SUPPORT), *filter(None, [env.get("PYTHONPATH")])]
    )
    done = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=110,
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out["status"] == "ml_unavailable"
    assert "sklearn" in out["reason"] and "lightgbm" in out["reason"]
    assert out["ml_verdicts"] == ["ml_unavailable", "ml_unavailable"]
    assert out["same_verdicts"] and out["same_policies"] and out["same_groups"]
    assert out["ml_rows"] == [] and out["loaded"] == []
