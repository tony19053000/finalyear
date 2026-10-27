"""V0.9 G6 (0.9.9, D221): the Benchmarks page end to end.

Real controller + 2 real agents (cluster harness, subprocess runtime, the real
benchmark workload library), the built SPA, real Chromium. The shared
``new_page`` guard checks every page at the end (console, uncaught errors, CSP
- Recharts included -, axe on the final state).

1. A seeded campaign (``cpu.primes``, local + mode1 from node A, 3
   repetitions = 6 real runs) driven from the browser: list -> detail ->
   Start -> Pause -> Resume (each checked against the API) -> it completes
   live -> labels visible and equal to the API's -> the Quality tab shows
   G5c's report (every record bucket, summing to ``records``) -> both downloads are
   Parquet files byte-identical to the API's export (and their rows match).
2. Controls reflect state and refusals are shown: a campaign cancelled behind
   the page's back refuses Start with 409 - the toast carries the server's
   message; Cancel asks first and then cancels on the server; a user sees the
   standard admins-only state and no nav entry.
"""

from __future__ import annotations

import io
import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")
pytest.importorskip("pyarrow", reason="needs pyarrow (controller extra)")

import pyarrow.parquet as pa_parquet
from mf_cluster import NORMAL_USER
from mf_e2e import EXEC_OPTIONS, assert_axe_clean, sign_in, wait_all_reporting
from playwright.sync_api import expect

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(600),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

RUNS = 6


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    return {**EXEC_OPTIONS, "controller_extra": {"bench": {"tick_s": 0.5}}}


def _spec(name: str, origin: str, *, strategies: str = "[local, mode1]") -> str:
    return (
        f"name: {name}\n"
        "seed: 66\n"
        "families:\n"
        "  - family: cpu.primes\n"
        f"strategies: {strategies}\n"
        f"origin: {origin}\n"
        "repetitions: 3\n"
        "budget: {max_runs: 20, max_hours: 2}\n"
    )


def _create(fabric: Fabric, spec: str) -> dict[str, Any]:
    resp = fabric.call("POST", "/v1/bench/campaigns", json={"spec": spec})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _status(fabric: Fabric, cid: str) -> dict[str, Any]:
    resp = fabric.call("GET", f"/v1/bench/campaigns/{cid}/status")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _wait_status(
    fabric: Fabric, cid: str, until: Any, within: float, what: str
) -> dict[str, Any]:
    deadline = time.monotonic() + within
    while True:
        status = _status(fabric, cid)
        if until(status):
            return status
        assert time.monotonic() < deadline, f"{what}: {status}"
        time.sleep(0.5)


def _row(page: Page, name: str) -> Any:
    return page.get_by_role("row").filter(
        has=page.get_by_role("link", name=name, exact=True)
    )


def _short(value: str, keep: int = 6) -> str:
    """lib/format.ts ``shortId``: ``node_01J...XYZ`` -> ``node_…XYZ123``."""
    head, sep, _ = value.partition("_")
    if not sep or len(value) <= len(head) + 1 + keep:
        return value
    return f"{head}_…{value[-keep:]}"


def _parquet_rows(data: bytes) -> list[dict[str, Any]]:
    return list(pa_parquet.read_table(io.BytesIO(data)).to_pylist())


