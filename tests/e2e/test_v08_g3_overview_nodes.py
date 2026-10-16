"""V0.8 G3 (0.8.3, 0.8.4, live wiring of 0.8.7): Overview, Nodes and Node
detail end to end - a real controller + 2 real agents (cluster harness), the
built SPA, real Chromium.

* Every number checked here is compared with what the API returns at that
  moment (checklist 26).
* Live: a node killed -> its status changes in the page without a reload,
  within 2 s of the API showing it; a heartbeat sample -> visible in the
  node page within 2 s (both measured with a MutationObserver in the page
  against the same machine clock; the numbers are printed).
* Revoke by keyboard only; Radix Tabs and the native <dialog> under the CSP;
  axe-core clean on Overview, Nodes and every Node-detail tab.
* No console error of any kind is allowed (D189/D191 b: a signed-out first
  visit no longer logs a 401 - `GET /v1/session` answers `{session: null}`).
"""

from __future__ import annotations

import re
import statistics
import time
from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import NORMAL_USER
from mf_e2e import api_nodes, assert_axe_clean, sign_in, tab_to, wait_all_reporting
from playwright.sync_api import expect

from meshforge.common.errors import ExitCode

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage

pytestmark = [pytest.mark.e2e, pytest.mark.docker, pytest.mark.timeout(300)]

TWO_AGENTS = pytest.mark.parametrize("fabric_options", [{"agents": 2}])
ONE_AGENT = pytest.mark.parametrize("fabric_options", [{"agents": 1}])
LIVE_BUDGET_MS = 2000.0
"""Criterion 2: a fabric change is visible in the page within 2 s."""


@pytest.mark.docker
@TWO_AGENTS
def test_overview_and_nodes_show_what_the_api_returns(
    fabric: Fabric, new_page: NewPage
) -> None:
    nodes = wait_all_reporting(fabric, 2)
    page, watch = new_page()
    sign_in(page, fabric)

    # --- Overview: every figure is the API's.
    online = sum(1 for n in nodes if n["state"] == "online")
    expect(page.get_by_test_id("nodes-total")).to_have_text(str(len(nodes)))
    expect(page.get_by_test_id("nodes-online")).to_have_text(str(online))
    expect(page.get_by_test_id("health-fabric")).to_contain_text(
        f"All {len(nodes)} nodes online"
    )
    health = fabric.get("/healthz")
    assert health is not None
    expect(page.get_by_test_id("health-controller")).to_have_text("OK")
    expect(page.get_by_test_id("health-card")).to_contain_text(
        f"{health['sessions']} agent sessions connected"
    )
    active = fabric.get(
        "/v1/jobs",
        state=[
            "submitted",
            "planning",
            "queued",
            "preparing",
            "running",
            "collecting",
            "retrying",
        ],
        limit=500,
    )
    assert active is not None
    expect(page.get_by_test_id("jobs-active")).to_have_text(str(len(active["jobs"])))
    expect(page.get_by_test_id("util-reporting")).to_have_text(
        "From 2 of 2 nodes reporting now."
    )
    expect(page.get_by_test_id("util-cpu")).to_have_text(re.compile(r"^\d+%$"))
    alerts = fabric.get("/v1/alerts")
    assert alerts is not None and alerts["alerts"] == []
    expect(page.get_by_test_id("alerts-empty")).to_be_visible()
    assert_axe_clean(page, "the Overview")

    # --- Nodes: one row per API node, sorted and filtered in the page.
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Nodes"
    ).click()
    expect(page.get_by_role("heading", level=1, name="Nodes")).to_be_visible()
    table = page.get_by_role("table")
    expect(table.locator("tbody tr")).to_have_count(len(nodes))
    for node in nodes:
        row = table.locator("tbody tr").filter(
            has=page.locator(f'[title="{node["node_id"]}"]')
        )
        expect(row).to_have_count(1)
        expect(row.locator("[data-state]")).to_have_attribute(
            "data-state", node["state"]
        )
        expect(row).to_contain_text(node["os"])
    header = table.get_by_role("columnheader", name=re.compile("CPU"))
    expect(header).to_have_attribute("aria-sort", "none")
    header.get_by_role("button").click()
    expect(header).to_have_attribute("aria-sort", "ascending")
    header.get_by_role("button").click()
    expect(header).to_have_attribute("aria-sort", "descending")
    expect(page).to_have_url(re.compile(r"sort=cpu&dir=desc"))
    page.get_by_role("button", name=re.compile(r"^online \(2\)$")).click()
    expect(table.locator("tbody tr")).to_have_count(2)
    assert_axe_clean(page, "the Nodes list")

    # --- Node detail: every tab renders (Radix Tabs under the CSP), axe-clean.
    first = nodes[0]
    table.get_by_role("link").first.click()
    expect(page).to_have_url(re.compile(r"/nodes/node_"))
    shown_id = page.url.rsplit("/", 1)[-1]
    detail = fabric.get(f"/v1/nodes/{shown_id}")
    assert detail is not None
    expect(page.get_by_role("heading", level=1)).to_have_text(detail["hostname"])
    expect(
        page.get_by_test_id("node-status").locator("[data-state]")
    ).to_have_attribute("data-state", detail["state"])
    expect(page.get_by_test_id("live-metrics")).to_be_visible()
    assert_axe_clean(page, "the Node detail (live)")
    tabs = page.get_by_role("tablist", name="Node details")
    for name, marker in (
        ("Hardware", "hardware-card"),
        ("Network", "network-card"),
        ("Caches & policy", "policy-card"),
    ):
        tabs.get_by_role("tab", name=name).click()
        expect(page.get_by_test_id(marker)).to_be_visible()
        assert_axe_clean(page, f"the Node detail ({name})")
    # Arrow keys move between tabs (Radix roving focus).
    tabs.get_by_role("tab", name="Caches & policy").focus()
    page.keyboard.press("ArrowLeft")
    expect(tabs.get_by_role("tab", name="Network")).to_have_attribute(
        "aria-selected", "true"
    )
    # The policy card is the API's owner_policy (read-only).
    if detail.get("owner_policy"):
        tabs.get_by_role("tab", name="Caches & policy").click()
        card = page.get_by_test_id("policy-card")
        for key in detail["owner_policy"]:
            expect(card).to_contain_text(key)
    assert first["node_id"]
    watch.assert_clean()


