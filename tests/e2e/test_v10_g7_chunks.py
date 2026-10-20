"""V0.10 G7 (0.10.8, D270): a split job's Chunks tab end to end.

Real controller + 2 real agents (cluster harness, subprocess runtime) run a
small split job (40 range items, 2 per chunk, each chunk sleeps), the built
SPA, real Chromium. The shared ``new_page`` guard checks every page at the end
(console, uncaught errors, CSP, axe on the final state).

1. Mid-run the tab shows what ``GET /v1/jobs/{id}/split`` returns (the
   controller's rows, checklist 37): items and chunks done (polled until the
   page and the API agree on one moment), and it moves on live.
2. At the end every number on the page equals the API's, and equals
   ``compute watch JOB --json`` (the CLI); the per-node table has one row per
   lane with the API's chunks / items; the grid has one cell per chunk, all
   done; a clicked cell shows that chunk as ``/chunks?start=N`` returns it.
3. A 10 000-chunk job (the same page, the summary answered by a Playwright
   route - a fake split job of the maximum size): the grid is virtualised in
   the real browser (a few hundred cells in the DOM, not 10 000), scrolling
   to the end draws the last chunk.

Screenshots: set ``MESHFORGE_E2E_SCREENSHOTS=<dir>`` to keep them
(mid-run, final, 10 000 chunks).
"""

from __future__ import annotations

import json
import os
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_e2e import (
    EXEC_OPTIONS,
    assert_axe_clean,
    put_tree,
    run_compute,
    sign_in,
    submit_job,
    wait_all_reporting,
    wait_job_state,
)
from playwright.sync_api import expect

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Page, Route

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(360),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

ITEMS = 40
CHUNK = 2
CHUNKS = ITEMS // CHUNK


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    return dict(EXEC_OPTIONS)


def _project(root: Path) -> Path:
    project = root / "split-proj"
    project.mkdir(parents=True)
    (project / "main.py").write_text(
        textwrap.dedent(
            """\
            import json, os, sys, time
            items = json.load(open(sys.argv[1]))
            out = sys.argv[2]
            time.sleep(1.0)
            with open(os.path.join(out, "squares.txt"), "w") as fh:
                for item in items:
                    fh.write(f"{item} {int(item) ** 2}\\n")
            """
        ),
        encoding="utf-8",
    )
    return project


def _shot(page: Page, name: str) -> None:
    folder = os.environ.get("MESHFORGE_E2E_SCREENSHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"), full_page=True)


