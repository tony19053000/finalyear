"""The jobs list as a user feels it, in real Chromium: first render, in-app
navigation, loading rows by scrolling, a scroll through the virtualised rows,
a filter change, opening a job - and :func:`run`, one measurement pass."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from .events import measure_events
from .report import summary
from .scenarios import Api

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page


JOB_ROW: Final = 'table tbody tr a[href^="/jobs/job_"]'
#: Recorded from the first moment of every document: long tasks, and the
#: first job row's paint time (performance.now() = ms since navigation).
PAGE_PROBES: Final = """
window.__mfLong = [];
window.__mfFirstRow = null;
try {
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) window.__mfLong.push({at: e.startTime, ms: e.duration});
  }).observe({type: "longtask", buffered: true});
} catch (e) {}
new MutationObserver((_, obs) => {
  if (document.querySelector('table tbody tr a[href^="/jobs/job_"]')) {
    window.__mfFirstRow = performance.now();
    obs.disconnect();
  }
}).observe(document, {subtree: true, childList: true});
"""
#: Resolves with ms from now until ``ready()`` holds (checked every frame).
UNTIL: Final = """
async ({start, ready}) => {
  const ok = new Function(ready);
  const t0 = performance.now();
  new Function(start)();
  while (!ok()) {
    if (performance.now() - t0 > 30000) throw new Error("timed out: " + ready);
    await new Promise((r) => requestAnimationFrame(r));
  }
  return performance.now() - t0;
}
"""
ROWS: Final = (
    "const rows = [...document.querySelectorAll('table tbody tr')]"
    ".filter((r) => r.querySelector('a[href^=\"/jobs/job_\"]'));"
)


def sign_in(page: Page, base: str, user: str, password: str) -> None:
    from playwright.sync_api import expect

    page.goto(base + "/")
    page.get_by_label("User name").fill(user)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_test_id("current-user")).to_be_visible()
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )


def _loaded(page: Page) -> int:
    text = page.get_by_role("status").filter(has_text="loaded").inner_text()
    return int(text.split()[0])


def measure_list(
    page: Page, base: str, *, repeats: int = 5, rows_to_load: int = 1000
) -> dict[str, Any]:
    """The jobs list under the history the fabric holds now."""
    from playwright.sync_api import expect

    out: dict[str, Any] = {}
    # 1. cold first render: full page load -> first job row in the DOM.
    cold: list[float] = []
    for _ in range(repeats):
        page.goto(base + "/jobs")
        page.wait_for_function("() => window.__mfFirstRow !== null", timeout=30_000)
        cold.append(float(page.evaluate("window.__mfFirstRow")))
    out["list_first_render"] = summary(cold)

    # 2. in-app navigation Overview -> Jobs (the SPA's own route change).
    nav: list[float] = []
    for _ in range(repeats):
        page.goto(base + "/")
        expect(page.get_by_role("heading", level=1, name="Overview")).to_be_visible()
        nav.append(
            float(
                page.evaluate(
                    UNTIL,
                    {
                        "start": "document.querySelector("
                        '\'nav[aria-label="Main"] a[href="/jobs"]\').click();',
                        "ready": f"return !!document.querySelector('{JOB_ROW}');",
                    },
                )
            )
        )
    out["list_in_app_nav"] = summary(nav)

    # 3. load rows by scrolling (each page of 50 fetched on reaching the end).
    expect(page.locator(JOB_ROW).first).to_be_visible()
    scroller = "document.querySelector('[data-windowed]')"
    pages: list[float] = []
    while _loaded(page) < rows_to_load:
        before = _loaded(page)
        if (
            "more on the server"
            not in page.get_by_role("status").filter(has_text="loaded").inner_text()
        ):
            break
        pages.append(
            float(
                page.evaluate(
                    UNTIL,
                    {
                        "start": f"const s = {scroller}; s.scrollTop = s.scrollHeight;",
                        "ready": "const t = [...document.querySelectorAll("
                        "'[role=status]')].map((e) => e.textContent).join(' ');"
                        f"const m = t.match(/(\\d+) jobs? loaded/); "
                        f"return !!m && Number(m[1]) > {before};",
                    },
                )
            )
        )
    loaded = _loaded(page)
    out["rows_loaded"] = loaded
    out["load_more_by_scroll"] = summary(pages)

    # 4. scroll the whole loaded list top -> bottom -> top, a frame at a time.
    page.evaluate("window.__mfLong = []")
    scroll = page.evaluate(
        """async () => {
          const s = document.querySelector('[data-windowed]');
          const gaps = []; let last = performance.now();
          const frame = () => new Promise((r) => requestAnimationFrame(() => {
            const now = performance.now(); gaps.push(now - last); last = now; r();
          }));
          s.scrollTop = 0; await frame();
          const step = Math.max(1, Math.floor(s.clientHeight * 0.9));
          let maxDom = 0;
          // stop short of the end: reaching it would fetch another page
          const end = s.scrollHeight - 3 * s.clientHeight;
          while (s.scrollTop < end) {
            s.scrollTop = Math.min(end, s.scrollTop + step); await frame();
            maxDom = Math.max(maxDom, s.querySelectorAll('tbody tr').length);
          }
          while (s.scrollTop > 0) { s.scrollTop -= step; await frame(); }
          return {gaps, maxDom, windowed: s.dataset.windowed};
        }"""
    )
    long_tasks = [float(t["ms"]) for t in page.evaluate("window.__mfLong")]
    out["scroll"] = {
        "frames": len(scroll["gaps"]),
        "frame_gap": summary(scroll["gaps"]),
        "long_tasks": len(long_tasks),
        "long_task_max": round(max(long_tasks, default=0.0), 1),
        "max_dom_rows": int(scroll["maxDom"]),
        "windowed": scroll["windowed"] == "true",
    }

    # 5. a filter change (server query) -> the table shows only that state.
    #    Both states must exist (the seed adds MIXED_CANCELLED cancelled jobs):
    #    an empty answer would time an empty table, not a filtered list.
    page.goto(base + "/jobs")
    expect(page.locator(JOB_ROW).first).to_be_visible()
    filters: list[float] = []
    for want in ("succeeded", "cancelled", "succeeded", "cancelled", "succeeded"):
        page.evaluate(
            """(want) => {
              window.__mfFilter = null;
              const label = [...document.querySelectorAll('label')]
                .find((l) => l.textContent.trim().toLowerCase() === 'state');
              const sel = document.getElementById(label.htmlFor);
              sel.addEventListener('change', () => {
                const t0 = performance.now();
                const tick = () => {
                  const rows = [...document.querySelectorAll('table tbody tr')]
                    .filter((r) => r.querySelector('a[href^="/jobs/job_"]'));
                  const done = rows.length > 0 &&
                    rows.every((r) => r.cells[1].textContent.trim() === want) &&
                    !document.body.textContent.includes('Loading jobs');
                  if (done) window.__mfFilter = performance.now() - t0;
                  else requestAnimationFrame(tick);
                };
                requestAnimationFrame(tick);
              }, {once: true, capture: true});
            }""",
            want,
        )
        page.get_by_label("State").select_option(want)
        page.wait_for_function("() => window.__mfFilter !== null", timeout=30_000)
        filters.append(float(page.evaluate("window.__mfFilter")))
    out["filter_change"] = summary(filters)

    # 6. open a job from the list -> its page shows the job.
    details: list[float] = []
    page.goto(base + "/jobs")
    for i in range(repeats):
        expect(page.locator(JOB_ROW).nth(i)).to_be_visible()
        details.append(
            float(
                page.evaluate(
                    UNTIL,
                    {
                        "start": f"{ROWS} window.__mfName = "
                        f"rows[{i}].querySelector('a').textContent; "
                        f"rows[{i}].querySelector('a').click();",
                        "ready": "const h = document.querySelector('h1');"
                        "return !!h && h.textContent === window.__mfName && "
                        "!!document.querySelector('[role=tablist]');",
                    },
                )
            )
        )
        page.go_back()
    out["detail_open"] = summary(details)
    return out


def new_page(browser: Browser) -> Page:
    page = browser.new_context(reduced_motion="reduce").new_page()
    page.set_default_timeout(30_000)
    page.add_init_script(PAGE_PROBES)
    return page


def run(
    browser: Browser,
    api: Api,
    *,
    base: str,
    user: str,
    password: str,
    tree_id: str,
    with_events: bool,
    rows_to_load: int = 1000,
    make_page: Callable[[], Page] | None = None,
) -> dict[str, Any]:
    """One measurement pass over the fabric as it is now. ``make_page`` opens
    a page that already has :data:`PAGE_PROBES` (default: :func:`new_page`)."""
    opener = make_page or (lambda: new_page(browser))
    page = opener()
    sign_in(page, base, user, password)
    results: dict[str, Any] = {
        "list": measure_list(page, base, rows_to_load=rows_to_load)
    }
    if with_events:
        node_page = opener()
        sign_in(node_page, base, user, password)
        nodes = api.node_ids()
        results["events"] = measure_events(
            page, node_page, base, api, tree_id, nodes[0]
        )
    return results
