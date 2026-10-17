"""V0.8 G7 (0.8.9, criterion 1 + 3): the Mode 1 job life cycle end to end in
the browser, keyboard only where D179 says (login, submit, cancel, logout),
every step checked against the API - and against the real ``compute`` CLI
where the brief asks for the same answer from both (outputs, plan, job list).

Real controller + 2 real agents (cluster harness, subprocess runtime), the
built SPA, real Chromium. The shared ``new_page`` guard checks every page at
the end (console, uncaught errors, CSP, axe on the final state).

The other Mode 1 flows have their own end-to-end tests, named in the parity
table (tests/e2e/parity.py): node revoke by keyboard (G3), invite -> join
(G5), API tokens / secrets / data (G5).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import NORMAL_USER
from mf_e2e import (
    EXEC_OPTIONS,
    assert_axe_clean,
    get_job,
    run_compute,
    tab_to,
    tab_to_locator,
    wait_job_state,
)
from playwright.sync_api import expect

from meshforge.common.bundle import unpack

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(360),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

JOB_URL = re.compile(r"/jobs/(job_[0-9A-Z]{26})$")


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    return dict(EXEC_OPTIONS)


def _project(root: Path, name: str, *, lines: int, pause_s: float) -> Path:
    project = root / name
    (project / "data").mkdir(parents=True)
    (project / "main.py").write_text(
        textwrap.dedent(
            f"""\
            import os, time
            os.makedirs("results/deep", exist_ok=True)
            for i in range({lines}):
                print(f"line {{i}} of {lines}", flush=True)
                time.sleep({pause_s})
            text = open("data/input.txt").read()
            open("results/upper.txt", "w").write(text.upper())
            open("results/deep/count.txt", "w").write(str(len(text)))
            print("done", flush=True)
            """
        ),
        encoding="utf-8",
    )
    (project / "data" / "input.txt").write_text("mode one from the browser\n", "utf-8")
    (project / "compute.yaml").write_text(
        "entrypoint: python main.py\n"
        "env:\n  base: python-cpu\n"
        "resources:\n  gpu: none\n  cpus: 1\n"
        "outputs: [results/]\n"
        "limits:\n  timeout: 5m\n",
        "utf-8",
    )
    return project


def _files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _keyboard_login(page: Page, fabric: Fabric, user: str) -> None:
    page.goto(fabric.http_base + "/")
    expect(page.get_by_label("User name")).to_be_focused()
    page.keyboard.type(user)
    page.keyboard.press("Tab")
    expect(page.get_by_label("Password")).to_be_focused()
    page.keyboard.type(fabric.password(user))
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", level=1, name="Overview")).to_be_visible()
    expect(page.get_by_test_id("current-user")).to_have_text(f"{user}(user)")
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )


def _keyboard_submit(page: Page, fabric: Fabric, project: Path) -> str:
    page.goto(fabric.http_base + "/jobs/new")
    expect(page.get_by_role("heading", level=1, name="Submit a job")).to_be_visible()
    tab_to_locator(page, page.get_by_label("Project folder"))
    with page.expect_file_chooser() as chooser:
        page.keyboard.press("Space")
    chooser.value.set_files(project)
    expect(page.get_by_label("Project summary")).to_contain_text(project.name)
    tab_to_locator(page, page.get_by_role("button", name="Upload and submit"))
    page.keyboard.press("Enter")
    expect(page).to_have_url(JOB_URL)
    match = JOB_URL.search(page.url)
    assert match is not None
    return match.group(1)


def _open_tab(page: Page, name: str | re.Pattern[str]) -> None:
    page.get_by_role("tablist", name="Job details").get_by_role(
        "tab", name=name
    ).click()


def _assert_attempts_tab(page: Page, job: dict[str, Any]) -> None:
    """The Attempts tab lists exactly the API's attempts, with their node and
    state."""
    _open_tab(page, re.compile(r"^Attempts \("))
    table = page.get_by_role("table", name="Attempts, newest first")
    rows = table.locator("tbody tr")
    expect(rows).to_have_count(len(job["attempts"]))
    for attempt in job["attempts"]:
        row = rows.filter(has_text=attempt["attempt_id"])
        expect(row).to_have_count(1)
        expect(row).to_contain_text(attempt["node_id"])
        expect(row).to_contain_text(attempt["state"])


@pytest.mark.docker
def test_mode1_job_life_cycle_in_the_browser_matches_the_api_and_the_cli(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, _watch = new_page(accept_downloads=True)
    _keyboard_login(page, fabric, NORMAL_USER)

    # --- Submit a folder (keyboard only) -> it runs on a real agent.
    project = _project(tmp_path, "mode1-proj", lines=6, pause_s=0.4)
    job_id = _keyboard_submit(page, fabric, project)
    log = page.get_by_role("log", name="Job output")
    expect(log).to_contain_text("line 0 of 6", timeout=60_000)  # live, mid-run
    job = wait_job_state(fabric, job_id, {"succeeded"}, 90, NORMAL_USER)
    (attempt,) = job["attempts"]
    assert attempt["node_id"] in {n.node_id for n in fabric.nodes}
    expect(page.get_by_test_id("log-end")).to_contain_text(
        "Job succeeded", timeout=30_000
    )
    expect(page.get_by_role("region", name="Summary")).to_contain_text("succeeded")

    # The page's log holds every line the API stored for the job.
    stored = fabric.call(
        "GET",
        f"/v1/jobs/{job_id}/logs",
        as_user=NORMAL_USER,
        params={"stream": "stdout"},
    )
    assert stored.status_code == 200, stored.text
    shown = log.inner_text()
    for line in filter(None, (x.strip() for x in stored.text.splitlines())):
        assert line in shown, (line, shown[-500:])

    # --- Outputs: the browser's download is the CLI's `compute get`, byte for byte.
    _open_tab(page, "Outputs")
    digest = attempt["output_digest"]
    assert digest and digest.startswith("sha256:")
    with page.expect_download() as info:
        page.get_by_role("link", name="Download outputs").click()
    archive = Path(info.value.path())
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest.removeprefix(
        "sha256:"
    )
    browser_tree = tmp_path / "from-browser"
    unpack(archive, browser_tree)
    cli_tree = tmp_path / "from-cli"
    got = run_compute(
        fabric, "get", job_id, "--dest", str(cli_tree), cwd=tmp_path, user=NORMAL_USER
    )
    assert got.returncode == 0, got.stderr
    assert _files(browser_tree) == _files(cli_tree)
    assert _files(cli_tree) == {
        "results/upper.txt": b"MODE ONE FROM THE BROWSER\n",
        "results/deep/count.txt": b"26",
    }

    # --- Attempts: the API's attempts.
    _assert_attempts_tab(page, job)

    # --- Plan explanation: the decision table the API (and `compute explain`) give.
    _open_tab(page, "Plan")
    explained = run_compute(
        fabric, "explain", job_id, "--json", cwd=tmp_path, user=NORMAL_USER
    )
    assert explained.returncode == 0, explained.stderr
    cli_plan = json.loads(explained.stdout)
    api_plan = fabric.call(
        "GET", f"/v1/jobs/{job_id}/decisions", as_user=NORMAL_USER
    ).json()
    assert cli_plan["count"] == api_plan["count"] >= 1
    chosen = api_plan["decisions"][-1]["chosen_node_id"]
    assert cli_plan["decisions"][-1]["chosen_node_id"] == chosen == attempt["node_id"]
    candidates = page.get_by_role("table", name=re.compile("Candidates of decision"))
    expect(candidates.last).to_contain_text(chosen)
    expect(candidates.last.get_by_text("chosen", exact=True)).to_have_count(1)
    assert_axe_clean(page, "the plan tab")

    # --- The jobs list: the same jobs and states as `compute jobs --json`.
    listed = run_compute(fabric, "jobs", "--json", cwd=tmp_path, user=NORMAL_USER)
    assert listed.returncode == 0, listed.stderr
    cli_jobs = {j["job_id"]: j["state"] for j in json.loads(listed.stdout)["jobs"]}
    assert cli_jobs == {job_id: "succeeded"}
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Jobs", exact=True
    ).click()
    table = page.get_by_role("table", name="Jobs, newest first")
    row = table.locator("tbody tr").filter(has_text=job_id)
    expect(row).to_have_count(1)
    expect(row).to_contain_text("succeeded")

    # --- Cancel a running job (keyboard only) -> the API says cancelled.
    long_project = _project(tmp_path, "mode1-long", lines=600, pause_s=0.1)
    long_id = _keyboard_submit(page, fabric, long_project)
    expect(page.get_by_role("log", name="Job output")).to_contain_text(
        "line 1 of 600", timeout=60_000
    )
    tab_to_locator(page, page.get_by_role("button", name="Cancel job"))
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Cancel this job?")
    expect(dialog).to_be_visible()
    tab_to_locator(page, dialog.get_by_role("button", name="Cancel job"), limit=5)
    page.keyboard.press("Enter")
    expect(page.get_by_text("Cancel requested", exact=True)).to_be_visible()
    cancelled = wait_job_state(fabric, long_id, {"cancelled"}, 60, NORMAL_USER)
    expect(page.get_by_role("region", name="Summary")).to_contain_text(
        "cancelled", timeout=30_000
    )
    expect(page.get_by_role("button", name="Cancel job")).to_be_disabled()
    cancelled = get_job(fabric, long_id, NORMAL_USER)  # attempts settled
    _assert_attempts_tab(page, cancelled)

    # --- Sign out (keyboard only) -> the browser session is gone server-side.
    session = next(
        c["value"] for c in page.context.cookies() if c["name"] == "mf_session"
    )
    tab_to(page, "Sign out", limit=60)
    page.keyboard.press("Enter")
    expect(page.get_by_text("You are signed out.")).to_be_visible()
    assert fabric.ssl_context is not None
    with httpx.Client(verify=fabric.ssl_context, timeout=10) as http:
        probe = http.get(
            fabric.http_base + "/v1/session",
            headers={"cookie": f"mf_session={session}"},
        )
    assert probe.status_code == 200, probe.text
    assert probe.json() == {"session": None}
