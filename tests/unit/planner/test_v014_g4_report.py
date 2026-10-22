"""V0.14 G4 (D317, D318): the report artefact.

* byte-identical canonical JSON for the same inputs at the real 10 000
  resamples (bootstrap determinism), and a different seed is a different
  report (the seed is really used and written in it);
* the report validates against the D318 API model (the contract G5 builds on),
  every field of the model is produced (no optional field silently missing);
* the Markdown names the verdicts and policies, escapes table-breaking text;
* the pure modules import only the stdlib, ``meshforge.common``,
  ``meshforge.bench.stats``, the planner and each other (AST test).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from mf_eval_world import fabric, group, records

pytest.importorskip("fastapi", reason="needs the controller extra")

from meshforge.controller import planner_eval
from meshforge.controller.api.planner_eval_models import EvalReportOut
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.model import (
    EVAL_VERSION,
    EvalConfig,
    default_policies,
)
from meshforge.controller.planner_eval.report import render_markdown, report_text

pytestmark = pytest.mark.unit

FAST_B = {"node_a": 100.0, "node_b": 10.0, "node_c": 50.0, "node_d": 80.0}
PURE = (
    "model.py",
    "mapping.py",
    "heldout.py",
    "core.py",
    "report.py",
    "regret_gate.py",
    "latency.py",  # V0.14 G6
)


def _report(config: EvalConfig | None = None, **kw: object) -> dict:  # type: ignore[type-arg]
    request, snapshot = fabric(FAST_B)
    groups = [
        group(f"g{i:02d}", {"node_a": 300 + 7 * i, "node_b": 100 + i, "node_c": 120})
        for i in range(12)
    ]
    groups.append(group("gx", {"node_a": 1}, label="insufficient", reason="n<3"))
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    return evaluate(
        groups,
        recs,
        {},
        default_policies(),
        config or EvalConfig(),
        campaign_ids=["camp_2", "camp_1", "camp_1"],
        **kw,  # type: ignore[arg-type]
    )


def test_same_inputs_same_bytes_at_the_real_resample_count() -> None:
    first, second = report_text(_report()), report_text(_report())
    assert first == second
    data = json.loads(first)
    assert data["settings"]["bootstrap_resamples"] == 10_000
    assert data["campaign_ids"] == ["camp_1", "camp_2"]  # sorted, deduplicated
    other = report_text(_report(EvalConfig(bootstrap_seed=1)))
    assert other != first
    assert json.loads(other)["settings"]["bootstrap_seed"] == 1


def test_the_report_validates_against_the_api_model_field_for_field() -> None:
    report = _report()
    model = EvalReportOut.model_validate(report)
    assert model.eval_version == EVAL_VERSION == 1
    # the model's JSON is the report: nothing dropped, nothing invented
    assert json.loads(model.model_dump_json()) == json.loads(report_text(report))
    leaked = EvalReportOut.model_validate(_report(leaked=True))
    assert {v.status for v in leaked.verdicts} == {"leaked"}


def test_markdown_lists_verdicts_policies_and_escapes_cells() -> None:
    report = _report()
    text = render_markdown(report)
    assert text.startswith("# Planner evaluation report\n")
    assert "## Verdicts" in text and "| 1 | PASS |" in text
    for row in report["policies"]:
        assert f"`{row['key']}`" in text
    assert "## Groups not counted" in text and "insufficient: n<3" in text
    report["groups"][-1]["excluded_reason"] = "a | b\nc"
    assert "a \\| b c" in render_markdown(report)


def test_pure_modules_import_nothing_impure() -> None:
    root = Path(planner_eval.__file__).parent
    allowed_std = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "hashlib",
        "json",
        "math",
        "typing",
    }
    for name in PURE:
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for item in ast.walk(tree):
            modules: list[str] = []
            if isinstance(item, ast.Import):
                modules = [a.name for a in item.names]
            elif isinstance(item, ast.ImportFrom) and item.module:
                modules = [item.module]
            for module in modules:
                if module.split(".")[0] == "meshforge":
                    assert module.startswith(
                        (
                            "meshforge.common",
                            "meshforge.bench.stats",
                            "meshforge.controller.planner.",
                            "meshforge.controller.planner_eval.",
                        )
                    ), f"{name} imports {module}"
                    assert not module.startswith(
                        (
                            "meshforge.controller.planner_eval.loader",
                            "meshforge.controller.planner_eval.store",
                            "meshforge.controller.planner_eval.service",
                        )
                    ), f"{name} imports {module}"
                else:
                    assert module.split(".")[0] in allowed_std, (
                        f"{name} imports {module}"
                    )
