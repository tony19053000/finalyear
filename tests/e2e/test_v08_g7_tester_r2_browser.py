# ruff: noqa: E501
"""V0.8 G7 tester round 2 (real Chromium, no controller): attacks on the leak
watch's storage coverage (T-7 / T-7b) and the page guard's popup coverage.

Each test plants a known secret in one place the way a page script could and
asks whether ``LeakWatch.findings()`` names it, never printing the value.
``xfail(strict=True)`` pins a remaining hole (it flips to FAILED when fixed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)

from mf_leaks import LeakWatch

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Browser, BrowserContext, BrowserType, Page

pytestmark = pytest.mark.e2e

ORIGIN = "https://r2.test"
SECRET = 'r2-"secret"\\value-' + "Qx9" * 5


@pytest.fixture
def bare_browser(
    browser_type: BrowserType, chromium_installed: None
) -> Iterator[Browser]:
    browser = browser_type.launch()
    try:
        yield browser
    finally:
        browser.close()


def _context(browser: Browser) -> BrowserContext:
    context = browser.new_context()

    def page_route(route: Any) -> None:
        if route.request.url.endswith("/api.txt"):
            route.fulfill(status=200, content_type="text/plain", body="ok")
        else:
            route.fulfill(
                status=200,
                content_type="text/html",
                body="<title>t</title><h1>x</h1>",
            )

    context.route(f"{ORIGIN}/**", page_route)
    return context


def _watched(browser: Browser) -> tuple[LeakWatch, Page]:
    watch = LeakWatch(known={SECRET})
    page = _context(browser).new_page()
    watch.attach(page, "r2")
    page.goto(ORIGIN + "/")
    return watch, page


def _verdict(watch: LeakWatch, page: Page | None = None) -> list[str]:
    if page is not None:
        page.wait_for_timeout(250)  # binding callbacks are asynchronous
        watch.snapshot(page, "r2")
        watch.snapshot_followed()
    page_wait = page
    if page_wait is not None and not page_wait.is_closed():
        page_wait.wait_for_timeout(100)
    found = watch.findings()
    assert all(SECRET not in line for line in found)
    return found


# JS snippets: each receives the secret as `s` and stores it, then (mostly)
# removes it again before the test ends - the live write log must have seen it.
IDB_OPEN = """(name) => new Promise((ok, no) => {
  const r = indexedDB.open(name, 1);
  r.onupgradeneeded = () => r.result.createObjectStore("st", { autoIncrement: true });
  r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error);
})"""
CASES: dict[str, str] = {
    "ls_set_remove": "(s) => { localStorage.setItem('k', s); localStorage.removeItem('k'); }",
    "ls_set_clear": "(s) => { localStorage.setItem('k', s); localStorage.clear(); }",
    "ls_secret_as_key": "(s) => { localStorage.setItem(s, '1'); localStorage.clear(); }",
    "ss_set_remove": "(s) => { sessionStorage.setItem('k', s); sessionStorage.removeItem('k'); }",
    "ls_call_via_prototype": (
        "(s) => { Storage.prototype.setItem.call(localStorage, 'k', s); localStorage.clear(); }"
    ),
    "idb_put_then_delete": (
        "async (s) => { const db = await (" + IDB_OPEN + ")('r2a');"
        " const t = db.transaction('st', 'readwrite'); t.objectStore('st').put(s, 'k');"
        " await new Promise((ok) => (t.oncomplete = ok));"
        " const d = db.transaction('st', 'readwrite'); d.objectStore('st').clear();"
        " await new Promise((ok) => (d.oncomplete = ok)); db.close(); }"
    ),
    "idb_add_then_clear": (
        "async (s) => { const db = await (" + IDB_OPEN + ")('r2b');"
        " const t = db.transaction('st', 'readwrite'); t.objectStore('st').add({v: s});"
        " await new Promise((ok) => (t.oncomplete = ok));"
        " const d = db.transaction('st', 'readwrite'); d.objectStore('st').clear();"
        " await new Promise((ok) => (d.oncomplete = ok)); db.close(); }"
    ),
    "cookie_set_then_expire": (
        "(s) => { document.cookie = 'k=' + encodeURIComponent(s) + '; path=/';"
        " document.cookie = 'k=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT'; }"
    ),
    "cache_put_then_delete": (
        "async (s) => { const c = await caches.open('r2'); await c.put('/api.txt', new Response(s));"
        " await caches.delete('r2'); }"
    ),
    "cache_put_url": (
        "async (s) => { const c = await caches.open('r2');"
        " await c.put('/api.txt?x=' + encodeURIComponent(s), new Response('ok'));"
        " await caches.delete('r2'); }"
    ),
    "history_push_then_replace": (
        "(s) => { history.pushState({s}, ''); history.replaceState(null, ''); }"
    ),
    "history_url": (
        "(s) => { history.pushState(null, '', '/x?q=' + encodeURIComponent(s)); history.back(); }"
    ),
    "console_nested_object": "(s) => console.log({a: {b: {c: s}}})",
    "console_array_nested": "(s) => console.log([[['x', s]]])",
    "console_error_object": "(s) => console.error(new Error(s))",
    "console_map": "(s) => console.log(new Map([['k', s]]))",
    "console_format_string": "(s) => console.log('%s and %o', 'x', {s})",
    "console_table": "(s) => console.table([{s}])",
    "console_dir": "(s) => console.dir({s})",
    "console_warn_group": "(s) => { console.group('g'); console.warn(s); console.groupEnd(); }",
    "console_after_timeout": "(s) => { setTimeout(() => console.log(s), 50); }",
    "iframe_ls": (
        "async (s) => { const f = document.createElement('iframe'); f.src = '/frame.html';"
        " document.body.appendChild(f); await new Promise((ok) => (f.onload = ok));"
        " f.contentWindow.localStorage.setItem('k', s); f.contentWindow.localStorage.clear(); }"
    ),
    "popup_ss_closed": (
        "async (s) => { const w = window.open('/popup.html');"
        " await new Promise((ok) => setTimeout(ok, 400));"
        " w.sessionStorage.setItem('k', s); w.sessionStorage.removeItem('k'); w.close(); }"
    ),
    "popup_noopener_ls": (
        "async (s) => { window.open('/popup.html#' + encodeURIComponent(s), '_blank', 'noopener'); }"
    ),
    # --- holes found by tester r2 (strict xfail below) ---
    "ls_property_assign": "(s) => { localStorage.k = s; delete localStorage.k; }",
    "ls_bracket_assign": "(s) => { localStorage['k'] = s; localStorage.clear(); }",
    "ss_property_assign": "(s) => { sessionStorage.k = s; delete sessionStorage.k; }",
    "window_name_then_reset": "(s) => { window.name = s; window.name = ''; }",
    "cookiestore_set_delete": (
        "async (s) => { await cookieStore.set('k', s); await cookieStore.delete('k'); }"
    ),
    "idb_cursor_update": (
        "async (s) => { const db = await (" + IDB_OPEN + ")('r2c');"
        " const t = db.transaction('st', 'readwrite'); t.objectStore('st').put('seed', 'k');"
        " await new Promise((ok) => (t.oncomplete = ok));"
        " const u = db.transaction('st', 'readwrite');"
        " const req = u.objectStore('st').openCursor();"
        " req.onsuccess = () => { req.result.update(s); };"
        " await new Promise((ok) => (u.oncomplete = ok));"
        " const d = db.transaction('st', 'readwrite'); d.objectStore('st').clear();"
        " await new Promise((ok) => (d.oncomplete = ok)); db.close(); }"
    ),
    "worker_idb_put_then_delete": (
        "async (s) => { const code = `const open = () => new Promise((ok, no) => {"
        " const r = indexedDB.open('r2w', 1);"
        " r.onupgradeneeded = () => r.result.createObjectStore('st');"
        " r.onsuccess = () => ok(r.result); r.onerror = () => no(r.error); });"
        " onmessage = async (e) => { const db = await open();"
        " const t = db.transaction('st', 'readwrite'); t.objectStore('st').put(e.data, 'k');"
        " await new Promise((ok) => (t.oncomplete = ok));"
        " const d = db.transaction('st', 'readwrite'); d.objectStore('st').clear();"
        " await new Promise((ok) => (d.oncomplete = ok)); postMessage('done'); };`;"
        " const w = new Worker(URL.createObjectURL(new Blob([code])));"
        " await new Promise((ok) => { w.onmessage = ok; w.postMessage(s); }); w.terminate(); }"
    ),
    "worker_console": (
        "async (s) => { const code = `console.log(${JSON.stringify(s)}); postMessage(1)`;"
        " const w = new Worker(URL.createObjectURL(new Blob([code])));"
        " await new Promise((ok) => (w.onmessage = ok)); w.terminate(); }"
    ),
}
HOLES: dict[str, str] = {
    "window_name_then_reset": "T-7d (minor): window.name is read once at the end, never hooked",
    "cookiestore_set_delete": "T-7e (minor): the Cookie Store API (cookieStore.set) is not hooked",
    "idb_cursor_update": "T-7f (minor): IDBCursor.update / IDBObjectStore.openCursor writes are not hooked",
    "worker_idb_put_then_delete": "T-7g (minor): writes made inside a Web Worker are not hooked "
    "(no init script runs in workers)",
}


def _params() -> list[Any]:
    out = []
    for name in CASES:
        marks = (
            [pytest.mark.xfail(strict=True, reason=HOLES[name])]
            if name in HOLES
            else []
        )
        out.append(pytest.param(name, id=name, marks=marks))
    return out


@pytest.mark.parametrize("name", _params())
def test_t7_a_secret_stored_and_removed_is_still_found(
    bare_browser: Browser, name: str
) -> None:
    watch, page = _watched(bare_browser)
    page.evaluate(CASES[name], SECRET)
    found = _verdict(watch, page)
    assert found, f"{name}: the secret was written and nothing was reported"


def test_t7_a_clean_page_has_no_findings(bare_browser: Browser) -> None:
    watch, page = _watched(bare_browser)
    page.evaluate(
        "() => { localStorage.setItem('theme', 'dark'); console.log('hello');"
        " history.pushState({n: 1}, ''); document.cookie = 'a=b'; }"
    )
    assert _verdict(watch, page) == []


def test_t7_other_tab_of_the_context_is_followed_and_closed_before_the_end(
    bare_browser: Browser,
) -> None:
    watch, page = _watched(bare_browser)
    other = page.context.new_page()
    other.goto(ORIGIN + "/other")
    other.evaluate(
        "(s) => { sessionStorage.setItem('k', s); sessionStorage.clear(); }", SECRET
    )
    other.wait_for_timeout(250)
    other.close()
    assert _verdict(watch, page)


def test_t7_a_secret_left_in_a_closed_second_tab_localstorage_is_found(
    bare_browser: Browser,
) -> None:
    watch, page = _watched(bare_browser)
    other = page.context.new_page()
    other.goto(ORIGIN + "/other")
    other.evaluate("(s) => localStorage.setItem('keep', s)", SECRET)
    other.close()
    assert _verdict(watch, page)


def test_t7_unread_pages_are_listed_when_their_context_is_gone(
    bare_browser: Browser,
) -> None:
    watch, page = _watched(bare_browser)
    page.evaluate("(s) => localStorage.setItem('k', s)", SECRET)
    page.wait_for_timeout(250)
    ctx = page.context
    ctx.close()
    watch.snapshot(page, "r2")
    assert watch.unread, "a page whose context is gone must be listed as unread"
    assert watch.findings(), "its write was logged live and is still judged"


def test_t7_shapes_need_no_registered_value(bare_browser: Browser) -> None:
    watch = LeakWatch()
    page = _context(bare_browser).new_page()
    watch.attach(page, "r2")
    page.goto(ORIGIN + "/")
    page.evaluate(
        "() => { sessionStorage.setItem('k', 'mfbs_' + 'a'.repeat(30)); sessionStorage.clear(); }"
    )
    found = _verdict(watch, page)
    assert any("mfbs" in line for line in found)
    assert all("a" * 30 not in line for line in found)


def test_t7_two_watched_pages_in_one_context_do_not_double_register_the_hook(
    bare_browser: Browser,
) -> None:
    watch = LeakWatch(known={SECRET})
    context = _context(bare_browser)
    first = context.new_page()
    watch.attach(first, "r2")
    second = context.new_page()
    watch.attach(second, "r2")  # must not raise "binding already registered"
    second.goto(ORIGIN + "/")
    second.evaluate(
        "(s) => { localStorage.setItem('k', s); localStorage.clear(); }", SECRET
    )
    assert _verdict(watch, second)


def test_t7_the_write_hook_does_not_change_page_behaviour(
    bare_browser: Browser,
) -> None:
    watch, page = _watched(bare_browser)
    result = page.evaluate(
        """() => {
          localStorage.setItem('a', '1');
          const got = localStorage.getItem('a');
          let thrown = null;
          try { history.pushState({}, '', 'https://elsewhere.test/'); } catch (e) { thrown = e.name; }
          let ret = history.pushState({}, '', '/ok');
          document.cookie = 'z=1';
          return [got, thrown, ret === undefined, document.cookie.includes('z=1'),
                  Storage.prototype.setItem.name];
        }"""
    )
    assert result[0] == "1" and result[1] == "SecurityError" and result[2] is True
    assert result[3] is True
    assert _verdict(watch, page) == []


# ------------------------------------------------------------ T-2d: dead allowance


def _ws_failure_line(browser: Browser) -> tuple[str, str]:
    from mf_dashboard import PageWatch

    context = _context(browser)
    page = context.new_page()
    watch = PageWatch.attach(page)
    page.goto(ORIGIN + "/")
    page.evaluate("() => { new WebSocket('wss://127.0.0.1:1/v1/jobs/j/logs/ws'); }")
    page.wait_for_timeout(800)
    assert watch.console_errors, "Chromium logged nothing for the failed socket"
    text, _, url = watch.console_errors[0].rpartition(" @ ")
    return text, url


def test_t2d_chromium_names_no_resource_for_a_failed_websocket(
    bare_browser: Browser,
) -> None:
    """Fact the allowances must respect (also noted in g2_tester_r2): the console
    line of a failed WebSocket carries the socket URL only in its TEXT; its
    location URL is empty or the script's."""
    text, url = _ws_failure_line(bare_browser)
    assert text.startswith("WebSocket connection to")
    assert not url.endswith("logs/ws")


