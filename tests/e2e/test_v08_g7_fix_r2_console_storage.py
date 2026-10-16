"""V0.8 G7 fix round 2 (real Chromium, no controller): the leak watch reads
console arguments in full (T-7h) and sees web storage written by property
assignment (T-7c) - without breaking on hostile values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)

from mf_leaks import LeakWatch

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Browser, BrowserType, Page

pytestmark = pytest.mark.e2e

ORIGIN = "https://fixr2.test"
SECRET = "mf-fix-r2-secret-" + "Kp4" * 6


@pytest.fixture
def bare_browser(
    browser_type: BrowserType, chromium_installed: None
) -> Iterator[Browser]:
    browser = browser_type.launch()
    try:
        yield browser
    finally:
        browser.close()


def _watched(browser: Browser) -> tuple[LeakWatch, Page]:
    context = browser.new_context()
    context.route(
        f"{ORIGIN}/**",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body="<title>t</title><h1>x</h1>"
        ),
    )
    watch = LeakWatch(known={SECRET})
    page = context.new_page()
    watch.attach(page, "fix-r2")
    page.goto(ORIGIN + "/")
    return watch, page


def _findings(watch: LeakWatch, page: Page) -> list[str]:
    page.wait_for_timeout(250)  # CDP events and bindings are asynchronous
    found = watch.findings()
    assert all(SECRET not in line for line in found)
    return found


@pytest.mark.parametrize(
    "script",
    [
        # a cycle next to the secret: serialised, not a crash or a hang
        "(s) => { const o = {s}; o.self = o; console.log({wrap: o}); }",
        # nested 10 deep (below the depth limit)
        "(s) => { let o = s; for (let i = 0; i < 10; i++) o = {o}; console.log(o); }",
        # a throwing getter beside the secret does not hide it
        "(s) => console.log({get bad() { throw new Error('no'); }, deep: [[{s}]]})",
        "(s) => console.log(new Set([[s]]))",
        "(s) => console.log({bytes: new TextEncoder().encode(s)})",
        "(s) => console.warn('%o', {a: {b: {c: s}}})",
    ],
    ids=["cycle", "deep", "throwing_getter", "set", "bytes", "format_o"],
)
def test_a_secret_inside_a_logged_value_is_found(
    bare_browser: Browser, script: str
) -> None:
    watch, page = _watched(bare_browser)
    page.evaluate(script, SECRET)
    found = _findings(watch, page)
    assert any("argument" in line for line in found), found


def test_hostile_console_values_without_a_secret_are_clean(
    bare_browser: Browser,
) -> None:
    watch, page = _watched(bare_browser)
    page.evaluate(
        """() => {
          const o = {}; o.o = o;
          const p = new Proxy({}, {ownKeys() { throw new Error('x'); }});
          let deep = 'x'; for (let i = 0; i < 100; i++) deep = [deep];
          console.log(o, p, deep, document.body, Symbol('s'), 10n, () => 1);
          console.log(new Array(100000).fill('a'));
        }"""
    )
    assert _findings(watch, page) == []
    assert any(where.endswith("argument 1") for where, _ in watch.places)


@pytest.mark.parametrize(
    "script",
    [
        "(s) => { localStorage.k = s; delete localStorage.k; }",
        "(s) => { localStorage['k'] = 'x'; localStorage['k'] = s; localStorage.clear(); }",
        "(s) => { sessionStorage.k = s; delete sessionStorage.k; }",
        "(s) => { Object.assign(localStorage, {k: s}); localStorage.clear(); }",
    ],
    ids=["ls_property", "ls_update", "ss_property", "object_assign"],
)
def test_a_property_assigned_storage_write_is_seen(
    bare_browser: Browser, script: str
) -> None:
    watch, page = _watched(bare_browser)
    page.evaluate(script, SECRET)
    found = _findings(watch, page)
    assert any("Storage change" in line for line in found), found


def test_storage_events_follow_a_navigation(bare_browser: Browser) -> None:
    watch, page = _watched(bare_browser)
    page.goto(ORIGIN + "/next")
    page.evaluate("(s) => { sessionStorage.k = s; sessionStorage.clear(); }", SECRET)
    assert any("sessionStorage change" in line for line in _findings(watch, page))
    assert watch.unread == []
