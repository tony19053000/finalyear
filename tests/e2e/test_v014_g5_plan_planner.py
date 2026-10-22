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
   evaluation reports the real evaluator stored (D321: no route stub): a real
   ``cpu.primes`` benchmark campaign runs to completion, ``POST
   /v1/planner/eval-reports`` evaluates it twice (clean, then
   ``allow_leak``), and the page shows the newest (leaked: banner, every
   verdict stamped), then the clean one (its verdicts, criterion 2's paired
   rows, the CI labelled with the report's level), and "Report #N not found"
   for an id the controller does not have.
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
import time
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

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(720),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

ITEMS = 24
CHUNK = 2
CAMPAIGN_WITHIN_S = 420.0


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    return {**EXEC_OPTIONS, "controller_extra": {"bench": {"tick_s": 0.5}}}


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


def _campaign_spec(origin: str) -> str:
    return (
        "name: g5-planner-e2e\n"
        "seed: 66\n"
        "families:\n"
        "  - family: cpu.primes\n"
        "strategies: [local, mode1]\n"
        f"origin: {origin}\n"
        "repetitions: 3\n"
        "budget: {max_runs: 20, max_hours: 2}\n"
    )


def _finished_campaign(fabric: Fabric) -> str:
    """A real benchmark campaign (6 real runs, each with its plan decision and
    replay), run to completion by the controller's campaign runner."""
    created = fabric.call(
        "POST",
        "/v1/bench/campaigns",
        json={"spec": _campaign_spec(fabric.nodes[0].node_id)},
    )
    assert created.status_code == 201, created.text
    cid = str(created.json()["campaign_id"])
    started = fabric.call("POST", f"/v1/bench/campaigns/{cid}/start")
    assert started.status_code in (200, 202), started.text
    deadline = time.monotonic() + CAMPAIGN_WITHIN_S
    while True:
        status = fabric.call("GET", f"/v1/bench/campaigns/{cid}/status").json()
        if status["state"] == "done":
            assert status["finished_reason"] == "complete", status
            return cid
        assert time.monotonic() < deadline, f"campaign did not complete: {status}"
        time.sleep(0.5)


def _evaluate(fabric: Fabric, cid: str, *, allow_leak: bool) -> dict[str, Any]:
    """``POST /v1/planner/eval-reports`` (admin): the real evaluator stores
    the report (D321: no route stub)."""
    resp = fabric.call(
        "POST",
        "/v1/planner/eval-reports",
        json={"campaign_ids": [cid], "allow_leak": allow_leak},
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _verdict_text(v: dict[str, Any]) -> str:
    """The page's ``verdictText`` (plannerView.ts)."""
    would = (
        f" (would be {v['would_be']})"
        if v["status"] == "leaked" and v["would_be"] is not None
        else ""
    )
    return f"criterion {v['criterion']}: {v['status']}{would}"


def _ci_label(level: float) -> str:
    """The page's ``ciLabel`` (plannerView.ts)."""
    return f"{float(f'{level * 100:.1f}'):g} % CI"


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
    page, watch = new_page()
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

    # --- two real stored reports (clean, then leaked = newest)
    cid = _finished_campaign(fabric)
    clean = _evaluate(fabric, cid, allow_leak=False)
    leaked = _evaluate(fabric, cid, allow_leak=True)
    assert clean["leaked"] is False and leaked["leaked"] is True
    listed = fabric.call("GET", "/v1/planner/eval-reports").json()["reports"]
    assert [r["id"] for r in listed[:2]] == [leaked["id"], clean["id"]], listed

    # --- the Planner page, from the main navigation
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
    # the newest report is leaked: banner + every verdict stamped (the API's)
    banner = page.get_by_role("alert")
    expect(banner).to_contain_text("LEAKED")
    detail = page.get_by_test_id("eval-report-detail")
    expect(detail).to_contain_text(f"Report #{leaked['id']}")
    for verdict in leaked["report"]["verdicts"]:
        assert verdict["status"] == "leaked", verdict
        expect(detail.get_by_text(_verdict_text(verdict), exact=True)).to_be_visible()
    policies = detail.get_by_role("table", name="Regret per policy")
    expect(policies.locator("tbody tr")).to_have_count(
        len(leaked["report"]["policies"])
    )
    ci = _ci_label(leaked["report"]["settings"]["ci_level"])
    expect(policies.get_by_role("columnheader", name=ci)).to_be_visible()
    # the clean report: no banner, its own verdicts, criterion 2's paired rows
    page.get_by_role("button", name=f"#{clean['id']}", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/planner\?report={clean['id']}$"))
    expect(page.get_by_role("alert")).to_have_count(0)
    expect(detail).to_contain_text(f"Report #{clean['id']}")
    for verdict in clean["report"]["verdicts"]:
        expect(detail.get_by_text(_verdict_text(verdict), exact=True)).to_be_visible()
    paired = next(
        v.get("comparisons") or []
        for v in clean["report"]["verdicts"]
        if v["criterion"] == 2
    )
    paired_table = detail.get_by_role(
        "table", name="Criterion 2: paired regret difference per rule policy"
    )
    if paired:
        expect(paired_table.locator("tbody tr")).to_have_count(len(paired))
        for comparison in paired:
            expect(paired_table).to_contain_text(comparison["key"])
    else:
        expect(paired_table).to_have_count(0)
    # an unknown report id: "not found", the list stays (D321)
    missing = max(leaked["id"], clean["id"]) + 1000
    assert (
        fabric.call("GET", f"/v1/planner/eval-reports/{missing}").json()["error"][
            "reason"
        ]
        == "unknown_eval_report"
    )
    watch.allow(
        "status of 404",
        f"/v1/planner/eval-reports/{missing}",
        "the page asks for a report id the controller does not have",
    )
    page.goto(page.url.split("?")[0] + f"?report={missing}")
    expect(page.get_by_text(f"Report #{missing} not found")).to_be_visible()
    expect(
        page.get_by_role("table", name="Evaluation reports, newest first")
    ).to_be_visible()
    assert_axe_clean(page, "the Planner page")