def test_seeded_campaign_list_detail_pause_resume_labels_download(
    fabric: Fabric, new_page: NewPage
) -> None:
    wait_all_reporting(fabric, 2)
    origin = fabric.nodes[0].node_id
    created = _create(fabric, _spec("g6-e2e", origin))
    cid = created["campaign_id"]
    assert created["run_count"] == RUNS

    page, watch = new_page()
    sign_in(page, fabric)

    # List: reached from the nav; the row's numbers are the API's.
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Benchmarks"
    ).click()
    expect(page.get_by_role("heading", level=1, name="Benchmarks")).to_be_visible()
    row = _row(page, "g6-e2e")
    expect(row).to_contain_text(cid)
    expect(row).to_contain_text(f"0 / {RUNS}")
    expect(row).to_contain_text("not started")
    expect(row.get_by_role("button", name="Start campaign g6-e2e")).to_be_visible()
    assert_axe_clean(page, "the campaigns list")

    # Detail.
    row.get_by_role("link", name="g6-e2e").click()
    expect(page.get_by_role("heading", level=1, name="g6-e2e")).to_be_visible()
    expect(page.get_by_test_id("overview-budget")).to_have_text("max 20 runs, max 2 h")
    assert_axe_clean(page, "the campaign overview")

    # Start -> the runner owns it; Pause -> paused; Resume -> active again.
    page.get_by_role("button", name="Start campaign g6-e2e").click()
    expect(
        # exact: the toast's aria-live announcer repeats it with a "Notification" prefix
        page.get_by_text(
            "Campaign started: the controller's runner submits its runs", exact=True
        )
    ).to_be_visible()
    pause = page.get_by_role("button", name="Pause campaign g6-e2e")
    expect(pause).to_be_visible()
    assert _status(fabric, cid)["runner_owned"] is True
    pause.click()
    expect(page.get_by_role("button", name="Resume campaign g6-e2e")).to_be_visible()
    paused = _status(fabric, cid)
    assert paused["state"] == "paused", paused
    page.get_by_role("button", name="Resume campaign g6-e2e").click()
    expect(page.get_by_role("button", name="Pause campaign g6-e2e")).to_be_visible()
    assert _status(fabric, cid)["state"] == "active"

    # The campaign completes; the page follows live (campaign.progress).
    done = _wait_status(
        fabric,
        cid,
        lambda s: s["state"] == "done",
        within=420,
        what="the campaign did not complete",
    )
    assert done["finished_reason"] == "complete", done
    assert done["counts"]["done"] == RUNS, done
    expect(page.get_by_test_id("overview-done")).to_contain_text(
        "100.0 %", timeout=30_000
    )
    expect(page.get_by_test_id("overview-finished")).to_have_text("complete")
    expect(page.get_by_test_id("count-done")).to_have_text(str(RUNS))
    expect(page.get_by_test_id("overview-infra-rate")).to_have_text(
        "—"
        if done["infra_failure_rate"] is None
        else f"{done['infra_failure_rate'] * 100:.1f} %"
    )

    # Runs: every run with its state and tries, as the API lists them.
    page.get_by_role("tab", name="Runs").click()
    runs = fabric.call(
        "GET", f"/v1/bench/campaigns/{cid}/runs", params={"limit": 100}
    ).json()["runs"]
    table = page.get_by_role("table", name="Campaign runs in execution order")
    expect(table.get_by_role("row")).to_have_count(len(runs) + 1)
    assert all(r["state"] == "done" for r in runs), runs
    for i, run in enumerate(runs, start=1):  # row 0 is the header
        cells = table.get_by_role("row").nth(i).get_by_role("cell")
        expect(cells.nth(0)).to_have_text(str(run["seq"]))
        expect(cells.nth(3)).to_have_text(_short(run["node_id"]))
        expect(cells.nth(4)).to_have_text(run["strategy"])
        expect(cells.nth(6)).to_have_text("done")
        expect(cells.nth(7)).to_have_text(str(run["tries"]))

    # Labels: the API's labels, group by group.
    page.get_by_role("tab", name="Labels").click()
    labels = fabric.call("GET", f"/v1/bench/campaigns/{cid}/labels").json()
    assert labels["groups"], labels
    expect(page.get_by_test_id("labels-summary")).to_contain_text(
        f"from {labels['rows']} run records"
    )
    ltable = page.get_by_role("table", name="Per-group labels")
    expect(ltable.get_by_role("row")).to_have_count(len(labels["groups"]) + 1)
    for group in labels["groups"]:
        expect(ltable).to_contain_text(group["label"])
        expect(ltable).to_contain_text(group["family"])
    assert_axe_clean(page, "the labels tab")

    # Charts: Recharts under the controller's CSP (the page guard checks it).
    page.get_by_role("tab", name="Charts").click()
    expect(page.get_by_test_id("strategy-chart")).to_be_visible()
    expect(page.get_by_test_id("strategy-chart").locator("svg").first).to_be_visible()
    assert_axe_clean(page, "the charts tab")

    # Quality: G5c's real report for this campaign - the page's numbers are
    # the API's, and every record bucket is shown so they add up to `records`.
    page.get_by_role("tab", name="Quality").click()
    quality = fabric.call("GET", f"/v1/bench/campaigns/{cid}/quality")
    assert quality.status_code == 200, quality.text
    report = quality.json()
    summary = report["summary"]
    assert report["quality_version"] == 1 and not summary["pending"], summary
    assert summary["records"] >= RUNS and summary["measured_records"] >= RUNS
    buckets = {
        "measured": summary["measured_records"],
        "infra": summary["infra_failures"],
        "preempted": summary["owner_preempted"],
        "condition": sum(summary["condition_failures"].values()),
        "workload": sum(summary["workload_failures"].values()),
        "rejected": sum(summary["rejected_ok"].values()),
        "other": sum(summary["other_failures"].values()),
    }
    assert sum(buckets.values()) == summary["records"], summary
    expect(page.get_by_test_id("quality-records")).to_have_text(str(summary["records"]))
    for bucket, count in buckets.items():
        expect(page.get_by_test_id(f"quality-bucket-{bucket}")).to_have_text(str(count))
    expect(page.get_by_test_id("quality-campaign-state")).to_have_text(
        summary["campaign_state"]
    )
    expect(page.get_by_test_id("quality-crit1")).to_have_text(
        "met" if summary["crit1_infra_ok"] else "not met"
    )
    expect(page.get_by_test_id("quality-crit3")).to_have_text(
        "met" if summary["crit3_reps_ok"] else "not met"
    )
    qtable = page.get_by_role(
        "table", name="Data quality per configuration, flagged first"
    )
    expect(qtable.get_by_role("row")).to_have_count(len(report["configs"]) + 1)
    for config in report["configs"]:
        assert config["n"] == 3, config  # 3 real repetitions each
    expect(
        page.get_by_text("Quality report not available on this controller yet")
    ).to_have_count(0)
    assert_axe_clean(page, "the quality tab")

    # Download: the browser's files ARE the API's exports.
    page.get_by_role("tab", name="Download").click()
    with page.expect_download() as info:
        page.get_by_role("button", name="Download records (Parquet)").click()
    assert info.value.suggested_filename == f"{cid}-records.parquet"
    records = Path(info.value.path()).read_bytes()
    api_records = fabric.call(
        "GET", "/v1/telemetry/export", params={"campaign": cid}
    ).content
    assert records == api_records
    rows = _parquet_rows(records)
    assert {r["campaign_id"] for r in rows} == {cid}
    assert len(rows) == len(_parquet_rows(api_records)) >= RUNS

    with page.expect_download() as info:
        page.get_by_role("button", name="Download labels (Parquet)").click()
    assert info.value.suggested_filename == f"{cid}-labels.parquet"
    label_bytes = Path(info.value.path()).read_bytes()
    api_labels = fabric.call(
        "GET", f"/v1/bench/campaigns/{cid}/labels", params={"format": "parquet"}
    ).content
    assert label_bytes == api_labels
    label_rows = _parquet_rows(label_bytes)
    assert {r["group_id"] for r in label_rows} == {
        g["group_id"] for g in labels["groups"]
    }
    expect(page.get_by_text(f"Saved {cid}-labels.parquet", exact=True)).to_be_visible()
    watch.assert_clean()