@pytest.mark.docker
@TWO_AGENTS
def test_a_killed_node_changes_in_the_page_within_2_s(
    fabric: Fabric, new_page: NewPage
) -> None:
    wait_all_reporting(fabric, 2)
    victim = fabric.nodes[1]
    page, watch = new_page()
    sign_in(page, fabric)
    page.goto(fabric.http_base + "/nodes")
    status = page.locator(f'tr:has([title="{victim.node_id}"]) [data-state]')
    expect(status).to_have_attribute("data-connected", "true")
    page.evaluate(
        """(sel) => {
          window.__mfChange = null;
          const target = document.querySelector(sel).closest("tr");
          const obs = new MutationObserver(() => {
            const el = target.querySelector("[data-state]");
            if (el && (el.dataset.connected === "false" || el.dataset.state !== "online")) {
              window.__mfChange = Date.now();
              obs.disconnect();
            }
          });
          obs.observe(target, {subtree: true, attributes: true, childList: true});
        }""",
        f'[title="{victim.node_id}"]',
    )

    killed_ms = time.time() * 1000
    victim.kill()
    api_ms: float | None = None
    deadline = time.monotonic() + 20
    while api_ms is None and time.monotonic() < deadline:
        row = {n["node_id"]: n for n in api_nodes(fabric)}[victim.node_id]
        if not row["connected"] or row["state"] != "online":
            api_ms = time.time() * 1000
        else:
            time.sleep(0.05)
    assert api_ms is not None, "the API never showed the node gone"
    page.wait_for_function("() => window.__mfChange !== null", timeout=10_000)
    ui_ms = float(page.evaluate("window.__mfChange"))
    print(  # noqa: T201 - a measured number for the handoff (run with -s)
        f"\n[G3 live] node killed -> page changed after {ui_ms - killed_ms:.0f} ms; "
        f"API first showed it after {api_ms - killed_ms:.0f} ms (polled every 50 ms)"
    )
    assert ui_ms - api_ms <= LIVE_BUDGET_MS, (ui_ms - api_ms, ui_ms - killed_ms)
    # Without a reload: the same document, the stream still live.
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )
    watch.assert_clean()


