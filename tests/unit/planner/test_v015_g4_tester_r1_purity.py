"""V0.15 G4 tester round 1: planner purity, checked dynamically.

The implementer's AST test looks at the import statements of each file. Here
the claim is held on what really happens:

* importing the planner package (in a clean interpreter) loads no ML library
  and nothing of ``meshforge.controller.ml`` (transitively);
* ``plan()`` with a predictor reads no clock and draws no random number
  (``time.*``, ``datetime.now``, ``random.*`` patched to raise) - the clock
  lives in the budget wrapper only;
* planning with a model does not mutate its inputs (request, snapshot,
  calibration, the predictor's rules) and is repeatable;
* no module of the planner package writes to stdout/stderr or logs.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from mf_ml_stub import Rule, stub_ml
from mf_planner_fabrics import synthetic_fabric
from mf_planner_golden import decision_view

import meshforge.controller.planner as planner_pkg
from meshforge.controller.planner import plan
from meshforge.controller.planner.predictor import ML_TERMS
from meshforge.controller.planner.replay import project

pytestmark = pytest.mark.unit

PLANNER = Path(planner_pkg.__file__).parent


def test_importing_the_planner_loads_no_ml_library_transitively() -> None:
    code = textwrap.dedent(
        """
        import sys
        import meshforge.controller.planner  # noqa: F401
        import meshforge.controller.planner.predictor  # noqa: F401
        import meshforge.controller.planner.plan  # noqa: F401
        import meshforge.controller.planner.split_plan  # noqa: F401
        import meshforge.controller.planner.replay  # noqa: F401
        bad = sorted(
            m for m in sys.modules
            if m.split(".")[0] in {
                "numpy", "scipy", "sklearn", "lightgbm", "xgboost", "pandas",
                "pyarrow", "torch", "threadpoolctl",
            }
            or m.startswith("meshforge.controller.ml")
        )
        print("BAD:" + ",".join(bad))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "BAD:", out


@pytest.mark.parametrize("path", sorted(PLANNER.glob("*.py")), ids=lambda p: p.name)
def test_no_planner_module_prints_logs_or_touches_the_clock_directly(
    path: Path,
) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned_mods = {"logging", "structlog", "time", "secrets", "threading"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in banned_mods, (
                    path.name,
                    alias.name,
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned_mods, (
                path.name,
                node.module,
            )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                assert func.id not in {"print", "open", "input", "exec", "eval"}, (
                    path.name,
                    func.id,
                )
            if isinstance(func, ast.Attribute) and func.attr in {
                "now",
                "utcnow",
                "today",
            }:
                # datetime.now() with no tz argument, or with one, is a clock
                # read either way: the planner is handed `now`
                raise AssertionError((path.name, func.attr, node.lineno))


def test_plan_with_a_model_reads_no_clock_and_no_random(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import datetime as dt
    import random
    import time

    request, snapshot, now = synthetic_fabric(4, split=True, seed=4)
    view = project(request, snapshot)
    ml, _ = stub_ml({(t, None): Rule(y_hat=-0.2) for t in ML_TERMS})

    def boom(*_a: object, **_k: object) -> float:
        raise AssertionError("the planner touched the clock / randomness")

    for name in ("time", "monotonic", "perf_counter", "time_ns", "monotonic_ns"):
        monkeypatch.setattr(time, name, boom)
    for name in ("random", "randint", "choice", "shuffle", "uniform", "sample"):
        monkeypatch.setattr(random, name, boom)

    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz: object = None) -> Clock:  # type: ignore[override]
            raise AssertionError("datetime.now in the planner")

    monkeypatch.setattr(dt, "datetime", Clock)
    decision = plan(request, view, "cost_model", now, ml=ml)
    assert decision.chosen is not None and decision.ml is not None


def test_planning_with_a_model_is_repeatable_and_mutates_nothing() -> None:
    import copy

    request, snapshot, now = synthetic_fabric(5, split=True, seed=5)
    view = project(request, snapshot)
    ml, stub = stub_ml({(t, None): Rule(y_hat=0.15) for t in ML_TERMS})
    before = (copy.deepcopy(request), copy.deepcopy(view), dict(stub.rules))
    first = plan(request, view, "cost_model", now, ml=ml)
    second = plan(request, view, "cost_model", now, ml=ml)
    assert decision_view(first) == decision_view(second)
    assert request == before[0] and view == before[1] and stub.rules == before[2]
    assert stub.calls == 4  # two decisions x (lanes, combines)
