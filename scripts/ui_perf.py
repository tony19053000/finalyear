"""Dashboard performance under load (V0.8 G7, roadmap V0.8 criterion 2, D174).

Seeds job history through the REAL API (so the real DB holds it) and drives
real Chromium through the dashboard, measuring what a user feels:

* **seed** - ``history``: N jobs per node, pinned to each node and RUN for real
  (attempts, logs, decisions in the DB: "4 nodes x 100 jobs history");
  ``bulk``: M more jobs (submit + cancel, so no agent runs them: the 10 000
  jobs headroom of D174).
* **list** - jobs list first render (cold page load, and in-app navigation),
  loading rows by scrolling, a scroll through the virtualised rows (long tasks
  and frame gaps), a filter change, opening a job's page.
* **events** - event latency with a steady job stream running: a heartbeat
  sample and every ``job.state`` change, from the controller's timestamp to the
  moment the page shows it (p50/p95). Controller and browser must share a
  clock for this number: run the browser on the controller's host (the e2e
  harness does) - on a LAN fabric, skew between the hosts is added to it.

The e2e test ``tests/e2e/test_v08_g7_perf.py`` (``pytest -m perf``) runs all
of it on a cluster-harness fabric (4 real agents) and asserts the thresholds
below. Standalone against a running fabric (the 4-laptop lab run, 0.8.10)::

    MF_PASSWORD=... MF_TOKEN=... uv run python scripts/ui_perf.py \\
        --controller https://192.168.1.20:47110 --ca ~/.meshforge/identity/ca.crt \\
        --user admin --history 100 --bulk 10000 --out perf.json

``MF_TOKEN`` is an admin API token (``compute login`` stores one; it seeds
through the API), ``MF_PASSWORD`` the admin's password (the browser signs in).
Needs the dev dependency group (Playwright) and ``uv run playwright install
chromium``. Seeding is real work for the fabric: 100 runs per node.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import platform
import socket
import ssl
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page

#: Criterion 2 and the D174 headroom, as asserted by the -m perf test.
THRESHOLDS_MS: Final[dict[str, float]] = {
    "list_first_render_p95": 3000.0,
    "list_in_app_nav_p95": 1000.0,
    "filter_change_p95": 1500.0,
    "detail_open_p95": 1500.0,
    "scroll_long_task_max": 200.0,
    "heartbeat_latency_p95": 2000.0,
    "job_state_latency_p95": 2000.0,
}
TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})
#: Cancelled jobs added to the run history so a state filter has two
#: non-empty answers to switch between (succeeded <-> cancelled).
MIXED_CANCELLED: Final = 20
JOB_ROW: Final = 'table tbody tr a[href^="/jobs/job_"]'


# --------------------------------------------------------------- numbers


def summary(values: Sequence[float]) -> dict[str, float]:
    """min / p50 / p95 / max (ms) of ``values``; an empty list says so."""
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    p95 = (
        statistics.quantiles(ordered, n=20, method="inclusive")[-1]
        if len(ordered) > 1
        else ordered[0]
    )
    return {
        "n": len(ordered),
        "min": round(ordered[0], 1),
        "p50": round(statistics.median(ordered), 1),
        "p95": round(p95, 1),
        "max": round(ordered[-1], 1),
    }


def machine() -> dict[str, Any]:
    """Where the numbers were measured (they mean little without it)."""
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
    }
    with contextlib.suppress(OSError):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                info["cpu"] = line.split(":", 1)[1].strip()
                break
    with contextlib.suppress(OSError):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                info["ram_gib"] = round(int(line.split()[1]) / 1024 / 1024, 1)
                break
    return info


# --------------------------------------------------------------- the API


@dataclasses.dataclass
class Api:
    """A bearer-token REST client for seeding and reading the truth."""

    base: str
    token: str
    verify: ssl.SSLContext

    def __post_init__(self) -> None:
        self.http = httpx.Client(
            base_url=self.base,
            verify=self.verify,
            timeout=30.0,
            headers={"authorization": f"Bearer {self.token}"},
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
        )

    def close(self) -> None:
        self.http.close()

    def ok(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = self.http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else None

    def node_ids(self) -> list[str]:
        nodes = self.ok("GET", "/v1/nodes")["nodes"]
        return [n["node_id"] for n in nodes if n["state"] == "online"]

    def upload_project(self, root: Path) -> str:
        """Upload ``root`` as a tree (the CLI's own tree builder); its id."""
        from meshforge.common.tree import build_tree

        built = build_tree(root)
        for hex_, path in built.sources.items():
            self.ok("PUT", f"/v1/blobs/{hex_}", content=path.read_bytes())
        self.ok("PUT", f"/v1/blobs/{built.tree.tree_id.hex}", content=built.manifest)
        return str(built.tree.tree_id)

    def submit(self, tree_id: str, name: str, node_id: str | None = None) -> str:
        body: dict[str, Any] = {
            "manifest": {
                "name": name,
                "entrypoint": ["python", "main.py"],
                "retry": {"max_attempts": 1},
            },
            "tree_id": tree_id,
        }
        if node_id is not None:
            body["placement"] = {"node_id": node_id}
        return str(self.ok("POST", "/v1/jobs", json=body)["job_id"])

    def cancel(self, job_id: str) -> None:
        self.ok("POST", f"/v1/jobs/{job_id}/cancel")

    def job(self, job_id: str) -> dict[str, Any]:
        return dict(self.ok("GET", f"/v1/jobs/{job_id}"))


def tiny_project(root: Path) -> Path:
    project = root / "ui-perf-job"
    project.mkdir(parents=True, exist_ok=True)
    (project / "main.py").write_text(
        "import sys\nprint('ui-perf', sys.argv)\n", encoding="utf-8"
    )
    return project


@dataclasses.dataclass
class Seeded:
    jobs: int
    seconds: float
    states: dict[str, int]


def _wait_terminal(api: Api, ids: Sequence[str], timeout_s: float) -> dict[str, int]:
    pending = set(ids)
    states: dict[str, int] = {}
    deadline = time.monotonic() + timeout_s
    while pending:
        if time.monotonic() > deadline:
            raise RuntimeError(f"{len(pending)} seeded jobs never finished")
        for job_id in list(pending):
            state = api.job(job_id)["state"]
            if state in TERMINAL:
                pending.discard(job_id)
                states[state] = states.get(state, 0) + 1
        if pending:
            time.sleep(1.0)
    return states


def seed_history(
    api: Api, tree_id: str, per_node: int, timeout_s: float = 1800.0
) -> Seeded:
    """``per_node`` jobs pinned to EACH online node, run for real."""
    started = time.monotonic()
    nodes = api.node_ids()
    if not nodes:
        raise RuntimeError("no online node to seed history on")
    ids = [
        api.submit(tree_id, f"history-{n:02d}-{i:03d}", node_id)
        for i in range(per_node)
        for n, node_id in enumerate(nodes)
    ]
    states = _wait_terminal(api, ids, timeout_s)
    return Seeded(len(ids), round(time.monotonic() - started, 1), states)


def seed_bulk(api: Api, tree_id: str, count: int, workers: int = 8) -> Seeded:
    """``count`` jobs submitted and cancelled at once (rows in the real DB;
    no agent spends time on them)."""
    started = time.monotonic()

    def one(i: int) -> None:
        job_id = api.submit(tree_id, f"bulk-{i:05d}")
        api.cancel(job_id)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(one, range(count)):
            pass
    return Seeded(count, round(time.monotonic() - started, 1), {"cancelled": count})


class JobStream(threading.Thread):
    """A steady stream of small auto-placed jobs (one every ``interval_s``)."""

    def __init__(self, api: Api, tree_id: str, interval_s: float = 1.0) -> None:
        super().__init__(name="ui-perf-job-stream", daemon=True)
        self.api, self.tree_id, self.interval_s = api, tree_id, interval_s
        self.ids: list[str] = []
        self.errors: list[str] = []
        self._halt = threading.Event()

    def run(self) -> None:
        n = 0
        while not self._halt.is_set():
            try:
                self.ids.append(self.api.submit(self.tree_id, f"stream-{n:04d}"))
            except Exception as exc:  # noqa: BLE001 - reported, the stream goes on
                self.errors.append(str(exc))
            n += 1
            self._halt.wait(self.interval_s)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=30)


