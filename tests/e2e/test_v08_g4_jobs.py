"""V0.8 G4 (0.8.5, D176, D177): the Jobs pages end to end - a real controller and
a real agent (cluster harness, subprocess runtime), the built SPA, real Chromium.

* Submit a project FOLDER from the browser -> the job runs on the agent -> its
  log streams into the page -> its outputs download with the digest the API
  reports. The browser-built tree id equals the CLI's own ``build_tree`` of the
  same folder (one spec, two implementations).
* Cancel a running job (keyboard only: submit and cancel without the mouse).
* The list pages through >= 120 jobs with the server's keyset cursor.
* Every page: zero console errors / CSP violations, axe clean (WCAG A/AA).
"""

from __future__ import annotations

import hashlib
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

from mf_cluster import ADMIN_USER, NORMAL_USER
from mf_dashboard import PageWatch, axe_violations
from playwright.sync_api import Browser, Page, expect

from meshforge.common.tree import build_tree

if TYPE_CHECKING:
    from mf_cluster import Fabric

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

#: Chromium's console line for the signed-out boot probe (see test_v08_g2_shell).
BOOT_PROBE_401 = ("status of 401", "/v1/session")
JOB_URL = re.compile(r"/jobs/(job_[0-9A-Z]{26})$")


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    """The agent runs jobs (subprocess runtime, no Docker), dispatch is quick."""
    return {
        "agent_execution": {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0},
        "controller_jobs": {"dispatch_interval_s": 0.1},
        "agent_env": {"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    }


def _page(browser: Browser) -> tuple[Page, PageWatch]:
    context = browser.new_context(reduced_motion="reduce", accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(20_000)
    watch = PageWatch.attach(page)
    watch.allow(*BOOT_PROBE_401)
    return page, watch


def _login(page: Page, fabric: Fabric, user: str = NORMAL_USER) -> None:
    page.goto(fabric.http_base + "/")
    page.get_by_label("User name").fill(user)
    page.get_by_label("Password").fill(fabric.password(user))
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", level=1, name="Overview")).to_be_visible()


def _axe_clean(page: Page, where: str) -> None:
    violations = axe_violations(page)
    assert violations == [], f"axe violations on {where}: {violations}"


def _project(root: Path, *, lines: int, pause_s: float) -> Path:
    project = root / "browser-proj"
    (project / "data").mkdir(parents=True)
    (project / "main.py").write_text(
        textwrap.dedent(
            f"""\
            import os, time
            os.makedirs("results", exist_ok=True)
            for i in range({lines}):
                print(f"step {{i}} of {lines}", flush=True)
                time.sleep({pause_s})
            with open("results/answer.txt", "w") as fh:
                fh.write(open("data/input.txt").read().upper())
            print("done", flush=True)
            """
        ),
        encoding="utf-8",
    )
    (project / "data" / "input.txt").write_text("hello from the browser\n", "utf-8")
    (project / "compute.yaml").write_text(
        "entrypoint: python main.py\n"
        "env:\n  base: python-cpu\n"
        "resources:\n  gpu: none\n  cpus: 1\n"
        "outputs: [results/]\n"
        "limits:\n  timeout: 5m\n",
        "utf-8",
    )
    # Ignored by the shared rules: never uploaded.
    (project / "debug.log").write_text("noise", "utf-8")
    (project / ".gitignore").write_text("*.log\n", "utf-8")
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "x.pyc").write_bytes(b"x")
    return project


def _job(fabric: Fabric, job_id: str) -> dict[str, Any]:
    resp = fabric.call("GET", f"/v1/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _wait_state(
    fabric: Fabric, job_id: str, states: set[str], timeout_s: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    job = _job(fabric, job_id)
    while job["state"] not in states and time.monotonic() < deadline:
        time.sleep(0.25)
        job = _job(fabric, job_id)
    assert job["state"] in states, job
    return job


def _submit_folder(page: Page, fabric: Fabric, project: Path) -> str:
    page.goto(fabric.http_base + "/jobs/new")
    expect(page.get_by_role("heading", level=1, name="Submit a job")).to_be_visible()
    page.get_by_label("Project folder").set_input_files(project)
    summary = page.get_by_label("Project summary")
    expect(summary).to_contain_text("browser-proj")
    page.get_by_role("button", name="Upload and submit").click()
    expect(page).to_have_url(JOB_URL)
    match = JOB_URL.search(page.url)
    assert match is not None
    return match.group(1)


@pytest.mark.docker
def test_submit_a_folder_run_follow_logs_download_outputs(
    fabric: Fabric, dashboard_browser: Browser, tmp_path: Path
) -> None:
    project = _project(tmp_path, lines=5, pause_s=0.3)
    page, watch = _page(dashboard_browser)
    _login(page, fabric)

    page.goto(fabric.http_base + "/jobs/new")
    expect(page.get_by_role("heading", level=1, name="Submit a job")).to_be_visible()
    _axe_clean(page, "the submit page")
    page.get_by_label("Project folder").set_input_files(project)
    summary = page.get_by_label("Project summary")
    expect(summary).to_contain_text("compute.yaml")
    # main.py, data/input.txt, compute.yaml, .gitignore; debug.log + __pycache__ ignored
    expect(summary).to_contain_text("4 files")
    expect(summary).to_contain_text("(2 ignored)")
    tree_text = summary.locator("dd").nth(5).inner_text()

    # One spec, two implementations: the CLI's own tree of the same folder.
    cli_tree = build_tree(project)
    assert tree_text == str(cli_tree.tree.tree_id)

    page.get_by_role("button", name="Upload and submit").click()
    expect(page).to_have_url(JOB_URL)
    match = JOB_URL.search(page.url)
    assert match is not None
    job_id = match.group(1)

    # The log streams in while the job runs, then the job ends succeeded.
    log = page.get_by_role("log", name="Job output")
    expect(log).to_contain_text("step 0 of 5")
    expect(log).to_contain_text("done", timeout=60_000)
    expect(page.get_by_test_id("log-end")).to_contain_text(
        "Job succeeded", timeout=30_000
    )
    job = _wait_state(fabric, job_id, {"succeeded"}, 30)
    assert job["tree_id"] == str(cli_tree.tree.tree_id)
    assert job["manifest"]["name"] == "browser-proj"
    region = page.get_by_role("region", name="Summary")
    expect(region).to_contain_text("succeeded")
    _axe_clean(page, "the job page")

    # Outputs: the browser download IS the verified archive the API names.
    page.get_by_role("tab", name="Outputs").click()
    (attempt,) = job["attempts"]
    digest = attempt["output_digest"]
    assert digest and digest.startswith("sha256:")
    expect(page.get_by_text(digest)).to_be_visible()
    with page.expect_download() as info:
        page.get_by_role("link", name="Download outputs").click()
    saved = info.value.path()
    assert hashlib.sha256(Path(saved).read_bytes()).hexdigest() == digest.removeprefix(
        "sha256:"
    )

    # The plan explanation is the API's decision table.
    page.get_by_role("tab", name="Plan").click()
    decisions = fabric.call("GET", f"/v1/jobs/{job_id}/decisions").json()["decisions"]
    assert decisions
    chosen = decisions[-1]["chosen_node_id"]
    expect(
        page.get_by_role("table", name=re.compile("Candidates of decision"))
    ).to_contain_text(chosen)
    _axe_clean(page, "the plan tab")

    # Resubmitting the unchanged folder uploads nothing new (have/need): the
    # job reuses the same tree.
    second = _submit_folder(page, fabric, project)
    assert _job(fabric, second)["tree_id"] == job["tree_id"]
    _wait_state(fabric, second, {"succeeded"}, 60)
    watch.assert_clean()


@pytest.mark.docker
def test_keyboard_only_submit_and_cancel_a_running_job(
    fabric: Fabric, dashboard_browser: Browser, tmp_path: Path
) -> None:
    project = _project(
        tmp_path, lines=600, pause_s=0.1
    )  # ~60 s: still running when cancelled
    page, watch = _page(dashboard_browser)
    _login(page, fabric)
    page.goto(fabric.http_base + "/jobs/new")
    expect(page.get_by_role("heading", level=1, name="Submit a job")).to_be_visible()

    # Keyboard: Tab to the folder picker, Space opens the chooser.
    folder = page.get_by_label("Project folder")
    for _ in range(40):
        if folder.evaluate("el => el === document.activeElement"):
            break
        page.keyboard.press("Tab")
    expect(folder).to_be_focused()
    with page.expect_file_chooser() as chooser:
        page.keyboard.press("Space")
    chooser.value.set_files(project)
    expect(page.get_by_label("Project summary")).to_contain_text("browser-proj")
    submit = page.get_by_role("button", name="Upload and submit")
    for _ in range(40):
        if submit.evaluate("el => el === document.activeElement"):
            break
        page.keyboard.press("Tab")
    expect(submit).to_be_focused()
    page.keyboard.press("Enter")
    expect(page).to_have_url(JOB_URL)
    match = JOB_URL.search(page.url)
    assert match is not None
    job_id = match.group(1)
    expect(page.get_by_role("log", name="Job output")).to_contain_text(
        "step 1 of 600", timeout=60_000
    )

    # Keyboard: Tab to "Cancel job", Enter; in the dialog Tab to the confirm, Enter.
    cancel = page.get_by_role("button", name="Cancel job")
    for _ in range(40):
        if cancel.evaluate("el => el === document.activeElement"):
            break
        page.keyboard.press("Tab")
    expect(cancel).to_be_focused()
    page.keyboard.press("Enter")
    dialog = page.get_by_role("dialog", name="Cancel this job?")
    expect(dialog).to_be_visible()
    confirm = dialog.get_by_role("button", name="Cancel job")
    for _ in range(5):
        if confirm.evaluate("el => el === document.activeElement"):
            break
        page.keyboard.press("Tab")
    expect(confirm).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_text("Cancel requested")).to_be_visible()

    job = _wait_state(fabric, job_id, {"cancelled"}, 60)
    assert job["state"] == "cancelled"
    expect(page.get_by_role("region", name="Summary")).to_contain_text(
        "cancelled", timeout=30_000
    )
    expect(page.get_by_role("button", name="Cancel job")).to_be_disabled()
    _axe_clean(page, "a cancelled job")
    watch.assert_clean()


def _tiny_tree(fabric: Fabric, root: Path) -> str:
    project = root / "tiny"
    project.mkdir()
    (project / "main.py").write_text("print('x')\n", "utf-8")
    built = build_tree(project)
    for hex_, path in built.sources.items():
        resp = fabric.call("PUT", f"/v1/blobs/{hex_}", content=path.read_bytes())
        assert resp.status_code in (200, 201), resp.text
    resp = fabric.call(
        "PUT", f"/v1/blobs/{built.tree.tree_id.hex}", content=built.manifest
    )
    assert resp.status_code in (200, 201), resp.text
    return str(built.tree.tree_id)


@pytest.mark.docker
def test_list_pages_through_120_jobs(
    fabric: Fabric, dashboard_browser: Browser, tmp_path: Path
) -> None:
    tree_id = _tiny_tree(fabric, tmp_path)
    created: list[str] = []
    for i in range(120):
        body = {
            "manifest": {"name": f"bulk-{i:03d}", "entrypoint": ["python", "main.py"]},
            "tree_id": tree_id,
        }
        resp = fabric.call("POST", "/v1/jobs", json=body)
        assert resp.status_code == 201, resp.text
        job_id = resp.json()["job_id"]
        created.append(job_id)
        fabric.call("POST", f"/v1/jobs/{job_id}/cancel")
    newest = created[-1]

    page, watch = _page(dashboard_browser)
    _login(page, fabric, ADMIN_USER)
    page.goto(fabric.http_base + "/jobs")
    table = page.get_by_role("table", name="Jobs, newest first")
    expect(table.get_by_role("link", name="bulk-119")).to_be_visible()
    status = page.get_by_role("status").filter(has_text="loaded")
    expect(status).to_have_text(re.compile(r"^50 jobs loaded; more on the server"))
    _axe_clean(page, "the jobs list")

    page.get_by_role("button", name="Load more").click()
    expect(status).to_have_text(re.compile(r"^100 jobs loaded; more on the server"))
    page.get_by_role("button", name="Load more").click()
    expect(status).to_have_text(re.compile(r"^120 jobs loaded; that is all"))
    # Every job the API created is listed exactly once, newest first.
    api = fabric.call("GET", "/v1/jobs", params={"limit": 200}).json()["jobs"]
    assert api[0]["job_id"] == newest
    expect(table.get_by_role("link", name="bulk-000")).to_have_count(1)

    # A filter goes to the server and into the URL; a row opens its job.
    page.get_by_label("State").select_option("cancelled")
    expect(page).to_have_url(re.compile(r"/jobs\?state=cancelled$"))
    expect(table.get_by_role("link", name="bulk-119")).to_be_visible()
    table.get_by_role("link", name="bulk-119").click()
    expect(page).to_have_url(re.compile(f"/jobs/{newest}$"))
    expect(page.get_by_role("heading", level=1, name="bulk-119")).to_be_visible()
    watch.assert_clean()
