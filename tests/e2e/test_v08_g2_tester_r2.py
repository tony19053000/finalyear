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
from mf_e2e import OUTAGE_NOISE, fill_login, sign_in
from playwright.sync_api import expect

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_dashboard import PageWatch
    from mf_e2e import NewPage
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(400),
    pytest.mark.parametrize("fabric_options", [{"record_links": True}]),
]

NOISE = re.compile(OUTAGE_NOISE)


def _signed_in(fabric: Fabric, new_page: NewPage) -> tuple[Page, PageWatch]:
    page, watch = new_page()
    # A restart's network errors are expected (also for the end-of-test guard).
    watch.allow_matching(
        NOISE.pattern, "the restart's refused connections", may_not_occur=True
    )
    sign_in(page, fabric, path="/nodes")
    return page, watch


def _only_expected_noise(watch: PageWatch) -> None:
    unexpected = watch.unexpected_console_errors()  # the outage noise + declared
    assert unexpected == [], unexpected
    assert watch.page_errors == [] and watch.csp_violations() == []


def test_restart_with_expired_session_lands_on_login_not_a_stuck_shell(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = _signed_in(fabric, new_page)
    # The dead session's reads after the restart are refused (401); which ones
    # are in flight is timing. (Before G7 these were declared at the end, where
    # they never applied; the broad noise pattern hid the lines instead.)
    for path in ("/v1/nodes", "/v1/auth/whoami"):
        watch.allow(
            "status of 401", path, "the dead session's reads", may_not_occur=True
        )
    # ... and its event stream's reconnect is refused at the handshake (403);
    # Chromium names the script, not the socket, as the line's resource.
    watch.allow_matching(
        r"WebSocket connection to '[^']*/v1/events/ws[^']*' failed: "
        r"Error during WebSocket handshake: Unexpected response code: 403",
        "the dead session's event-stream reconnect",
        may_not_occur=True,
    )
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
    fill_login(page, ADMIN_USER, fabric.password(ADMIN_USER))
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live", timeout=30_000
    )
    _only_expected_noise(watch)


def test_long_outage_then_recovery(fabric: Fabric, new_page: NewPage) -> None:
    page, watch = _signed_in(fabric, new_page)
    fabric.kill_controller()
    status = page.get_by_test_id("stream-status")
    expect(status).to_have_attribute("data-status", "reconnecting", timeout=15_000)
    page.wait_for_timeout(60_000)  # backoff has reached its 15 s cap
    expect(status).to_have_attribute("data-status", "reconnecting")
    expect(page.get_by_text("Cannot reach the controller")).to_have_count(0)
    expect(page.get_by_test_id("current-user")).to_have_text(f"{ADMIN_USER}(admin)")
    # the page is still usable offline: navigation and theme work
    page.get_by_role("link", name="Jobs", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Jobs")).to_be_visible()
    fabric.restart_controller(graceful=False)
    expect(status).to_have_attribute("data-status", "live", timeout=60_000)
    expect(page.get_by_test_id("fabric-status")).to_contain_text("node")
    _only_expected_noise(watch)


def test_rapid_repeated_restarts(fabric: Fabric, new_page: NewPage) -> None:
    page, watch = _signed_in(fabric, new_page)
    status = page.get_by_test_id("stream-status")
    for _ in range(3):
        fabric.restart_controller(graceful=False)
        expect(status).to_have_attribute("data-status", "live", timeout=90_000)
        expect(page.get_by_test_id("current-user")).to_have_text(f"{ADMIN_USER}(admin)")
    assert page.url.endswith("/nodes")
    _only_expected_noise(watch)


def test_restart_during_login(fabric: Fabric, new_page: NewPage) -> None:
    page, watch = new_page()
    watch.allow_matching(
        NOISE.pattern, "the controller is down meanwhile", may_not_occur=True
    )
    page.goto(fabric.http_base + "/")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    fabric.kill_controller()
    fill_login(page, ADMIN_USER, fabric.password(ADMIN_USER))
    expect(page.get_by_role("alert")).to_contain_text("did not answer")
    fabric.restart_controller(graceful=False)
    page.get_by_label("Password").fill(fabric.password(ADMIN_USER))
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("current-user")).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live", timeout=30_000
    )
    _only_expected_noise(watch)
