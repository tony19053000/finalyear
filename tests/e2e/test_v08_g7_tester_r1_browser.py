"""V0.8 G7 tester r1 (e2e tier): attack the guard, the leak gate and D204 a in a
real Chromium.

Part A/B need no fabric: a routed https page (``https://leak.test/``) stands in
for the dashboard so the shared guard (``PageWatch``) and the leak gate
(``LeakWatch``) can be judged on pages that are dirty on purpose.
Part C drives the real Secrets page against a real controller (per-read token).

``xfail(strict=True)`` marks a DEFECT of the G7 infrastructure (T-n in the
progress file); it turns into a failure the day the defect is fixed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)

from mf_dashboard import PageWatch
from mf_leaks import LeakWatch

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Browser, BrowserType, Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

ORIGIN = "https://leak.test"
SECRET = "mf-tester-secret-" + "Qz8" * 6


@pytest.fixture
def bare_browser(
    browser_type: BrowserType, chromium_installed: None
) -> Iterator[Browser]:
    browser = browser_type.launch()
    try:
        yield browser
    finally:
        browser.close()


def _routed_page(browser: Browser) -> Page:
    context = browser.new_context(reduced_motion="reduce")
    context.route(
        f"{ORIGIN}/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<!doctype html><title>t</title><main><h1>x</h1></main>",
        ),
    )
    page = context.new_page()
    page.set_default_timeout(10_000)
    return page


# ----------------------------------------------------------------------
# A. the leak gate's detection, one planted leak per place
# ----------------------------------------------------------------------

PLANTS = {
    "localStorage": f"localStorage.setItem('k', '{SECRET}')",
    "sessionStorage": f"sessionStorage.setItem('k', '{SECRET}')",
    "indexedDB": (
        "new Promise((ok, no) => { const r = indexedDB.open('leakdb', 1);"
        " r.onupgradeneeded = () => r.result.createObjectStore('s');"
        f" r.onsuccess = () => {{ const t = r.result.transaction('s', 'readwrite');"
        f" t.objectStore('s').put({{deep: {{list: ['{SECRET}']}}}}, 1);"
        " t.oncomplete = () => { r.result.close(); ok(); }; };"
        " r.onerror = () => no(r.error); })"
    ),
    "cookie": f"document.cookie = 'plain={SECRET}; path=/'",
    "window.name": f"window.name = '{SECRET}'",
    "console.log": f"console.log('value {SECRET}')",
    "console.warn": f"console.warn('value {SECRET}')",
    "console.debug": f"console.debug('value {SECRET}')",
    "console.error": f"console.error('value {SECRET}')",
    "url query (fetch)": f"fetch('{ORIGIN}/v1/x?token={SECRET}').catch(() => 0)",
    "url path (fetch)": f"fetch('{ORIGIN}/v1/{SECRET}/x').catch(() => 0)",
    "url fragment (hash change)": f"location.hash = '{SECRET}'",
    "url (pushState)": f"history.pushState({{}}, '', '/p?c={SECRET}')",
    "websocket url": f"new WebSocket('wss://leak.test/ws?t={SECRET}')",
}


@pytest.mark.parametrize("place", sorted(PLANTS))
def test_the_leak_gate_finds_a_secret_planted_in(
    bare_browser: Browser, place: str
) -> None:
    page = _routed_page(bare_browser)
    watch = LeakWatch(known={SECRET})
    watch.attach(page, "plant")
    page.goto(ORIGIN + "/")
    page.evaluate(PLANTS[place])
    page.wait_for_timeout(300)  # let the request / navigation events arrive
    watch.snapshot(page, "plant")
    findings = watch.findings()
    assert findings, f"a secret in {place} was not noticed"


@pytest.mark.xfail(
    strict=True,
    reason="T-10: a finding is labelled 'storage of <page.url>' and the URL "
    "can itself hold the credential, so the report prints the value it says "
    "it never prints",
)
def test_a_finding_never_prints_the_value_even_when_the_page_url_holds_it(
    bare_browser: Browser,
) -> None:
    page = _routed_page(bare_browser)
    watch = LeakWatch(known={SECRET})
    page.goto(ORIGIN + "/")
    page.evaluate(PLANTS["url (pushState)"])
    watch.snapshot(page, "plant")
    findings = watch.findings()
    assert findings
    assert SECRET not in "".join(findings)


def test_a_clean_page_has_no_findings(bare_browser: Browser) -> None:
    page = _routed_page(bare_browser)
    watch = LeakWatch(known={SECRET})
    watch.attach(page, "clean")
    page.goto(ORIGIN + "/")
    page.evaluate("localStorage.setItem('theme', 'dark'); console.log('hello')")
    watch.snapshot(page, "clean")
    assert watch.findings() == []
    assert watch.snapshots == 1 and watch.pages == 1


def test_a_shape_without_a_registered_value_is_found_in_storage(
    bare_browser: Browser,
) -> None:
    page = _routed_page(bare_browser)
    watch = LeakWatch()
    page.goto(ORIGIN + "/")
    page.evaluate("localStorage.setItem('t', 'mfat_' + 'A'.repeat(30))")
    watch.snapshot(page, "shape")
    assert len(watch.findings()) == 1


def test_a_csrf_companion_cookie_is_exempt_but_other_cookies_are_not(
    bare_browser: Browser,
) -> None:
    page = _routed_page(bare_browser)
    watch = LeakWatch(known={SECRET})
    page.goto(ORIGIN + "/")
    page.evaluate(f"document.cookie = 'mf_csrf={SECRET}; path=/'")
    watch.snapshot(page, "csrf")
    assert watch.findings() == []  # readable by design (D170)
    page.evaluate(f"document.cookie = 'mf_csrf_x={SECRET}; path=/'")
    watch.snapshot(page, "csrf2")
    assert watch.findings(), "a cookie that only STARTS like mf_csrf must not be exempt"


# --- holes: places a credential can sit that the gate does not look at ---


def _hole(script: str, why: str | None) -> Any:
    marks = [pytest.mark.xfail(strict=True, reason=why)] if why is not None else []
    return pytest.param(script, marks=marks)


HOLES = {
    "control: stack string logged": _hole(
        f"console.log(new Error('{SECRET}').stack)", None
    ),
    "control: console.log(object)": _hole(f"console.log({{token: '{SECRET}'}})", None),
    "Cache Storage": _hole(
        f"caches.open('c').then(c => c.put('/x', new Response('{SECRET}')))",
        "T-7: the dump never reads Cache Storage",
    ),
    "history.state": _hole(
        f"history.replaceState({{secret: '{SECRET}'}}, '')",
        "T-7: the dump never reads history.state",
    ),
    "set then removed before the test ends": _hole(
        f"localStorage.setItem('k', '{SECRET}'); localStorage.removeItem('k')",
        "T-7: storage is read only once, at the end of the test, so a secret "
        "that was stored and cleaned up in between is not seen",
    ),
}


@pytest.mark.parametrize("script", list(HOLES.values()), ids=list(HOLES))
def test_leak_gate_blind_spots(bare_browser: Browser, script: str) -> None:
    page = _routed_page(bare_browser)
    watch = LeakWatch(known={SECRET})
    watch.attach(page, "hole")
    page.goto(ORIGIN + "/")
    page.evaluate(script)
    page.wait_for_timeout(200)
    watch.snapshot(page, "hole")
    assert watch.findings(), "the gate did not see the planted secret"


@pytest.mark.xfail(
    strict=True,
    reason="T-7: closed pages escape the storage snapshot (silently)",
)
def test_a_page_the_test_closed_before_teardown_is_still_snapshotted(
    bare_browser: Browser,
) -> None:
    """T-7: `snapshot()` returns at once for a closed page, and new_page only
    snapshots at teardown - a test that closes its page (or whose page was
    closed by the app) has its storage skipped silently, and the snapshot
    counter does not even tell."""
    page = _routed_page(bare_browser)
    watch = LeakWatch(known={SECRET})
    page.goto(ORIGIN + "/")
    page.evaluate(f"localStorage.setItem('k', '{SECRET}')")
    page.close()
    watch.snapshot(page, "closed")
    assert watch.snapshots == 1, "a closed page's storage was skipped silently"


# ----------------------------------------------------------------------
# B. the page guard
# ----------------------------------------------------------------------


def _guarded(browser: Browser) -> tuple[Page, PageWatch]:
    page = _routed_page(browser)
    watch = PageWatch.attach(page)
    page.goto(ORIGIN + "/")
    return page, watch


def _wait(page: Page, watch: PageWatch, field: str) -> None:
    for _ in range(40):
        if getattr(watch, field):
            return
        page.wait_for_timeout(50)


def test_guard_catches_an_unhandled_promise_rejection(bare_browser: Browser) -> None:
    page, watch = _guarded(bare_browser)
    page.evaluate(
        "setTimeout(() => { Promise.reject(new Error('nobody catches me')); })"
    )
    _wait(page, watch, "page_errors")
    problems = watch.final_problems()
    assert any("nobody catches me" in p for p in problems), problems


@pytest.mark.parametrize("reason", ["'a plain string'", "undefined", "null", "42"])
def test_guard_catches_a_rejection_with_a_non_error_value(
    bare_browser: Browser, reason: str
) -> None:
    page, watch = _guarded(bare_browser)
    page.evaluate(f"setTimeout(() => {{ Promise.reject({reason}); }})")
    _wait(page, watch, "page_errors")
    assert watch.final_problems(), f"Promise.reject({reason}) went unnoticed"


def test_guard_catches_an_error_thrown_in_a_timer_and_in_an_event_handler(
    bare_browser: Browser,
) -> None:
    page, watch = _guarded(bare_browser)
    page.evaluate("setTimeout(() => { throw new Error('timer boom'); })")
    page.evaluate(
        "document.body.addEventListener('click', () => { throw new Error('click boom'); });"
        " document.body.click()"
    )
    for _ in range(40):
        if len(watch.page_errors) >= 2:
            break
        page.wait_for_timeout(50)
    text = " ".join(watch.final_problems())
    assert "timer boom" in text and "click boom" in text


def test_guard_catches_console_error_from_a_same_origin_iframe_and_a_worker(
    bare_browser: Browser,
) -> None:
    page, watch = _guarded(bare_browser)
    page.evaluate(
        "const f = document.createElement('iframe'); f.src = '/frame';"
        " document.body.appendChild(f);"
        " new Promise((ok) => f.onload = () => {"
        " f.contentWindow.console.error('iframe boom'); ok(); })"
    )
    page.evaluate(
        "const w = new Worker(URL.createObjectURL(new Blob([\"console.error('worker boom')\"],"
        " {type: 'text/javascript'}))); undefined"
    )
    page.wait_for_timeout(500)
    text = " ".join(watch.final_problems())
    assert "iframe boom" in text
    assert "worker boom" in text, f"a worker's console.error is not guarded: {text!r}"


def test_guard_sees_a_console_error_logged_before_a_navigation_inside_the_app(
    bare_browser: Browser,
) -> None:
    """Errors survive in-app navigation and full reloads of the same page."""
    page, watch = _guarded(bare_browser)
    page.evaluate("console.error('before reload')")
    page.reload()
    page.evaluate("console.error('after reload')")
    text = " ".join(watch.final_problems())
    assert "before reload" in text and "after reload" in text


@pytest.mark.xfail(
    strict=True,
    reason="T-7b (minor): a popup's console errors are not guarded (no app popups today)",
)
def test_guard_blind_spot_popup_window(bare_browser: Browser) -> None:
    """T-7b (minor): a popup opened by the page has no PageWatch."""
    page, watch = _guarded(bare_browser)
    with page.expect_popup() as popup:
        page.evaluate(f"window.open('{ORIGIN}/pop')")
    popup.value.evaluate("console.error('popup boom')")
    page.wait_for_timeout(300)
    assert any("popup boom" in p for p in watch.final_problems())


def test_a_failing_fetch_is_a_console_error_the_guard_reports(
    bare_browser: Browser,
) -> None:
    page, watch = _guarded(bare_browser)
    page.context.route(f"{ORIGIN}/v1/**", lambda r: r.fulfill(status=500, body="no"))
    page.evaluate("fetch('/v1/anything')")
    _wait(page, watch, "console_errors")
    assert any("500" in p for p in watch.final_problems()), watch.final_problems()


# ----------------------------------------------------------------------
# C. D204 a - a slow file read never overwrites a newer choice (real controller)
# ----------------------------------------------------------------------

SLOW_READ = """
(() => {
  const real = File.prototype.arrayBuffer;
  File.prototype.arrayBuffer = function () {
    if (this.name.startsWith('slow')) {
      return new Promise((ok) => setTimeout(() => ok(real.call(this)), 1500));
    }
    if (this.name.startsWith('broken')) {
      return Promise.reject(new DOMException('read failed', 'NotReadableError'));
    }
    return real.call(this);
  };
})();
"""


def _open_secret_form(page: Page, fabric: Fabric, name: str) -> None:
    from playwright.sync_api import expect

    page.goto(fabric.http_base + "/settings/secrets")
    page.get_by_role("button", name="Set a secret").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Name", exact=True).fill(name)
    expect(dialog).to_be_visible()


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_slow_file_read_after_a_newer_file_and_after_typing_is_dropped(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    from mf_e2e import sign_in
    from playwright.sync_api import expect

    page, _watch = new_page(init=SLOW_READ)
    sign_in(page, fabric)
    slow = tmp_path / "slow.bin"
    fast = tmp_path / "fast.bin"
    slow.write_bytes(b"SLOW-SLOW-SLOW-SLOW")
    fast.write_bytes(b"FAST-FAST-FAST-FAST")

    # 1. slow file, then a fast one: the fast one wins and stays.
    _open_secret_form(page, fabric, "race_one")
    dialog = page.get_by_role("dialog")
    chooser = dialog.get_by_label("Or read the value from a file", exact=False)
    chooser.set_input_files(slow)
    chooser.set_input_files(fast)
    expect(dialog).to_contain_text("fast.bin")
    page.wait_for_timeout(2500)  # the slow read has finished by now
    expect(dialog).to_contain_text("fast.bin")
    expect(dialog).not_to_contain_text("slow.bin")
    with page.expect_request(
        lambda r: r.method == "PUT" and "/v1/secrets/RACE_ONE" in r.url
    ) as sent:
        dialog.get_by_role("button", name="Store secret").click()
    assert sent.value.post_data_buffer == b"FAST-FAST-FAST-FAST"
    expect(page.get_by_text("Secret RACE_ONE stored", exact=True)).to_be_visible()

    # 2. slow file, then typing: the typed value wins and stays.
    _open_secret_form(page, fabric, "race_two")
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Or read the value from a file", exact=False).set_input_files(
        slow
    )
    dialog.get_by_label("Value", exact=True).fill("typed-value-wins")
    page.wait_for_timeout(2500)
    expect(dialog.get_by_label("Value", exact=True)).to_have_value("typed-value-wins")
    expect(dialog).not_to_contain_text("slow.bin")
    with page.expect_request(
        lambda r: r.method == "PUT" and "/v1/secrets/RACE_TWO" in r.url
    ) as sent2:
        dialog.get_by_role("button", name="Store secret").click()
    assert sent2.value.post_data_buffer == b"typed-value-wins"

    # 3. slow file then SEND (typed value) before it finishes: the form stays empty.
    _open_secret_form(page, fabric, "race_three")
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Value", exact=True).fill("send-me-now")
    dialog.get_by_label("Or read the value from a file", exact=False).set_input_files(
        slow
    )
    # the file choice replaced nothing yet (slow); Store sends the typed value
    with page.expect_request(
        lambda r: r.method == "PUT" and "/v1/secrets/RACE_THREE" in r.url
    ) as sent3:
        dialog.get_by_role("button", name="Store secret").click()
    assert sent3.value.post_data_buffer == b"send-me-now"
    page.wait_for_timeout(
        2200
    )  # the slow read ends after the send: nothing refills a form


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_an_unreadable_file_is_reported_not_an_unhandled_rejection(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    from mf_e2e import sign_in

    page, watch = new_page(init=SLOW_READ)
    sign_in(page, fabric)
    broken = tmp_path / "broken.bin"
    broken.write_bytes(b"whatever-bytes")
    _open_secret_form(page, fabric, "broken_one")
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Or read the value from a file", exact=False).set_input_files(
        broken
    )
    page.wait_for_timeout(1000)
    assert watch.page_errors == [], watch.page_errors
    text = dialog.inner_text().lower()
    assert "broken.bin" in text and (
        "could not" in text or "cannot" in text or "unreadable" in text
    )
