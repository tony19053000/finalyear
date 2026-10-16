"""V0.8 G7 fix round 1 (T-7): a page closed before the test ends is read
through its context - and, like an open page, only what a script could read
counts: the HttpOnly session cookie is not a leak, a script-readable cookie
is. (Found by the first full e2e run of fix round 1: the closed-page path
first counted every cookie of the context, the HttpOnly ``mf_session`` too.)
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

ORIGIN = "https://closed.test"
SECRET = "mf-closed-secret-" + "Zq7" * 6


@pytest.fixture
def bare_browser(
    browser_type: BrowserType, chromium_installed: None
) -> Iterator[Browser]:
    browser = browser_type.launch()
    try:
        yield browser
    finally:
        browser.close()


def _closed_page(browser: Browser, name: str, value: str, *, http_only: bool) -> Page:
    context = browser.new_context()
    context.route(
        f"{ORIGIN}/**",
        lambda route: route.fulfill(
            status=200, content_type="text/html", body="<title>t</title><h1>x</h1>"
        ),
    )
    context.add_cookies(
        [{"url": ORIGIN + "/", "name": name, "value": value, "httpOnly": http_only}]
    )
    page = context.new_page()
    page.goto(ORIGIN + "/")
    page.evaluate("localStorage.setItem('theme', 'dark')")
    page.close()
    return page


def test_an_httponly_cookie_of_a_closed_page_is_not_a_leak(
    bare_browser: Browser,
) -> None:
    page = _closed_page(bare_browser, "mf_session", SECRET, http_only=True)
    watch = LeakWatch(known={SECRET})
    watch.snapshot(page, "closed")
    assert watch.snapshots == 1
    assert watch.findings() == []


def test_a_script_readable_cookie_of_a_closed_page_is_a_leak(
    bare_browser: Browser,
) -> None:
    page = _closed_page(bare_browser, "plain", SECRET, http_only=False)
    watch = LeakWatch(known={SECRET})
    watch.snapshot(page, "closed")
    assert watch.findings() == [
        "closed: storage of https://closed.test/ (closed page): holds a known credential"
    ]


def test_a_closed_page_of_a_closed_context_is_listed_unread(
    bare_browser: Browser,
) -> None:
    page = _closed_page(bare_browser, "plain", "x", http_only=False)
    page.context.close()
    watch = LeakWatch()
    watch.snapshot(page, "gone")
    assert watch.snapshots == 0
    assert len(watch.unread) == 1 and "gone" in watch.unread[0]
