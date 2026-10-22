"""V0.14 G4 fix round 1: ``compute planner reports`` (reviewer N6) and the
paired criterion-2 lines of ``compute planner eval`` (D322).

* ``reports [--limit N]`` asks ``GET /v1/planner/eval-reports?limit=N``
  (default 50, 1..200) and prints id, time, campaigns, held-out family,
  leaked, evaluated groups and both verdicts; an empty list says so;
* ``eval`` prints one "vs <rule policy>" line per paired comparison with the
  common groups and that policy's unmeasured count.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from mf_eval_world import fabric, group, records
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli.planner_commands import eval_lines
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.model import EvalConfig, default_policies

pytestmark = pytest.mark.unit

CAMP = "camp_01AAAAAAAAAAAAAAAAAAAAAAAA"


def test_reports_lists_stored_reports_newest_first(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[str] = []
    summary: dict[str, Any] = {
        "id": 9,
        "created_at": "2026-10-22T11:00:00Z",
        "campaign_ids": [CAMP],
        "holdout": "cpu.sort",
        "leaked": True,
        "eval_version": 1,
        "evaluated_groups": 14,
        "verdicts": [
            {"criterion": 1, "status": "leaked"},
            {"criterion": 2, "status": "not_separable"},
        ],
    }
    answers = iter([{"reports": [summary]}, {"reports": []}])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=next(answers))

    controller(handler)
    result = runner.invoke(cli_app.app, ["planner", "reports", "--limit", "5"])
    assert result.exit_code == 0, result.output
    assert seen[0].endswith("/v1/planner/eval-reports?limit=5")
    out = result.stdout
    assert "planner evaluation reports" in out
    for text in ("cpu.sort", "yes", "14", "leaked", "not_separable"):
        assert text in out, text
    result = runner.invoke(cli_app.app, ["planner", "reports"])
    assert result.exit_code == 0
    assert seen[1].endswith("limit=50")
    assert "no evaluation reports yet" in result.stdout
    bad = runner.invoke(cli_app.app, ["planner", "reports", "--limit", "0"])
    assert bad.exit_code == 2


def test_eval_prints_the_paired_comparisons() -> None:
    request, snapshot = fabric({"node_a": 100, "node_b": 10, "node_c": 50})
    g = group("g1", {"node_a": 300, "node_b": 100, "node_c": 200})
    report = evaluate(
        [g],
        records(g, request, snapshot),
        {},
        default_policies(),
        EvalConfig(bootstrap_resamples=50),
        campaign_ids=[CAMP],
    )
    lines = eval_lines({"id": 1, "report": report})
    paired = [line for line in lines if line.startswith("  vs ")]
    assert len(paired) == 6  # one per rule policy (D322)
    assert all("common groups" in line and "unmeasured" in line for line in paired)
    assert any("1 common groups" in line for line in paired)