def _split(fabric: Fabric, job_id: str, *, with_map: bool = False) -> dict[str, Any]:
    resp = fabric.call(
        "GET",
        f"/v1/jobs/{job_id}/split",
        params={"map": "true"} if with_map else None,
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _field(page: Page, name: str) -> str:
    return page.locator(f'[data-field="{name}"]').first.inner_text().strip()


def _open_chunks(page: Page) -> None:
    page.get_by_role("tablist", name="Job details").get_by_role(
        "tab", name="Chunks"
    ).click()
    expect(page.get_by_role("region", name="Progress")).to_be_visible()


def _agree(page: Page, fabric: Fabric, job_id: str, within_s: float) -> dict[str, Any]:
    """Poll until the page shows exactly one API read (between two equal
    reads, so the rows did not move while the page was read)."""
    deadline = time.monotonic() + within_s
    last: tuple[str, str] | None = None
    while time.monotonic() < deadline:
        before = _split(fabric, job_id)
        shown = (_field(page, "items"), _field(page, "chunks-done"))
        after = _split(fabric, job_id)
        want = (
            f"{before['items_done']} / {before['items_total']}",
            f"{before['chunks']['done']} / {before['chunks']['total']}",
        )
        if before["chunks"] == after["chunks"] and shown == want:
            return before
        last = shown
        time.sleep(0.2)
    pytest.fail(f"the page never showed the API's numbers (last shown {last})")


def _big_summary(real: dict[str, Any]) -> dict[str, Any]:
    big = dict(real)
    big["chunk_map"] = "d" * 6000 + "l" * 4 + "s" + "f" + "p" * 3994
    big["chunks"] = {
        "total": 10000,
        "pending": 3994,
        "leased": 5,
        "done": 6000,
        "failed": 1,
    }
    big["items_total"] = 10000
    big["items_done"] = 6000
    return big


def test_split_job_chunks_tab_matches_the_api_and_the_cli(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    wait_all_reporting(fabric, 2)
    tree_id = put_tree(fabric, _project(tmp_path))
    job_id = submit_job(
        fabric,
        tree_id,
        "split-e2e",
        entrypoint=["python", "main.py", "{items_file}", "{out}"],
        env={"base": "python-cpu"},
        resources={"gpu": "none", "cpus": 1},
        limits={"timeout": "5m"},
        split={"over": {"range": {"stop": ITEMS}}, "chunk": {"items": CHUNK}},
    )

    page, _watch = new_page()
    sign_in(page, fabric, path=f"/jobs/{job_id}")
    expect(page.get_by_role("heading", level=1, name="split-e2e")).to_be_visible()
    _open_chunks(page)

    # --- mid-run: the page equals the API (the rows) at one moment
    deadline = time.monotonic() + 90
    while _split(fabric, job_id)["chunks"]["done"] < 2:
        assert time.monotonic() < deadline, _split(fabric, job_id)
        time.sleep(0.2)
    mid = _agree(page, fabric, job_id, 30)
    if mid["job_state"] == "running":
        _shot(page, "V0.10-G7-chunks-mid-run")
    expect(page.locator("svg[role=img] rect")).to_have_count(CHUNKS)

    # --- the end: page == API == CLI
    wait_job_state(fabric, job_id, {"succeeded"}, 120)
    final = _split(fabric, job_id, with_map=True)
    assert final["chunks"]["done"] == CHUNKS and final["items_done"] == ITEMS
    expect(page.locator('[data-field="chunks-done"]')).to_have_text(
        f"{CHUNKS} / {CHUNKS}", timeout=15_000
    )
    expect(page.locator('[data-field="items"]')).to_have_text(f"{ITEMS} / {ITEMS}")
    expect(page.get_by_role("region", name="Progress")).to_contain_text("succeeded")
    expect(page.locator("svg[role=img] rect.fill-status-ok")).to_have_count(CHUNKS)
    expect(page.locator('[data-legend="d"]')).to_have_text(f"done {CHUNKS}")
    lanes = page.get_by_role("table", name=re.compile("One row per lane"))
    rows = lanes.locator("tbody tr")
    expect(rows).to_have_count(len(final["lanes"]))
    assert final["lanes"], final
    for lane in final["lanes"]:
        row = rows.filter(has_text=lane["node_id"])
        expect(row).to_have_count(1)
        expect(row.locator('[data-field="lane-chunks"]')).to_have_text(
            str(lane["chunks_done"])
        )
        expect(row.locator('[data-field="lane-items"]')).to_have_text(
            str(lane["items_done"])
        )
    assert sum(lane["items_done"] for lane in final["lanes"]) == ITEMS
    cli = run_compute(fabric, "watch", job_id, "--json", cwd=tmp_path)
    assert cli.returncode == 0, cli.stderr
    from_cli = json.loads(cli.stdout)
    for key in ("job_state", "chunks", "items_done", "items_total", "lanes_total"):
        assert from_cli[key] == final[key], key
    assert [(x["node_id"], x["chunks_done"]) for x in from_cli["lanes"]] == [
        (x["node_id"], x["chunks_done"]) for x in final["lanes"]
    ]
    # a clicked cell shows that chunk exactly as the API returns it
    page.locator('svg[role=img] rect[data-chunk="7"]').click()
    chunk = fabric.call(
        "GET", f"/v1/jobs/{job_id}/chunks", params={"start": 7, "limit": 1}
    ).json()["chunks"][0]
    expect(page.locator('[data-field="chunk-index"]')).to_have_text("#7")
    expect(page.locator('[data-field="chunk-state"]')).to_have_text("done")
    expect(page.locator('[data-field="chunk-node"]')).to_have_text(chunk["node_id"])
    expect(page.locator('[data-field="chunk-items"]')).to_have_text(
        f"{chunk['first_item']}\u2013{chunk['first_item'] + chunk['item_count'] - 1} "
        f"({chunk['item_count']})"
    )
    assert_axe_clean(page, "the Chunks tab")
    _shot(page, "V0.10-G7-chunks-final")

    # --- 10 000 chunks: the same page, a fake summary of the maximum size
    big = _big_summary(final)

    def fake(route: Route) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(big))

    page.route(re.compile(rf"/v1/jobs/{job_id}/split\?map=true$"), fake)
    page.reload()
    _open_chunks(page)
    grid = page.get_by_role("img", name="10000 chunks coloured by state")
    expect(grid).to_be_visible()
    expect(page.locator('[data-field="chunks-done"]')).to_have_text("6000 / 10000")
    drawn = page.locator("svg[role=img] rect").count()
    columns = int(grid.get_attribute("data-columns") or "0")
    # virtualised: the rows of the 288 px viewport (24) + 4 overscan rows on
    # each side + one partly visible row - not 10 000 cells
    window = columns * (24 + 2 * 4 + 1)
    assert 0 < drawn <= window < 10_000, (drawn, columns)
    region = page.get_by_role("region", name=re.compile("^Chunk grid"))
    region.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    expect(page.locator('svg[role=img] rect[data-chunk="9999"]')).to_have_count(1)
    expect(page.locator('svg[role=img] rect[data-chunk="0"]')).to_have_count(0)
    assert page.locator("svg[role=img] rect").count() <= window
    region.evaluate("el => { el.scrollTop = 0; }")
    expect(page.locator('svg[role=img] rect[data-chunk="0"]')).to_have_count(1)
    _shot(page, "V0.10-G7-chunks-10000")
    page.unroute(re.compile(rf"/v1/jobs/{job_id}/split\?map=true$"))
