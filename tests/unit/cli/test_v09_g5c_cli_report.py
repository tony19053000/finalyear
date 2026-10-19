"""V0.9 G5c (0.9.8): ``compute bench report`` (real client, faked transport)."""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
import pytest
from mf_cli_support import TransportFactory
from typer.testing import CliRunner

from meshforge.cli.app import app
from meshforge.cli.bench_report_commands import config_rows, criteria_met, summary_lines

pytestmark = pytest.mark.unit

CAMP = "camp_01J0000000000000000000DDDD"
_MISSING0 = dict.fromkeys(
    (
        "total_ms",
        "compute_ns",
        "compute_iterations",
        "bytes_in",
        "bytes_out",
        "queue_ms",
        "run_ms",
        "node_state",
        "node_cpu_percent",
        "planner_node_id",
    ),
    0,
)


def _stats(median: float, cv: float | None) -> dict[str, Any]:
    return {
        "n": 3,
        "median": median,
        "mean": median,
        "std": None,
        "cv": cv,
        "ci_low": median - 1,
        "ci_high": median + 1,
        "ci_coverage": 0.75,
        "outliers": 0,
    }


def _config(cid: str, *, flagged: bool, **kw: Any) -> dict[str, Any]:
    base = {
        "group_id": "g1",
        "config_id": cid,
        "family": "cpu.sort",
        "condition": "none",
        "strategy": "local",
        "node_id": f"node_{cid}",
        "state": "measured",
        "planned": 3,
        "done": 3,
        "n": 3,
        "total_ms": _stats(100.0, 0.01),
        "compute_per_iter_ns": None,
        "outlier_attempt_ids": [],
        "cv_flag": False,
        "reps_flag": False,
        "flagged": flagged,
        "records_checked": 3,
        "missing": dict(_MISSING0),
    }
    base.update(kw)
    return base


DOC: dict[str, Any] = {
    "campaign_id": CAMP,
    "quality_version": 1,
    "summary": {
        "campaign_state": "done",
        "pending": False,
        "runs_by_state": {
            "planned": 0,
            "submitted": 0,
            "done": 9,
            "failed": 1,
            "skipped": 0,
        },
        "records": 11,
        "bench_role_records": {"filler": 2},
        "infra_failures": 1,
        "owner_preempted": 1,
        "infra_failure_rate": 0.1,
        "condition_failures": {"cpu_load:1": 1},
        "workload_failures": {},
        "crit1_infra_ok": False,
        "crit3_reps_ok": True,
        "configs": 3,
        "flagged_configs": 2,
        "cv_threshold": 0.1,
    },
    "configs": [
        _config("a", flagged=False),
        _config("b", flagged=True, cv_flag=True, total_ms=_stats(200.0, 0.4)),
        _config(
            "c",
            flagged=True,
            n=2,
            reps_flag=True,
            outlier_attempt_ids=["att_1"],
            missing={**_MISSING0, "compute_ns": 2},
        ),
    ],
}


def _serve(controller: TransportFactory, doc: dict[str, Any]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=doc)

    controller(handler)
    return seen


def test_summary_then_flagged_configs_first(
    runner: CliRunner, controller: TransportFactory
) -> None:
    seen = _serve(controller, DOC)
    result = runner.invoke(app, ["bench", "report", CAMP])
    assert result.exit_code == 0, result.output  # a failed criterion: still 0
    (request,) = seen
    assert request.url.path == f"/v1/bench/campaigns/{CAMP}/quality"
    out = result.stdout
    for text in (
        "infrastructure failures: 10.0 %",
        "criterion 1 (infra failures < 2 %): NOT met",
        "criterion 3 (every config >= 3 repetitions with a CV): met",
        "fillers / warm-ups apart: filler 2",
        "condition failures: cpu_load:1 1",
        "compute_ns 2",
    ):
        assert text in out, (text, out)
    order = [row[1] for row in config_rows(DOC)]
    assert order == ["local @ node_b", "local @ node_c", "local @ node_a"]
    assert [row[-1] for row in config_rows(DOC)] == ["cv", "reps,missing", "-"]


def test_json_is_the_document(runner: CliRunner, controller: TransportFactory) -> None:
    _serve(controller, DOC)
    result = runner.invoke(app, ["bench", "report", CAMP, "--json"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["quality_version"] == 1 and "schema_version" in body
    assert body["summary"]["crit1_infra_ok"] is False


@pytest.mark.parametrize(
    ("crit1", "crit3", "code"),
    [(True, True, 0), (False, True, 1), (True, False, 1), (None, None, 1)],
)
def test_strict_fails_unless_both_criteria_are_met(
    runner: CliRunner,
    controller: TransportFactory,
    crit1: bool | None,
    crit3: bool | None,
    code: int,
) -> None:
    doc = copy.deepcopy(DOC)
    doc["summary"].update(
        crit1_infra_ok=crit1, crit3_reps_ok=crit3, pending=crit1 is None
    )
    _serve(controller, doc)
    result = runner.invoke(app, ["bench", "report", CAMP, "--strict"])
    assert result.exit_code == code, result.output
    plain = runner.invoke(app, ["bench", "report", CAMP])
    assert plain.exit_code == 0
    assert criteria_met(doc) is (code == 0)


def test_pending_says_so() -> None:
    doc = copy.deepcopy(DOC)
    doc["summary"].update(pending=True, crit1_infra_ok=None, crit3_reps_ok=None)
    lines = summary_lines(doc)
    assert "verdicts pending" in lines[0]
    assert any(line.endswith(": pending") for line in lines)


def test_a_bad_campaign_id_is_a_usage_error(
    runner: CliRunner, controller: TransportFactory
) -> None:
    controller(lambda _r: pytest.fail("no request expected"))  # type: ignore[arg-type,return-value]
    assert runner.invoke(app, ["bench", "report", "../etc"]).exit_code == 2


def test_an_empty_report_renders() -> None:
    doc = {"campaign_id": CAMP, "summary": {}, "configs": []}
    assert config_rows(doc) == []
    assert summary_lines(doc)[0].startswith(CAMP)
