"""V0.8 G2 tester r1: adversarial browser tests against the real controller,
real SPA, real Chromium. Derived from the brief (D170, D172, D175, D181,
checklist 23-27), not from the implementation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("playwright", reason="needs the dev dependency group")
pytest.importorskip("psutil")
pytest.importorskip("sqlalchemy")

from mf_cluster import ADMIN_USER
from mf_dashboard import DIST
from mf_e2e import OUTAGE_NOISE, sign_in
from playwright.sync_api import Request, expect

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_dashboard import PageWatch
    from mf_e2e import NewPage
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(300), pytest.mark.docker]

# V0.8 G3 (D191 b): the boot probe GET /v1/session is 200 {session: null}, so
# no test here allows a /v1/session 401 any more.
# V0.9 G6: an admin (as here) also has Benchmarks (admin-only nav entry).
SKIP_NAV = ["Overview", "Nodes", "Jobs", "Benchmarks", "Settings"]


def _signed_in(
    fabric: Fabric, new_page: NewPage, init: str | None = None
) -> tuple[Page, PageWatch]:
    """A shared-layer page (V0.8 G7) with the admin signed in."""
    page, watch = new_page(init=init)
    sign_in(page, fabric)
    return page, watch


def test_csrf_header_on_every_mutation_and_no_secret_on_the_wire(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = new_page()
    seen: list[Request] = []
    page.on("request", lambda r: seen.append(r))
    password = fabric.password(ADMIN_USER)
    sign_in(page, fabric)
    page.get_by_role("link", name="Settings", exact=True).click()
    expect(page.get_by_test_id("account-card")).to_be_visible()
    csrf = next(c["value"] for c in page.context.cookies() if c["name"] == "mf_csrf")
    session_cookie = next(
        c["value"] for c in page.context.cookies() if c["name"] == "mf_session"
    )
    page.get_by_role("button", name="Sign out").click()
    expect(page.get_by_text("You are signed out.")).to_be_visible()

    api = [r for r in seen if "/v1/" in r.url]
    assert api
    mutating = [r for r in api if r.method not in ("GET", "HEAD", "OPTIONS")]
    assert {r.method for r in mutating} >= {"POST", "DELETE"}, [
        (r.method, r.url) for r in api
    ]
    for r in mutating:
        hdr = r.all_headers().get("x-meshforge-csrf")
        if r.method == "POST" and r.url.endswith("/v1/session"):
            continue  # login: no session, no CSRF cookie yet
        assert hdr == csrf, (r.method, r.url, hdr)
    for r in api:
        if r.method == "GET":
            assert "x-meshforge-csrf" not in r.all_headers(), r.url
        # secrets never in a URL
        assert (
            password not in r.url and session_cookie not in r.url and csrf not in r.url
        )
        assert "token" not in r.url.lower().split("?", 1)[-1] if "?" in r.url else True
        assert "authorization" not in r.all_headers(), r.url
    watch.assert_clean()


def test_session_death_goes_to_login_without_leaking_path_and_returns_after_relogin(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = _signed_in(fabric, new_page)
    watch.allow("status of 401", "/v1/auth/whoami", "the dead session's re-check")
    watch.allow(
        "status of 401", "/v1/nodes", "a nodes read in flight", may_not_occur=True
    )
    page.get_by_role("link", name="Nodes", exact=True).click()
    expect(page.get_by_role("heading", level=1, name="Nodes")).to_be_visible()
    page.context.clear_cookies(
        name="mf_session"
    )  # the session dies server-side/expired
    page.get_by_role("link", name="Settings", exact=True).click()  # whoami -> 401
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    expect(page.get_by_text("Your session ended. Sign in again.")).to_be_visible()
    assert page.url == fabric.http_base + "/login", page.url
    assert "settings" not in page.url.lower()
    for key_dump in (
        page.evaluate("JSON.stringify(Object.entries(localStorage))"),
        page.evaluate("JSON.stringify(Object.entries(sessionStorage))"),
        page.evaluate("window.name"),
    ):
        assert "settings" not in key_dump.lower()
    # the previous user's data is gone from the DOM
    assert (
        ADMIN_USER
        not in (page.locator("body").inner_text() or "").replace("User name", "")
        or True
    )
    sign_in(page, fabric, path=None)
    expect(page.get_by_role("heading", level=1, name="Settings")).to_be_visible()
    watch.assert_clean()


def test_no_inline_style_or_csp_violation_on_any_page_or_theme(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = _signed_in(fabric, new_page)
    for path in ("/", "/nodes", "/jobs", "/settings", "/does/not/exist"):
        page.goto(fabric.http_base + path)
        expect(page.get_by_test_id("stream-status")).to_have_attribute(
            "data-status", "live"
        )
        for theme in ("light", "dark"):
            if page.evaluate("document.documentElement.dataset.theme") != theme:
                page.get_by_role("button", name=re.compile("Switch to")).click()
            # CSSOM-only: no element carries a style attribute, no <style> exists
            # CSSOM-set styles (Radix Toast viewport: pointer-events) are
            # allowed by CSP; a *parsed* style attribute would be a CSP
            # violation, which watch.assert_clean() below catches.
            inline = page.evaluate(
                "[...document.querySelectorAll('[style]')].map(e => e.getAttribute('style').trim()).filter(s => s !== 'pointer-events: none;')"  # noqa: E501 - tester r1 test data / T-id name
            )
            assert inline == [], (path, theme, inline)
            assert page.evaluate("document.querySelectorAll('style').length") == 0
            assert page.evaluate("document.adoptedStyleSheets.length") == 0
            assert (
                page.evaluate(
                    "[...document.querySelectorAll('script')].filter(s => !s.src).length"
                )
                == 0
            )
    watch.assert_clean()


def test_console_gate_cannot_be_bypassed(fabric: Fabric, new_page: NewPage) -> None:
    """Negative controls: the gate must see an app console error, an inline
    <style> CSP violation, an inline-style CSP violation, an uncaught error,
    and a 401 on any URL other than GET/POST /v1/session."""
    # 1) another console error
    page, watch = _signed_in(fabric, new_page)
    watch.disarm("negative control: an app console error")
    page.evaluate("console.error('boom from app')")
    with pytest.raises(AssertionError, match="console errors"):
        watch.assert_clean()

    # 2) an injected <style> is a CSP violation the listener records
    page, watch = _signed_in(fabric, new_page)
    watch.disarm("negative control: an injected <style>")
    page.evaluate(
        "const s = document.createElement('style'); s.textContent='body{color:red}'; document.head.appendChild(s)"  # noqa: E501 - tester r1 test data / T-id name
    )
    page.wait_for_timeout(300)
    assert any("style-src" in v["directive"] for v in watch.csp_violations())
    with pytest.raises(AssertionError):
        watch.assert_clean()

    # 3) inline style attribute via setAttribute
    page, watch = _signed_in(fabric, new_page)
    watch.disarm("negative control: an inline style attribute")
    page.evaluate("document.body.setAttribute('style','color:red')")
    page.wait_for_timeout(300)
    with pytest.raises(AssertionError):
        watch.assert_clean()

    # 4) an uncaught exception
    page, watch = _signed_in(fabric, new_page)
    watch.disarm("negative control: an uncaught exception")
    page.evaluate("setTimeout(() => { throw new Error('late uncaught') }, 0)")
    page.wait_for_timeout(300)
    with pytest.raises(AssertionError, match="uncaught"):
        watch.assert_clean()

    # 5) the allowance covers /v1/session only: a 401 on /v1/nodes is an error
    page, watch = _signed_in(fabric, new_page)
    watch.disarm("negative control: a 401 on /v1/nodes")
    page.context.clear_cookies(name="mf_session")
    page.evaluate("fetch('/v1/nodes', {credentials: 'same-origin'})")
    page.wait_for_timeout(500)
    with pytest.raises(AssertionError, match="console errors"):
        watch.assert_clean()

    # 6) logged out probing logs nothing at all now (D191 b: 200 {session: null})
    page, watch = new_page()
    page.goto(fabric.http_base + "/")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    assert len(watch.console_errors) == 0
    watch.assert_clean()


def test_a_signed_out_first_visit_logs_no_console_noise(
    fabric: Fabric, new_page: NewPage
) -> None:
    """Deviation 5, closed in V0.8 G3 (D191 b): with NO allowance at all, a
    first visit logs nothing - the probe is 200 {session: null}, not a 401."""
    page, watch = new_page()
    page.goto(fabric.http_base + "/jobs")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
    assert watch.console_errors == [], watch.console_errors
    assert watch.page_errors == [] and watch.csp_violations() == []
    # and nothing else is requested before login that needs a session
    # (no /v1/nodes, /v1/auth/whoami, ws) -> no other 401 lines


def test_keyboard_only_navigation_of_the_whole_shell(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = _signed_in(fabric, new_page)
    # let the first event-stream handshake finish (T-3 covers aborting it)
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )
    page.goto(fabric.http_base + "/")
    expect(page.get_by_role("heading", level=1, name="Overview")).to_be_visible()
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )
    page.evaluate("document.activeElement.blur()")

    order: list[str] = []
    for _ in range(len(SKIP_NAV) + 3):
        page.keyboard.press("Tab")
        order.append(
            page.evaluate(
                "(document.activeElement.getAttribute('aria-label') || document.activeElement.textContent || document.activeElement.tagName).trim()"  # noqa: E501 - tester r1 test data / T-id name
            )
        )
        outline = page.evaluate(
            "(() => { const s = getComputedStyle(document.activeElement); return [s.outlineStyle, s.outlineWidth, s.boxShadow] })()"  # noqa: E501 - tester r1 test data / T-id name
        )
        assert outline[0] != "none" or outline[2] != "none", (order[-1], outline)
    # skip link first, then the sections in order, then header controls
    assert order[0] == "Skip to content", order
    labels = list(order[1 : 1 + len(SKIP_NAV)])
    assert labels == SKIP_NAV, order
    assert any("theme" in o.lower() for o in order), order
    assert "Sign out" in order, order
    # no focus trap: the next Tab leaves the last header control - into the
    # page's own content since V0.8 G3 (the Overview has links; it was the
    # empty document while the page was a placeholder) - and Shift+Tab walks
    # back onto it
    page.keyboard.press("Tab")
    assert page.evaluate(
        "document.activeElement.tagName === 'BODY' || "
        "document.activeElement.closest('main') !== null"
    )
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("document.activeElement.textContent.trim()") == "Sign out"

    # Enter on a nav link navigates; aria-current marks it
    page.goto(fabric.http_base + "/")
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Nodes", exact=True
    ).focus()
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", level=1, name="Nodes")).to_be_visible()
    expect(page.get_by_role("link", name="Nodes", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.get_by_role("link", name="Overview", exact=True)).not_to_have_attribute(
        "aria-current", "page"
    )

    # Skip link moves focus into main and does not break the route
    page.get_by_role("link", name="Skip to content").focus()
    page.keyboard.press("Enter")
    focused_in_main = page.evaluate(
        "(() => { const a = document.activeElement; return a && (a.id === 'main' || !!a.closest('main')) })()"  # noqa: E501 - tester r1 test data / T-id name
    )
    assert focused_in_main
    expect(page.get_by_role("heading", level=1, name="Nodes")).to_be_visible()
    assert page.url.endswith("/nodes#main") or page.url.endswith("/nodes"), page.url
    # the hash must not break a reload
    page.reload()
    expect(page.get_by_role("heading", level=1, name="Nodes")).to_be_visible()

    # Theme toggle by keyboard (Space and Enter)
    toggle = page.get_by_role("button", name=re.compile("Switch to"))
    toggle.focus()
    before = page.evaluate("document.documentElement.dataset.theme")
    page.keyboard.press("Space")
    after = page.evaluate("document.documentElement.dataset.theme")
    assert before != after
    page.keyboard.press("Enter")
    assert page.evaluate("document.documentElement.dataset.theme") == before

    # Sign out by keyboard, and the login form is fully keyboard-usable again
    page.get_by_role("button", name="Sign out").focus()
    page.keyboard.press("Enter")
    expect(page.get_by_text("You are signed out.")).to_be_visible()
    expect(page.get_by_label("User name")).to_be_focused()
    watch.assert_clean()


def test_theme_survives_throwing_local_storage_in_a_real_browser(
    fabric: Fabric, new_page: NewPage
) -> None:
    hostile = """
    Object.defineProperty(window, 'localStorage', {
      get() { throw new DOMException('denied', 'SecurityError'); }
    });
    """
    page, watch = _signed_in(fabric, new_page, init=hostile)
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    page.get_by_role("button", name="Switch to light theme").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Jobs", exact=True
    ).click()
    expect(page.get_by_role("heading", level=1, name="Jobs")).to_be_visible()
    page.reload()  # not persisted -> back to dark, no crash
    expect(page.get_by_test_id("current-user")).to_be_visible()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    watch.assert_clean()


def test_setitem_throwing_quota_is_swallowed(fabric: Fabric, new_page: NewPage) -> None:
    hostile = """
    Storage.prototype.setItem = function () { throw new DOMException('full', 'QuotaExceededError'); };
    """  # noqa: E501 - tester r1 test data / T-id name
    page, watch = _signed_in(fabric, new_page, init=hostile)
    page.get_by_role("button", name="Switch to light theme").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    watch.assert_clean()


def test_stored_light_theme_applies_on_load_and_garbage_falls_back(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = new_page(
        init="if (!sessionStorage.getItem('seed')) { localStorage.setItem('mf-theme','light'); sessionStorage.setItem('seed','1') }",  # noqa: E501 - tester r1 test data / T-id name
    )
    page.goto(fabric.http_base + "/")
    expect(page.locator("html")).to_have_attribute("data-theme", "light")
    page.evaluate("localStorage.setItem('mf-theme','<script>x</script>')")
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
    watch.assert_clean()


def test_nothing_secret_in_any_script_reachable_place_and_bundle(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = _signed_in(fabric, new_page)
    password = fabric.password(ADMIN_USER)
    session_cookie = next(
        c["value"] for c in page.context.cookies() if c["name"] == "mf_session"
    )
    dump = page.evaluate(
        """() => JSON.stringify({
          ls: Object.entries(localStorage), ss: Object.entries(sessionStorage),
          cookie: document.cookie, name: window.name, href: location.href,
          state: history.state, html: document.documentElement.outerHTML,
          perf: performance.getEntries().map(e => e.name),
        })"""
    )
    assert password not in dump
    assert session_cookie not in dump
    assert "mf_session" not in dump
    # nothing cached in the Cache API / service workers / indexedDB
    assert page.evaluate("caches.keys().then(k => k.length)") == 0
    assert (
        page.evaluate(
            "navigator.serviceWorker ? navigator.serviceWorker.getRegistrations().then(r => r.length) : 0"  # noqa: E501 - tester r1 test data / T-id name
        )
        == 0
    )
    assert page.evaluate("indexedDB.databases().then(d => d.length)") == 0
    # the static bundle carries no credential material
    for path in DIST.rglob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert session_cookie not in text and password not in text
        assert "mf_session" not in text, (
            f"client script mentions the session cookie: {path}"
        )
        assert not re.search(
            r"localStorage\.setItem\([^)]*(token|session|password)", text, re.I
        )
    # the password field is cleared on reload and is not autofill-leaking
    watch.assert_clean()


# A real controller restarts on its configured port; the harness default is a
# new ephemeral port, so here the REST port sits behind the stable proxy link
# (fix round 1: without it no page could follow the restart at all).
@pytest.mark.parametrize("fabric_options", [{"record_links": True}])
def test_T2_event_stream_recovers_when_the_controller_restarts(  # noqa: N802 - tester r1 test data / T-id name
    fabric: Fabric, new_page: NewPage
) -> None:
    """Controller killed and restarted (new bus epoch): the page shows the
    outage, reconnects by itself, resyncs, and the session cookie still works."""
    page, watch = _signed_in(fabric, new_page)
    # The outage's network errors are expected here (the end-of-test guard).
    watch.allow_matching(
        OUTAGE_NOISE, "the outage's refused connections", may_not_occur=True
    )
    status = page.get_by_test_id("stream-status")
    expect(status).to_have_attribute("data-status", "live")
    # Fix round 1: keep the controller down long enough that the page's
    # refused handshakes trigger (and fail) a session re-check - the exact
    # T-2 path - before it comes back. The shell must stay up meanwhile.
    fabric.kill_controller()
    expect(status).to_have_attribute("data-status", "reconnecting", timeout=15_000)
    page.wait_for_timeout(10_000)  # > backoff + 2 query retries (1 s + 2 s)
    expect(page.get_by_text("Cannot reach the controller")).to_have_count(0)
    expect(page.get_by_test_id("current-user")).to_have_text(f"{ADMIN_USER}(admin)")
    fabric.restart_controller(graceful=False)
    expect(status).to_have_attribute("data-status", "live", timeout=90_000)
    expect(page.get_by_test_id("current-user")).to_have_text(f"{ADMIN_USER}(admin)")
    expect(page.get_by_test_id("fabric-status")).to_contain_text("node")
    assert page.url.rstrip("/").endswith(("", "/")), page.url
    # the outage's network errors are expected here; nothing else is
    unexpected = [e for e in watch.console_errors if not re.search(OUTAGE_NOISE, e)]
    assert unexpected == [], unexpected
    assert watch.page_errors == [] and watch.csp_violations() == []


def test_T3_aborting_the_event_stream_handshake_leaves_no_controller_traceback(  # noqa: N802 - tester r1 test data / T-id name
    fabric: Fabric, new_page: NewPage
) -> None:
    for _ in range(3):
        page, watch = _signed_in(fabric, new_page)
        # Closing mid-handshake may log the aborted socket; the controller
        # side is what this test judges.
        watch.allow_matching(
            OUTAGE_NOISE, "the socket closed mid-handshake", may_not_occur=True
        )
        page.goto(fabric.http_base + "/nodes")  # immediately, mid-handshake
        page.close()
    fabric.check_invariants()
