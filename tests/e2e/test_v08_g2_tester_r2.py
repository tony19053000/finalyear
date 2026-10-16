"""V0.8 G2 tester r2: controller restarts in a real browser (stable REST port
via record_links, as a real controller keeps its configured port)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("playwright", reason="needs the dev dependency group")
pytest.importorskip("psutil")
pytest.importorskip("sqlalchemy")

from mf_cluster import ADMIN_USER
from mf_dashboard import PageWatch
from playwright.sync_api import Browser, Page, expect

if TYPE_CHECKING:
    from mf_cluster import Fabric

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(400),
    pytest.mark.parametrize("fabric_options", [{"record_links": True}]),
]

NOISE = re.compile(r"ERR_|WebSocket connection|Failed to load resource")


def _signed_in(fabric: Fabric, browser: Browser) -> tuple[Page, PageWatch]:
    page = browser.new_context(reduced_motion="reduce").new_page()
    page.set_default_timeout(15_000)
    watch = PageWatch.attach(page)
    watch.allow("status of 401", "/v1/session")
    page.goto(fabric.http_base + "/nodes")
    page.get_by_label("User name").fill(ADMIN_USER)
    page.get_by_label("Password").fill(fabric.password(ADMIN_USER))
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("current-user")).to_be_visible()
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )
    return page, watch


def _only_expected_noise(watch: PageWatch) -> None:
    unexpected = [e for e in watch.console_errors if not NOISE.search(e)]
    assert unexpected == [], unexpected
    assert watch.page_errors == [] and watch.csp_violations() == []


def test_restart_with_expired_session_lands_on_login_not_a_stuck_shell(
    fabric: Fabric, dashboard_browser: Browser
) -> None:
    page, watch = _signed_in(fabric, dashboard_browser)
    fabric.kill_controller()
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "reconnecting", timeout=15_000
    )
    page.context.clear_cookies(name="mf_session")  # the session is gone
    fabric.restart_controller(graceful=False)
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible(timeout=90_000)
    assert page.url == fabric.http_base + "/login", page.url
    expect(page.get_by_test_id("current-user")).to_have_count(0)
    expect(page.get_by_text("Cannot reach the controller")).to_have_count(0)
    # and signing in again works on the restarted controller
    page.get_by_label("User name").fill(ADMIN_USER)
    page.get_by_label("Password").fill(fabric.password(ADMIN_USER))
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live", timeout=30_000
    )
    watch.allow("status of 401", "/v1/nodes")
    watch.allow("status of 401", "/v1/auth/whoami")
    _only_expected_noise(watch)


def test_long_outage_then_recovery(fabric: Fabric, dashboard_browser: Browser) -> None:
    page, watch = _signed_in(fabric, dashboard_browser)
    fabric.kill_controller()
    status = page.get_by_test_id("stream-status")
    expect(status).to_have_attribute("data-status", "reconnecting", timeout=15_000)
    page.wait_for_timeout(60_000)  # backoff has reached its 15 s cap
    expect(status).to_have_attribute("data-status", "reconnecting")
    expect(page.get_by_text("Cannot reach the controller")).to_have_count(0)
    expect(page.get_by_test_id("current-user")).to_have_text(f"{ADMIN_USER}(admin)")
    # the page is still usable offline: navigation and theme work
    page.get_by_role("link", name="Jobs").click()
    expect(page.get_by_role("heading", level=1, name="Jobs")).to_be_visible()
    fabric.restart_controller(graceful=False)
    expect(status).to_have_attribute("data-status", "live", timeout=60_000)
    expect(page.get_by_test_id("fabric-status")).to_contain_text("node")
    _only_expected_noise(watch)


def test_rapid_repeated_restarts(fabric: Fabric, dashboard_browser: Browser) -> None:
    page, watch = _signed_in(fabric, dashboard_browser)
    status = page.get_by_test_id("stream-status")
    for _ in range(3):
        fabric.restart_controller(graceful=False)
        expect(status).to_have_attribute("data-status", "live", timeout=90_000)
        expect(page.get_by_test_id("current-user")).to_have_text(f"{ADMIN_USER}(admin)")
    assert page.url.endswith("/nodes")
    _only_expected_noise(watch)


def test_restart_during_login(fabric: Fabric, dashboard_browser: Browser) -> None:
    page = dashboard_browser.new_context(reduced_motion="reduce").new_page()
    page.set_default_timeout(15_000)
    watch = PageWatch.attach(page)
    watch.allow("status of 401", "/v1/session")
    page.goto(fabric.http_base + "/")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    fabric.kill_controller()
    page.get_by_label("User name").fill(ADMIN_USER)
    page.get_by_label("Password").fill(fabric.password(ADMIN_USER))
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_contain_text("did not answer")
    fabric.restart_controller(graceful=False)
    page.get_by_label("Password").fill(fabric.password(ADMIN_USER))
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("current-user")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live", timeout=30_000
    )
    _only_expected_noise(watch)
