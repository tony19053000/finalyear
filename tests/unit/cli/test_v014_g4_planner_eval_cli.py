"""V0.14 G4: ``compute planner eval``.

* POSTs ``/v1/planner/eval-reports`` with every ``--campaign``, ``--holdout``
  and ``--allow-leak``; prints the report id, counts, both verdicts and the
  per-policy table (mean regret, CI, within %, unmeasured, no replay);
* a leaked report prints the LEAKED line and "would be";
* ``--md PATH`` fetches the stored report's Markdown and writes it (an
  existing file is refused before any request);
* ``--json`` prints the API document; errors are one stderr line + exit code;
* an accepted calibration row's D319 note prints as ``note:``, never
  ``rejected:``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from mf_eval_world import fabric, group, records
from typer.testing import CliRunner

from meshforge.cli import app as cli_app
from meshforge.cli.planner_commands import calibration_lines
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.model import EvalConfig, default_policies

pytestmark = pytest.mark.unit

CAMP = "camp_01AAAAAAAAAAAAAAAAAAAAAAAA"


def _record(*, leaked: bool = False) -> dict[str, Any]:
    request, snapshot = fabric({"node_a": 100, "node_b": 10, "node_c": 50})
    g = group("g1", {"node_a": 300, "node_b": 100, "node_c": 200})
    report = evaluate(
        [g],
        records(g, request, snapshot),
        {},
        default_policies(),
        EvalConfig(bootstrap_resamples=50),
        campaign_ids=[CAMP],
        leaked=leaked,
    )
    return {
        "id": 7,
        "created_at": "2026-10-22T10:00:00Z",
        "campaign_ids": report["campaign_ids"],
        "holdout": None,
        "leaked": leaked,
        "report": report,
    }


def test_eval_posts_the_request_and_prints_verdicts_and_table(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        return httpx.Response(201, json=_record())

    controller(handler)
    result = runner.invoke(
        cli_app.app,
        [
            "planner",
            "eval",
            "--campaign",
            CAMP,
            "--campaign",
            "camp_01BBBBBBBBBBBBBBBBBBBBBBBB",
            "--holdout",
            "cpu.matmul",
        ],
    )
    assert result.exit_code == 0, result.output
    assert seen == [
        (
            "POST",
            "/v1/planner/eval-reports",
            {
                "campaign_ids": [CAMP, "camp_01BBBBBBBBBBBBBBBBBBBBBBBB"],
                "allow_leak": False,
                "holdout": "cpu.matmul",
            },
        )
    ]
    out = result.stdout
    assert "report 7: campaigns " + CAMP in out
    assert "groups 1: evaluated 1, no replay 0; records 9 of 9 replayed" in out
    assert "criterion 1: too_few_groups" in out
    assert "criterion 2: too_few_groups" in out
    assert "cost_model:calibrated:min_time" in out and "round_robin" in out
    assert "200.00" in out  # round_robin's regret
    assert "LEAKED" not in out


def test_a_leaked_report_says_so(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json=_record(leaked=True))

    controller(handler)
    result = runner.invoke(
        cli_app.app, ["planner", "eval", "--campaign", CAMP, "--allow-leak"]
    )
    assert result.exit_code == 0, result.output
    assert seen[0]["allow_leak"] is True and "holdout" not in seen[0]
    assert "LEAKED:" in result.stdout
    assert "criterion 1: leaked (would be too_few_groups)" in result.stdout


def test_md_writes_the_markdown_and_never_overwrites(
    runner: CliRunner, controller: TransportFactory, tmp_path: Path
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        if request.method == "POST":
            return httpx.Response(201, json=_record())
        return httpx.Response(200, text="# Planner evaluation report\n")

    controller(handler)
    target = tmp_path / "report.md"
    result = runner.invoke(
        cli_app.app, ["planner", "eval", "--campaign", CAMP, "--md", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert target.read_text(encoding="utf-8") == "# Planner evaluation report\n"
    assert paths[1].endswith("/v1/planner/eval-reports/7?format=markdown")
    assert f"wrote {target}" in result.stdout
    paths.clear()
    again = runner.invoke(
        cli_app.app, ["planner", "eval", "--campaign", CAMP, "--md", str(target)]
    )
    assert again.exit_code == 2 and paths == []  # refused before any request


def test_json_and_errors(runner: CliRunner, controller: TransportFactory) -> None:
    answers = iter(
        [
            httpx.Response(201, json=_record()),
            httpx.Response(
                404,
                json={"error": {"code": "not_found", "message": "no such campaign"}},
            ),
        ]
    )
    controller(lambda _r: next(answers))
    result = runner.invoke(
        cli_app.app, ["planner", "eval", "--campaign", CAMP, "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["report"]["eval_version"] == 1
    result = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", CAMP])
    assert result.exit_code == 8 and result.stdout == ""
    bad = runner.invoke(cli_app.app, ["planner", "eval", "--campaign", "../x"])
    assert bad.exit_code == 2


def test_an_accepted_rows_note_is_not_called_rejected() -> None:
    base = {
        "version": 3,
        "status": "active",
        "created_at": "t",
        "fit_inputs": {},
        "reason": "regret check skipped (too_few_groups: 2 < 10)",
    }
    lines = calibration_lines(base)
    assert "note: regret check skipped (too_few_groups: 2 < 10)" in lines
    assert not any(line.startswith("rejected:") for line in lines)
    rejected = calibration_lines({**base, "status": "rejected", "reason": "x"})
    assert "rejected: x" in rejected
