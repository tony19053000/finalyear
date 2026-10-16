"""V0.8 G7 criterion 2: the dashboard stays responsive with 4 nodes x 100 jobs
of history, and still at 10 000 jobs (D174); live updates reach the page in
<= 2 s (p95) while a steady job stream runs.

``pytest -m perf`` (skipped by default: it runs 400 real jobs and seeds
10 000 more - several minutes). The measuring itself is
``scripts/ui_perf.py`` (also runnable against a real fabric); this test runs
it on a cluster-harness fabric with 4 real agents and asserts
``ui_perf.THRESHOLDS_MS``. The numbers are printed (``-s``) and written to
``<tmp>/ui-perf.json``; record them with the machine's specs.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import ADMIN_USER
from mf_e2e import EXEC_OPTIONS

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Browser, Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.perf,
    pytest.mark.timeout(5400),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ui_perf.py"
NODES = 4
HISTORY_PER_NODE = 100
BULK = 10_000


def _ui_perf() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ui_perf", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "fabric_options",
    [
        {
            **EXEC_OPTIONS,
            "agents": NODES,
            # Every heartbeat sample reported, once a second (as G3's latency
            # test): enough samples for a p95 in a minute.
            "agent_resources": {
                "sample_interval_s": 1.0,
                "change_threshold_percent": 0.0,
                "max_report_interval_s": 1.0,
            },
        }
    ],
)
def test_dashboard_stays_responsive_at_4x100_history_and_10k_jobs(
    fabric: Fabric,
    dashboard_browser: Browser,
    new_page: NewPage,
    tmp_path: Path,
) -> None:
    ui_perf = _ui_perf()
    assert fabric.ssl_context is not None
    base = fabric.http_base
    api = ui_perf.Api(base, fabric.api_token(), fabric.ssl_context)

    def make_page() -> Page:
        page, _watch = new_page(timeout_ms=30_000)
        page.add_init_script(ui_perf.PAGE_PROBES)
        return page

    def measure(with_events: bool) -> dict[str, Any]:
        result: dict[str, Any] = ui_perf.run(
            dashboard_browser,
            api,
            base=base,
            user=ADMIN_USER,
            password=fabric.password(ADMIN_USER),
            tree_id=tree_id,
            with_events=with_events,
            make_page=make_page,
        )
        return result

    seeds: dict[str, Any] = {}
    phases: dict[str, Any] = {}
    try:
        tree_id = api.upload_project(ui_perf.tiny_project(tmp_path))
        assert len(api.node_ids()) == NODES
        history = ui_perf.seed_history(api, tree_id, HISTORY_PER_NODE)
        seeds["history"] = dataclasses.asdict(history)
        assert history.states == {"succeeded": NODES * HISTORY_PER_NODE}, history
        phases["4x100"] = measure(with_events=True)
        seeds["bulk"] = dataclasses.asdict(ui_perf.seed_bulk(api, tree_id, BULK))
        phases["10k"] = measure(with_events=True)
    finally:
        api.close()

    report = {"machine": ui_perf.machine(), "seeds": seeds, "phases": phases}
    (tmp_path / "ui-perf.json").write_text(json.dumps(report, indent=2), "utf-8")
    print("\n" + ui_perf.table(phases))  # noqa: T201 - the measured numbers (-s)
    print(json.dumps({"machine": report["machine"], "seeds": seeds}, indent=2))  # noqa: T201
    print(f"full results: {tmp_path / 'ui-perf.json'}")  # noqa: T201

    for name, phase in phases.items():
        listed = phase["list"]
        assert listed["scroll"]["windowed"], (name, "over 200 rows must be windowed")
        assert listed["scroll"]["max_dom_rows"] < listed["rows_loaded"], name
        events = phase["events"]
        assert events["stream_errors"] == [], (name, events["stream_errors"])
        assert events["job_terminal_missed"] == 0, (name, events)
        assert events["job_state_latency"]["n"] >= 20, (name, events)
        assert events["heartbeat_latency"]["n"] >= 10, (name, events)
    assert phases["10k"]["list"]["rows_loaded"] >= 1000
    problems = {name: ui_perf.check(phase) for name, phase in phases.items()}
    assert not any(problems.values()), problems