@pytest.mark.docker
@pytest.mark.parametrize(
    "fabric_options",
    # Report every sample (no change threshold), once a second: the agent's
    # default only reports a change > 5 % or every 10 s, too few for a sample.
    [
        {
            "agents": 1,
            "agent_resources": {
                "sample_interval_s": 1.0,
                "change_threshold_percent": 0.0,
                "max_report_interval_s": 1.0,
            },
        }
    ],
)
def test_a_heartbeat_sample_is_visible_within_2_s(
    fabric: Fabric, new_page: NewPage
) -> None:
    node = wait_all_reporting(fabric, 1)[0]
    page, watch = new_page()
    sign_in(page, fabric)
    page.goto(f"{fabric.http_base}/nodes/{node['node_id']}")
    expect(page.get_by_test_id("latest-sample")).to_be_visible()
    page.evaluate(
        """() => {
          window.__mfLag = [];
          const card = document.querySelector('[data-testid="live-metrics"]');
          let last = null;
          const note = () => {
            const t = card.querySelector('[data-testid="latest-sample"]');
            const at = t && t.getAttribute("datetime");
            if (at && at !== last) {
              if (last !== null) window.__mfLag.push(Date.now() - Date.parse(at));
              last = at;
            }
          };
          note();
          new MutationObserver(note).observe(
            card, {subtree: true, attributes: true, childList: true, characterData: true});
        }"""
    )
    page.wait_for_function("() => window.__mfLag.length >= 6", timeout=40_000)
    lags = [float(x) for x in page.evaluate("window.__mfLag")]
    p95 = statistics.quantiles(lags, n=20, method="inclusive")[-1]
    print(  # noqa: T201 - a measured number for the handoff (run with -s)
        f"\n[G3 live] sample observed -> visible in the page: "
        f"min {min(lags):.0f} / median {statistics.median(lags):.0f} / "
        f"p95 {p95:.0f} / max {max(lags):.0f} ms over {len(lags)} samples"
    )
    assert max(lags) <= LIVE_BUDGET_MS, lags
    watch.assert_clean()


@pytest.mark.docker
@TWO_AGENTS
def test_revoke_by_keyboard_only(fabric: Fabric, new_page: NewPage) -> None:
    wait_all_reporting(fabric, 2)
    victim = fabric.nodes[1]
    detail = fabric.get(f"/v1/nodes/{victim.node_id}")
    assert detail is not None
    page, watch = new_page()
    sign_in(page, fabric)
    page.goto(f"{fabric.http_base}/nodes/{victim.node_id}")
    expect(page.get_by_role("heading", level=1)).to_have_text(detail["hostname"])
    # The agent is still running (V0.8 G5, D195): revoking it cuts its live
    # session and the agent exits cleanly (`agent.revoked`, no traceback - the
    # harness's invariant check at teardown refuses any traceback).
    expect(
        page.get_by_test_id("node-status").locator("[data-state]")
    ).to_have_attribute("data-connected", "true")

    trigger = page.get_by_role("button", name="Revoke node")
    tab_to(page, "Revoke node", limit=40)
    expect(trigger).to_be_focused()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    confirm = dialog.get_by_role("button", name="Revoke permanently")
    expect(confirm).to_be_disabled()
    field = dialog.get_by_role("textbox")
    expect(field).to_be_focused()
    page.keyboard.type(detail["hostname"])
    expect(confirm).to_be_enabled()
    assert_axe_clean(page, "the revoke dialog")
    page.keyboard.press("Enter")

    expect(
        page.get_by_text(f"{detail['hostname']} revoked", exact=True)
    ).to_be_visible()
    expect(dialog).to_be_hidden()
    expect(
        page.get_by_test_id("node-status").locator("[data-state]")
    ).to_have_attribute("data-state", "revoked", timeout=10_000)
    after = fabric.get(f"/v1/nodes/{victim.node_id}")
    assert after is not None and after["state"] == "revoked"
    # A revoked node has no Revoke button any more.
    expect(page.get_by_role("button", name="Revoke node")).to_have_count(0)
    # The running agent stopped by itself, for good, with the contract exit
    # code and a clean `agent.revoked` line (no traceback).
    assert victim.process.wait_exit(30) == int(ExitCode.CONFIG_ERROR)
    victim.process.expected_alive = False
    events = [r.get("event") for r in victim.process.logs[-1].records()]
    assert "agent.revoked" in events, events
    assert "agent.exited_with_error" not in events, events
    watch.assert_clean()


@pytest.mark.docker
@ONE_AGENT
def test_a_user_sees_no_revoke_and_no_alerts(fabric: Fabric, new_page: NewPage) -> None:
    node = wait_all_reporting(fabric, 1)[0]
    page, watch = new_page()
    sign_in(page, fabric, NORMAL_USER)
    expect(page.get_by_test_id("alerts-card")).to_contain_text(
        "Alerts are shown to admins only."
    )
    page.goto(f"{fabric.http_base}/nodes/{node['node_id']}")
    expect(page.get_by_test_id("summary-card")).to_be_visible()
    expect(page.get_by_role("button", name="Revoke node")).to_have_count(0)
    assert_axe_clean(page, "the Node detail (user)")
    watch.assert_clean()


@pytest.mark.docker
def test_signed_out_first_visit_logs_nothing(fabric: Fabric, new_page: NewPage) -> None:
    """D191 b: the boot probe is `200 {session: null}` - no 401 console line,
    so no test needs the old allowance for it."""
    page, watch = new_page()
    for path in ("/", "/nodes", "/nodes/node_01J00000000000000000000000"):
        page.goto(fabric.http_base + path)
        expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    assert watch.allowed == []
    watch.assert_clean()
