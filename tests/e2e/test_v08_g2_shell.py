"""V0.8 G2 deliverable 6 (D179): the dashboard shell end to end - a real
controller + agent (cluster harness), the built SPA, real Chromium.

Every test fails on a console error, an uncaught page error or a CSP
violation (``PageWatch``); axe-core finds no WCAG A/AA violation on the login
page and the shell. The event stream reaching "Live" proves the same-origin
``wss:`` connection is allowed by ``connect-src 'self'`` (D181).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import ADMIN_USER, NORMAL_USER
from mf_dashboard import DIST, PageWatch, axe_violations
from playwright.sync_api import Browser, Page, expect

from meshforge.controller.web.spa import CSP

if TYPE_CHECKING:
    from mf_cluster import Fabric

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(240)]

#: What Chromium logs for a REFUSED login (`POST /v1/session` -> 401): the
#: browser reports every 4xx. Only the wrong-password test allows it. (The boot
#: probe `GET /v1/session` answers 200 `{session: null}` since V0.8 G3 - D191 b
#: - so a signed-out first visit logs nothing and needs no allowance.)
LOGIN_REFUSED_401 = ("status of 401", "/v1/session")


def _page(browser: Browser) -> tuple[Page, PageWatch]:
    # Reduced motion: the CSS then has no colour transitions, so axe measures
    # contrast at rest, not half-way through a theme switch.
    context = browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    page.set_default_timeout(15_000)
    return page, PageWatch.attach(page)


def _login(page: Page, name: str, password: str) -> None:
    page.get_by_label("User name").fill(name)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def _expected_online(fabric: Fabric) -> str:
    """The header text the API says it must show (checklist 26)."""
    listed = fabric.get("/v1/nodes")
    assert listed is not None
    nodes = listed["nodes"]
    online = sum(1 for n in nodes if n["state"] == "online")
    return f"{online} of {len(nodes)} node{'' if len(nodes) == 1 else 's'} online"


def _assert_no_axe_violations(page: Page, where: str) -> None:
    violations = axe_violations(page)
    assert violations == [], f"axe violations on {where}: {violations}"


@pytest.mark.docker
def test_login_shell_and_logout(fabric: Fabric, dashboard_browser: Browser) -> None:
    page, watch = _page(dashboard_browser)

    response = page.goto(fabric.http_base + "/jobs")
    assert response is not None and response.status == 200
    assert response.headers["content-security-policy"] == CSP

    # Not signed in: the login page, focus in the name field.
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    expect(page.get_by_label("User name")).to_be_focused()
    _assert_no_axe_violations(page, "the login page")

    _login(page, ADMIN_USER, fabric.password(ADMIN_USER))

    # Back where the user was going, inside the shell.
    expect(page.get_by_role("heading", level=1, name="Jobs")).to_be_visible()
    expect(page).to_have_url(re.compile(r"/jobs$"))
    expect(page.get_by_test_id("current-user")).to_have_text(f"{ADMIN_USER}(admin)")
    # The header number is the API's number.
    expect(page.get_by_test_id("fabric-status")).to_have_text(_expected_online(fabric))
    # The event stream is up: same-origin wss: passed the CSP.
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )

    # Navigation keeps the one stream.
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Nodes"
    ).click()
    expect(page.get_by_role("heading", level=1, name="Nodes")).to_be_visible()
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )
    _assert_no_axe_violations(page, "the shell")
    page.get_by_role("button", name="Switch to light theme").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    _assert_no_axe_violations(page, "the shell (light theme)")

    # Checklist 25: no token anywhere script can reach.
    cookies = {c["name"]: c for c in page.context.cookies()}
    assert cookies["mf_session"]["httpOnly"] is True
    assert cookies["mf_session"]["secure"] is True
    assert cookies["mf_session"]["sameSite"] == "Strict"
    assert "mf_session" not in page.evaluate("document.cookie")
    assert page.evaluate("Object.keys(localStorage)") in ([], ["mf-theme"])
    assert page.evaluate("sessionStorage.length") == 0
    assert "mfbs_" not in page.url

    # Settings shows the real whoami.
    page.get_by_role("link", name="Settings", exact=True).click()
    card = page.get_by_test_id("account-card")
    expect(card.locator("dd").nth(0)).to_have_text(ADMIN_USER)  # User
    expect(card.locator("dd").nth(1)).to_have_text("admin")  # Role
    expect(card.locator("dd").nth(2)).to_have_text(re.compile(r"^tok_"))  # Session id

    # Sign out: back to login, session gone server-side.
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_text("You are signed out.")).to_be_visible()
    assert "mf_session" not in {c["name"] for c in page.context.cookies()}
    page.reload()
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    watch.assert_clean()


@pytest.mark.docker
def test_wrong_password_is_refused_with_a_message(
    fabric: Fabric, dashboard_browser: Browser
) -> None:
    page, watch = _page(dashboard_browser)
    watch.allow(*LOGIN_REFUSED_401)  # the refused POST below
    page.goto(fabric.http_base + "/")
    _login(page, NORMAL_USER, "not-the-password")
    expect(page.get_by_role("alert")).to_have_text("Wrong user name or password.")
    expect(page.get_by_label("Password")).to_have_value("")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    assert "mf_session" not in {c["name"] for c in page.context.cookies()}
    watch.assert_clean()


@pytest.mark.docker
def test_keyboard_only_login_and_logout(
    fabric: Fabric, dashboard_browser: Browser
) -> None:
    page, watch = _page(dashboard_browser)
    page.goto(fabric.http_base + "/")
    expect(page.get_by_label("User name")).to_be_focused()
    page.keyboard.type(NORMAL_USER)
    page.keyboard.press("Tab")
    expect(page.get_by_label("Password")).to_be_focused()
    page.keyboard.type(fabric.password(NORMAL_USER))
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", level=1, name="Overview")).to_be_visible()
    expect(page.get_by_test_id("current-user")).to_have_text(f"{NORMAL_USER}(user)")

    # First Tab reaches the skip link; it is visible when focused.
    page.keyboard.press("Tab")
    skip = page.get_by_role("link", name="Skip to content")
    expect(skip).to_be_focused()
    expect(skip).to_be_visible()

    # Tab on to "Sign out" and press it - no mouse anywhere.
    sign_out = page.get_by_role("button", name="Sign out")
    for _ in range(20):
        if page.evaluate("document.activeElement?.textContent") == "Sign out":
            break
        page.keyboard.press("Tab")
    expect(sign_out).to_be_focused()
    outline = page.evaluate("getComputedStyle(document.activeElement).outlineStyle")
    assert outline != "none", "keyboard focus must be visible"
    page.keyboard.press("Enter")
    expect(page.get_by_text("You are signed out.")).to_be_visible()
    watch.assert_clean()


def test_built_bundle_has_no_inline_styles_or_scripts(dashboard_dist: object) -> None:
    """D181: CSP ``style-src 'self'`` / ``script-src 'self'`` - the build has no
    ``style="``, no ``<style>``, no inline ``<script>`` (mirrors
    ``frontend/scripts/check-dist.mjs``)."""
    files = [p for p in DIST.rglob("*") if p.suffix in {".html", ".js", ".css"}]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert 'style="' not in text, path
        if path.suffix == ".html":
            assert "<style" not in text, path
            for tag in re.findall(r"<script\b[^>]*>", text):
                assert "src=" in tag, f"inline script in {path}: {tag}"
            assert not re.search(r"(src|href)=\"(https?:)?//", text), path