# --------------------------------------------------------------- browser


def ssl_context(ca_file: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(ca_file))


def spki_pin(base: str, context: ssl.SSLContext) -> str:
    """base64 SHA-256 of the controller's public key, after verifying its
    certificate against the fabric CA (Chromium then trusts exactly it)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    parts = urlsplit(base)
    host, port = parts.hostname or "", parts.port or 443
    with (
        socket.create_connection((host, port), timeout=10) as raw,
        context.wrap_socket(raw, server_hostname=host) as tls,
    ):
        der = tls.getpeercert(binary_form=True)
    if der is None:
        raise RuntimeError("the controller presented no certificate")
    spki = (
        x509.load_der_x509_certificate(der)
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


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


def _parse_at(value: str) -> float:
    return datetime.fromisoformat(value).timestamp() * 1000.0


def measure_events(
    jobs_page: Page,
    node_page: Page,
    base: str,
    api: Api,
    tree_id: str,
    node_id: str,
    *,
    min_jobs: int = 20,
    min_samples: int = 15,
    interval_s: float = 1.0,
    timeout_s: float = 240.0,
) -> dict[str, Any]:
    """Event -> DOM latency while a steady job stream runs."""
    jobs_page.goto(base + "/jobs")
    jobs_page.locator("table").first.wait_for()
    jobs_page.evaluate(
        """() => {
          window.__mfSeen = {};
          const scan = () => {
            for (const r of document.querySelectorAll('table tbody tr')) {
              const a = r.querySelector('a[href^="/jobs/job_"]');
              if (!a || !r.cells[1]) continue;
              const id = a.getAttribute('href').split('/').pop();
              const st = r.cells[1].textContent.trim();
              const seen = (window.__mfSeen[id] ||= {});
              if (!(st in seen)) seen[st] = Date.now();
            }
          };
          scan();
          new MutationObserver(scan).observe(document.body,
            {subtree: true, childList: true, characterData: true, attributes: true});
        }"""
    )
    node_page.goto(f"{base}/nodes/{node_id}")
    node_page.get_by_test_id("latest-sample").wait_for()
    node_page.evaluate(
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
          new MutationObserver(note).observe(card,
            {subtree: true, attributes: true, childList: true, characterData: true});
        }"""
    )
    stream = JobStream(api, tree_id, interval_s)
    stream.start()
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            samples = int(node_page.evaluate("window.__mfLag.length"))
            if len(stream.ids) >= min_jobs and samples >= min_samples:
                break
            jobs_page.wait_for_timeout(500)
    finally:
        stream.stop()
    ids = list(stream.ids)
    _wait_terminal(api, ids, timeout_s)
    jobs_page.wait_for_timeout(3000)  # let the last events land in the page
    seen: dict[str, dict[str, float]] = jobs_page.evaluate("window.__mfSeen")
    lags: list[float] = []
    terminal_lags: list[float] = []
    missed_terminal = 0
    for job_id in ids:
        job = api.job(job_id)
        shown = seen.get(job_id, {})
        for transition in job["transitions"]:
            state = transition["to_state"]
            if state in shown:
                lag = shown[state] - _parse_at(transition["at"])
                lags.append(lag)
                if state in TERMINAL:
                    terminal_lags.append(lag)
            elif state in TERMINAL:
                missed_terminal += 1
    heartbeat = [float(x) for x in node_page.evaluate("window.__mfLag")]
    return {
        "stream_jobs": len(ids),
        "stream_errors": stream.errors,
        "job_state_latency": summary(lags),
        "job_terminal_latency": summary(terminal_lags),
        "job_terminal_missed": missed_terminal,
        "heartbeat_latency": summary(heartbeat),
    }


