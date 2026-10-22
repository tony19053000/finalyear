"""V0.14 G5 (0.14.6, D300, D320, criterion 4): the decision page and the
Planner page end to end.

Real controller + 2 real agents (cluster harness, subprocess runtime), the
built SPA, real Chromium; the shared ``new_page`` guard checks console,
uncaught errors, CSP and axe on the final state.

1. **A finished split job's Plan tab** shows what ``GET /v1/jobs/{id}/
   decisions`` answers: the decision header (policy, objective, calibration,
   replay), the server's margin sentence verbatim, every candidate with the
   chosen row marked once, and ``compute explain --json`` is the same answer
   (identical fields, criterion 4); ``compute explain`` prints the same margin
   sentence.
2. **The Planner page** (reached from the main navigation) shows the
   calibration the API answers (here: none fitted yet, or its factors), and the
   evaluation reports: until V0.14 G4 is merged the controller has no
   ``/v1/planner/eval-reports`` route, so the reports are answered by a
   Playwright route with the D318 fixture (``frontend/src/test/fixtures/
   evalReports.ts`` shape) - the page then shows verdicts, the regret chart and
   the LEAKED banner. MERGE (Lead): drop the route stub once G4's API is on
   main and create a report with ``POST /v1/planner/eval-reports`` instead.
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_e2e import (
    EXEC_OPTIONS,
    assert_axe_clean,
    put_tree,
    run_compute,
    sign_in,
    submit_job,
    wait_all_reporting,
    wait_job_state,
)
from playwright.sync_api import expect

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Page, Route

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(360),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

ITEMS = 24
CHUNK = 2


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    return dict(EXEC_OPTIONS)


def _project(root: Path) -> Path:
    project = root / "split-plan"
    project.mkdir(parents=True)
    (project / "main.py").write_text(
        textwrap.dedent(
            """\
            import json, os, sys, time
            items = json.load(open(sys.argv[1]))
            time.sleep(0.5)
            with open(os.path.join(sys.argv[2], "out.txt"), "w") as fh:
                fh.write(" ".join(str(i) for i in items))
            """
        ),
        encoding="utf-8",
    )
    return project


def _verdicts(status: str) -> list[dict[str, Any]]:
    return [
        {
            "criterion": 1,
            "status": status,
            "would_be": "PASS" if status == "leaked" else None,
            "value": 83.3,
            "threshold": 80.0,
            "counted_groups": 12,
            "detail": "10 of 12 groups within 15 % of the oracle",
        },
        {
            "criterion": 2,
            "status": status if status == "leaked" else "not_separable",
            "would_be": "FAIL" if status == "leaked" else None,
            "value": 9.4,
            "threshold": 6.1,
            "counted_groups": 12,
            "detail": "CI upper 9.4 % >= best rule policy's CI lower 6.1 %",
        },
    ]


def _policy(
    key: str, kind: str, mean: float, low: float, high: float
) -> dict[str, Any]:
    return {
        "key": key,
        "policy": key.split(":")[0],
        "kind": kind,
        "calibrated": (":calibrated:" in key) if kind == "cost_model" else None,
        "objective": key.split(":")[2] if kind == "cost_model" else None,
        "counted_groups": 12,
        "unmeasured_groups": 0,
        "no_replay_groups": 0,
        "mean_regret_pct": mean,
        "ci_pct": {"low": low, "high": high},
        "mean_regret_ms": mean * 100,
        "within_threshold_pct": 80.0,
        "zero_regret_groups": 4,
    }


def _record(report_id: int, *, leaked: bool) -> dict[str, Any]:
    """A D318 ``EvalReportRecordOut`` (G4's ``planner_eval_models.py``)."""
    campaigns = ["camp_01JA0000000000000000000001"]
    return {
        "id": report_id,
        "created_at": "2026-10-22T08:00:00Z",
        "campaign_ids": campaigns,
        "holdout": None,
        "leaked": leaked,
        "report": {
            "eval_version": 1,
            "campaign_ids": campaigns,
            "holdout": None,
            "leaked": leaked,
            "settings": {
                "regret_threshold_pct": 15.0,
                "within_target_pct": 80.0,
                "min_groups": 10,
                "bootstrap_resamples": 10000,
                "bootstrap_seed": 20261022,
                "ci_level": 0.95,
                "criterion_policy": "cost_model:calibrated:min_time",
            },
            "counts": {
                "groups": 12,
                "evaluated_groups": 12,
                "labels": {"best": 10, "tie": 2},
                "no_replay_groups": 0,
                "records": 60,
                "records_used": 60,
                "records_skipped": {},
            },
            "verdicts": _verdicts("leaked" if leaked else "PASS"),
            "policies": [
                _policy("random", "rule", 41.5, 30.2, 55.0),
                _policy("least_loaded", "rule", 12.5, 6.1, 19.75),
                _policy("cost_model:calibrated:min_time", "cost_model", 4.5, 1.25, 9.4),
                _policy("oracle", "oracle", 0.0, 0.0, 0.0),
            ],
            "families": [],
            "calibrations": [
                {
                    "family": "cpu_sleep",
                    "source": "active" if leaked else "refit",
                    "version": 7 if leaked else None,
                    "fitted_runs": 40,
                    "excluded_runs": 0 if leaked else 20,
                    "factors": 9,
                    "digest": None,
                }
            ],
            "groups": [],
        },
    }


def _summary(record: dict[str, Any]) -> dict[str, Any]:
    report = record["report"]
    return {
        "id": record["id"],
        "created_at": record["created_at"],
        "campaign_ids": record["campaign_ids"],
        "holdout": record["holdout"],
        "leaked": record["leaked"],
        "eval_version": 1,
        "evaluated_groups": report["counts"]["evaluated_groups"],
        "verdicts": report["verdicts"],
    }


def _stub_eval_reports(page: Page) -> None:
    """MERGE: until G4's routes are on main (see module docstring)."""
    records = {5: _record(5, leaked=True), 4: _record(4, leaked=False)}

    def answer(route: Route) -> None:
        path = route.request.url.split("?")[0]
        tail = path.rsplit("/", 1)[-1]
        if tail == "eval-reports":
            body: Any = {"reports": [_summary(r) for r in records.values()]}
        elif tail.isdigit() and int(tail) in records:
            body = records[int(tail)]
        else:
            route.fulfill(status=404, body="{}", content_type="application/json")
            return
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(body)
        )

    page.route(re.compile(r"/v1/planner/eval-reports(/\d+)?(\?.*)?$"), answer)


