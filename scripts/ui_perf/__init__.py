"""Dashboard performance under load (V0.8 G7, roadmap V0.8 criterion 2, D174).

Seeds job history through the REAL API (so the real DB holds it) and drives
real Chromium through the dashboard, measuring what a user feels:

* **seed** (:mod:`.scenarios`) - ``history``: N jobs per node, pinned to each
  node and RUN for real (attempts, logs, decisions in the DB: "4 nodes x 100
  jobs history"); ``bulk``: M more jobs (submit + cancel, so no agent runs
  them: the 10 000 jobs headroom of D174).
* **list** (:mod:`.measure`) - jobs list first render (cold page load, and
  in-app navigation), loading rows by scrolling, a scroll through the
  virtualised rows (long tasks and frame gaps), a filter change, opening a
  job's page.
* **events** (:mod:`.events`) - event latency with a steady job stream
  running: a heartbeat sample and every ``job.state`` change, from the
  controller's timestamp to the moment the page shows it (p50/p95/max).
  Skew-safe (V0.9 G0, D213): only CONTROLLER timestamps are used - a heartbeat
  is timed from its arrival at the controller (the ``node.metrics`` event's
  ``recv_at``), never from the agent's own ``observed_at`` - and they are put
  on the browser's clock with the browser<->controller offset measured by
  round trips to ``GET /v1/clock`` just before (offset +- RTT/2,
  :mod:`.clocks`). The offset and its bound are in the results; a bound over
  :data:`MAX_CLOCK_BOUND_MS` makes the run ``skew_uncertain`` (exit 3), never
  a pass.
* **report** (:mod:`.report`) - thresholds, :func:`check`, :func:`verdict`,
  the Markdown table.

The e2e test ``tests/e2e/test_v08_g7_perf.py`` (``pytest -m perf``) runs all
of it on a cluster-harness fabric (4 real agents) and asserts the thresholds.
Standalone against a running fabric (the 4-laptop lab run, 0.8.10), from the
repository root::

    MF_PASSWORD=... MF_TOKEN=... uv run python scripts/ui_perf \\
        --controller https://192.168.1.20:47110 --ca ~/.meshforge/identity/ca.crt \\
        --user admin --history 100 --bulk 10000 --out perf.json

(``uv run python -m scripts.ui_perf ...`` is the same.) ``MF_TOKEN`` is an
admin API token (``compute login`` stores one; it seeds through the API),
``MF_PASSWORD`` the admin's password (the browser signs in). Needs the dev
dependency group (Playwright) and ``uv run playwright install chromium``.
Seeding is real work for the fabric: 100 runs per node. Exit code: 0 every
limit met, 1 a limit broken, 3 ``skew_uncertain`` (the clock offset could not
be bounded within :data:`MAX_CLOCK_BOUND_MS`).
"""

from __future__ import annotations

from .cli import chromium, main, spki_pin, ssl_context
from .clocks import (
    CLOCK_PING,
    CLOCK_PINGS,
    MAX_CLOCK_BOUND_MS,
    clock_report,
    measure_clock,
)
from .events import HeartbeatSamples, JobStateSamples, MetricsFrames, measure_events
from .events import _parse_at as _parse_at  # the V0.8 G7 tests use it
from .measure import (
    JOB_ROW,
    PAGE_PROBES,
    ROWS,
    UNTIL,
    measure_list,
    new_page,
    run,
    sign_in,
)
from .report import (
    EXIT_CODES,
    EXIT_FAIL,
    EXIT_PASS,
    EXIT_SKEW_UNCERTAIN,
    LIVE_LATENCY_MS,
    THRESHOLDS_MS,
    check,
    machine,
    skew_uncertain,
    summary,
    table,
    verdict,
)
from .scenarios import (
    MIXED_CANCELLED,
    TERMINAL,
    Api,
    JobStream,
    Seeded,
    seed_bulk,
    seed_history,
    tiny_project,
    wait_terminal,
)

__all__ = [
    "CLOCK_PING",
    "CLOCK_PINGS",
    "EXIT_CODES",
    "EXIT_FAIL",
    "EXIT_PASS",
    "EXIT_SKEW_UNCERTAIN",
    "JOB_ROW",
    "LIVE_LATENCY_MS",
    "MAX_CLOCK_BOUND_MS",
    "MIXED_CANCELLED",
    "PAGE_PROBES",
    "ROWS",
    "TERMINAL",
    "THRESHOLDS_MS",
    "UNTIL",
    "Api",
    "HeartbeatSamples",
    "JobStateSamples",
    "JobStream",
    "MetricsFrames",
    "Seeded",
    "check",
    "chromium",
    "clock_report",
    "machine",
    "main",
    "measure_clock",
    "measure_events",
    "measure_list",
    "new_page",
    "run",
    "seed_bulk",
    "seed_history",
    "sign_in",
    "skew_uncertain",
    "spki_pin",
    "ssl_context",
    "summary",
    "table",
    "tiny_project",
    "verdict",
    "wait_terminal",
]