def test_controls_follow_state_refusals_toast_and_users_see_no_benchmarks(
    fabric: Fabric, new_page: NewPage
) -> None:
    wait_all_reporting(fabric, 2)
    origin = fabric.nodes[0].node_id
    cid = _create(fabric, _spec("g6-refused", origin, strategies="[local]"))[
        "campaign_id"
    ]
    other = _create(fabric, _spec("g6-cancel", origin, strategies="[local]"))[
        "campaign_id"
    ]

    page, watch = new_page()
    sign_in(page, fabric, path="/benchmarks")
    row = _row(page, "g6-refused")
    start = row.get_by_role("button", name="Start campaign g6-refused")
    expect(start).to_be_visible()
    expect(row.get_by_role("button", name="Pause campaign g6-refused")).to_have_count(0)

    # Cancelled behind the page's back (a not-started campaign sends no
    # campaign.progress; the page polls only every 15 s): Start is refused.
    resp = fabric.call("POST", f"/v1/bench/campaigns/{cid}/cancel")
    assert resp.status_code == 200, resp.text
    watch.allow(
        "409",
        f"/v1/bench/campaigns/{cid}/start",
        "the campaign was cancelled through the API: start is refused (the test's point)",
    )
    start.click()
    expect(page.get_by_text("Start refused", exact=True)).to_be_visible()
    expect(
        page.get_by_text(re.compile(r"the campaign is cancelled; it cannot start"))
    ).to_be_visible()
    # The refresh after the refusal shows the real state: no controls left.
    page.reload()
    row = _row(page, "g6-refused")
    expect(row).to_contain_text("cancelled")
    expect(row.get_by_role("button")).to_have_count(0)

    # Cancel from the browser: asks first ("Keep running" sends nothing), then
    # the campaign is cancelled on the server and its row loses its controls.
    row = _row(page, "g6-cancel")
    row.get_by_role("button", name="Cancel campaign g6-cancel").click()
    dialog = page.get_by_role("dialog", name="Cancel this campaign?")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="Keep running").click()
    expect(dialog).to_be_hidden()
    assert _status(fabric, other)["state"] == "active"
    row.get_by_role("button", name="Cancel campaign g6-cancel").click()
    dialog.get_by_role("button", name="Cancel campaign", exact=True).click()
    expect(page.get_by_text("Campaign cancelled", exact=True)).to_be_visible()
    cancelled = _status(fabric, other)
    assert cancelled["state"] == "cancelled", cancelled
    assert cancelled["counts"]["skipped"] == 3, cancelled  # its open runs
    expect(row).to_contain_text("cancelled")
    expect(row.get_by_role("button")).to_have_count(0)
    watch.assert_clean()

    # A user: no nav entry, the standard admins-only state, no bench call.
    user_page, user_watch = new_page()
    bench_calls: list[str] = []
    user_page.on(
        "request",
        lambda r: bench_calls.append(r.url) if "/v1/bench" in r.url else None,
    )
    sign_in(user_page, fabric, NORMAL_USER)
    nav = user_page.get_by_role("navigation", name="Main")
    expect(nav.get_by_role("link", name="Jobs")).to_be_visible()
    expect(nav.get_by_role("link", name="Benchmarks")).to_have_count(0)
    user_page.goto(fabric.http_base + "/benchmarks")
    expect(user_page.get_by_text("Benchmarks are run by admins")).to_be_visible()
    assert bench_calls == []
    user_watch.assert_clean()