def test_plan_tab_of_a_split_job_and_the_planner_page(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    wait_all_reporting(fabric, 2)
    tree_id = put_tree(fabric, _project(tmp_path))
    job_id = submit_job(
        fabric,
        tree_id,
        "split-plan-e2e",
        entrypoint=["python", "main.py", "{items_file}", "{out}"],
        env={"base": "python-cpu"},
        resources={"gpu": "none", "cpus": 1},
        limits={"timeout": "5m"},
        split={"over": {"range": {"stop": ITEMS}}, "chunk": {"items": CHUNK}},
    )
    wait_job_state(fabric, job_id, {"succeeded"}, 180)
    api = fabric.call("GET", f"/v1/jobs/{job_id}/decisions").json()
    assert api["count"] >= 1, api
    last = api["decisions"][-1]
    sentence = last["margin_sentence"]
    assert isinstance(sentence, str) and sentence.startswith(
        ("split over", "not split")
    )
    assert last["replay_version"] is not None  # every V0.14 decision is replayable

    # --- criterion 4: the CLI answers the same fields, prints the same sentence
    explained = run_compute(fabric, "explain", job_id, "--json", cwd=tmp_path)
    assert explained.returncode == 0, explained.stderr
    cli = json.loads(explained.stdout)
    cli.pop("schema_version", None)
    assert cli == api
    printed = run_compute(fabric, "explain", job_id, cwd=tmp_path)
    assert printed.returncode == 0, printed.stderr
    assert " ".join(sentence.split()[:6]) in " ".join(printed.stdout.split())

    # --- the Plan tab shows the same decision
    page, _watch = new_page()
    sign_in(page, fabric, path=f"/jobs/{job_id}")
    expect(page.get_by_role("heading", level=1, name="split-plan-e2e")).to_be_visible()
    page.get_by_role("tablist", name="Job details").get_by_role(
        "tab", name="Plan"
    ).click()
    section = page.get_by_role("region", name=f"Decision {last['id']}")
    expect(section).to_be_visible()
    expect(section.locator("p", has_text="Margin test:")).to_contain_text(sentence)
    expect(section).to_contain_text(f"replay v{last['replay_version']}")
    expect(section).to_contain_text(
        "uncalibrated"
        if last["calibration_version"] is None
        else f"calibration v{last['calibration_version']}"
    )
    if last["objective"] is not None:
        expect(section).to_contain_text(last["objective"])
    table = section.get_by_role("table", name=re.compile("Candidates of decision"))
    expect(table.locator("tbody tr")).to_have_count(len(last["candidates"]))
    if last["chosen_node_id"] is not None:
        expect(table.get_by_text("chosen", exact=True)).to_have_count(1)
        expect(table.locator('tr[aria-current="true"]')).to_contain_text(
            last["chosen_node_id"]
        )
    for candidate in last["candidates"]:
        expect(table).to_contain_text(candidate["node_id"])
    assert_axe_clean(page, "the Plan tab of a split job")

    # --- the Planner page, from the main navigation
    _stub_eval_reports(page)
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Planner", exact=True
    ).click()
    expect(page.get_by_role("heading", level=1, name="Planner")).to_be_visible()
    calibration = fabric.call("GET", "/v1/planner/calibration").json()
    if calibration["calibration"] is None:
        expect(page.get_by_text("No calibration yet")).to_be_visible()
    else:
        version = calibration["calibration"]["version"]
        factors = page.get_by_role(
            "table", name=f"Calibration factors of version {version}"
        )
        expect(factors.locator("tbody tr")).to_have_count(
            max(1, len(calibration["calibration"]["factors"]))
        )
    expect(page.get_by_test_id("calibration-in-use")).to_contain_text(
        f"mode {calibration['in_use']['mode']}"
    )
    # the newest report is leaked: banner + stamped verdicts
    banner = page.get_by_role("alert")
    expect(banner).to_contain_text("LEAKED")
    detail = page.get_by_test_id("eval-report-detail")
    expect(detail.get_by_text("criterion 1: leaked (would be PASS)")).to_be_visible()
    expect(detail.get_by_test_id("regret-chart")).to_be_visible()
    # the clean report: no banner, its verdicts
    page.get_by_role("button", name="#4").click()
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(detail.get_by_text("criterion 1: PASS")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/planner\?report=4$"))
    assert_axe_clean(page, "the Planner page")