def check(results: dict[str, Any]) -> list[str]:
    """Each threshold that the results break (empty = all met)."""
    got = {
        "list_first_render_p95": results["list"]["list_first_render"]["p95"],
        "list_in_app_nav_p95": results["list"]["list_in_app_nav"]["p95"],
        "filter_change_p95": results["list"]["filter_change"]["p95"],
        "detail_open_p95": results["list"]["detail_open"]["p95"],
        "scroll_long_task_max": results["list"]["scroll"]["long_task_max"],
    }
    if "events" in results:
        got["heartbeat_latency_p95"] = results["events"]["heartbeat_latency"]["p95"]
        got["job_state_latency_p95"] = results["events"]["job_state_latency"]["p95"]
    return [
        f"{name}: {value} ms > {THRESHOLDS_MS[name]} ms"
        for name, value in got.items()
        if value > THRESHOLDS_MS[name]
    ]


@contextlib.contextmanager
def chromium(base: str, verify: ssl.SSLContext) -> Iterator[Browser]:
    from playwright.sync_api import sync_playwright

    pin = spki_pin(base, verify)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=[f"--ignore-certificate-errors-spki-list={pin}"]
        )
        try:
            yield browser
        finally:
            browser.close()


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


def table(phases: dict[str, dict[str, Any]]) -> str:
    """The results as a Markdown table (one column per phase)."""
    rows = [
        ("jobs list first render (cold load), ms", "list", "list_first_render"),
        ("jobs list via in-app nav, ms", "list", "list_in_app_nav"),
        ("load next 50 rows by scrolling, ms", "list", "load_more_by_scroll"),
        ("filter change -> rows shown, ms", "list", "filter_change"),
        ("job page open, ms", "list", "detail_open"),
        ("heartbeat -> DOM, ms", "events", "heartbeat_latency"),
        ("job.state -> DOM, ms", "events", "job_state_latency"),
    ]
    names = list(phases)
    lines = [
        "| measure (p50 / p95 / max) | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for label, group, key in rows:
        cells = []
        for name in names:
            s = phases[name].get(group, {}).get(key)
            cells.append(
                "-"
                if not s or not s.get("n")
                else f"{s['p50']} / {s['p95']} / {s['max']}"
            )
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    scroll_cells = []
    for name in names:
        sc = phases[name]["list"]["scroll"]
        scroll_cells.append(
            f"max long task {sc['long_task_max']} ms ({sc['long_tasks']} tasks), "
            f"{phases[name]['list']['rows_loaded']} rows loaded, "
            f"<= {sc['max_dom_rows']} in the DOM"
        )
    lines.append("| scroll through the list | " + " | ".join(scroll_cells) + " |")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--controller", required=True, help="https://host:port")
    parser.add_argument("--ca", type=Path, required=True, help="the fabric ca.crt")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--history", type=int, default=100, help="jobs per node")
    parser.add_argument("--bulk", type=int, default=10_000)
    parser.add_argument("--skip-seed", action="store_true")
    parser.add_argument("--out", type=Path, help="write the results as JSON here")
    args = parser.parse_args(argv)
    password, token = os.environ.get("MF_PASSWORD"), os.environ.get("MF_TOKEN")
    if not password or not token:
        print("set MF_PASSWORD (browser sign-in) and MF_TOKEN (admin API token)")
        return 2
    base = args.controller.rstrip("/")
    verify = ssl_context(args.ca)
    api = Api(base, token, verify)
    phases: dict[str, dict[str, Any]] = {}
    seeds: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tree_id = api.upload_project(tiny_project(Path(tmp)))
        with chromium(base, verify) as browser:
            if not args.skip_seed:
                seeds["history"] = dataclasses.asdict(
                    seed_history(api, tree_id, args.history)
                )
                seeds["mixed"] = dataclasses.asdict(
                    seed_bulk(api, tree_id, MIXED_CANCELLED)
                )
            phases["history"] = run(
                browser, api, base=base, user=args.user, password=password,
                tree_id=tree_id, with_events=True,
            )  # fmt: skip
            if args.bulk and not args.skip_seed:
                seeds["bulk"] = dataclasses.asdict(seed_bulk(api, tree_id, args.bulk))
                phases["bulk"] = run(
                    browser, api, base=base, user=args.user, password=password,
                    tree_id=tree_id, with_events=False,
                )  # fmt: skip
    finally:
        api.close()
    result = {"machine": machine(), "seeds": seeds, "phases": phases}
    problems = [p for phase in phases.values() for p in check(phase)]
    print(table(phases))
    print(json.dumps({"machine": result["machine"], "seeds": seeds}, indent=2))
    if args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for problem in problems:
        print("THRESHOLD BROKEN:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