def test_t2d_the_g4_log_socket_allowances_can_apply(bare_browser: Browser) -> None:
    from mf_dashboard import PageWatch

    text, url = _ws_failure_line(bare_browser)

    class _Msg:
        type = "error"

        def __init__(self) -> None:
            self.text = text
            self.location = {"url": url}

    watch = PageWatch(page=None)  # type: ignore[arg-type]
    watch.allow("WebSocket", "logs/ws", "the dropped log socket", may_not_occur=True)
    watch._on_console(_Msg())  # type: ignore[arg-type]
    assert watch.allowed[0].used == 1


# ------------------------------------------------------------ T-7b: popups in the guard


@pytest.mark.parametrize("noopener", [False, True], ids=["opener", "noopener"])
def test_t7b_a_popup_console_error_and_uncaught_error_reach_the_opener_watch(
    bare_browser: Browser, noopener: bool
) -> None:
    from mf_dashboard import PageWatch

    page = _context(bare_browser).new_page()
    watch = PageWatch.attach(page)
    page.goto(ORIGIN + "/")
    features = "'_blank', 'noopener'" if noopener else "'_blank'"
    page.evaluate(f"() => {{ window.open('/popup.html', {features}); }}")
    page.wait_for_timeout(500)
    popup = page.context.pages[-1]
    assert popup is not page
    popup.evaluate(
        "() => { console.error('popup boom'); setTimeout(() => { throw new Error('popup uncaught'); }, 0); }"
    )
    page.wait_for_timeout(300)
    assert any("popup boom" in e for e in watch.console_errors)
    if not noopener:
        assert any("popup uncaught" in e for e in watch.page_errors)
