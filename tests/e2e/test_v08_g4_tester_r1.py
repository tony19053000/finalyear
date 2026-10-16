# ruff: noqa: E501
"""V0.8 G4 tester r1: job-op authorization and CSRF through a real controller + real Chromium.

Criteria: D176 job ops (cancel) obey the job API's owner rules (another user's job is 404, an
admin sees all) and the browser session's CSRF rule (a mutating call without the header is 403);
the pages show those refusals honestly, never a crash or another user's data.
"""

from __future__ import annotations

import re
import sys
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
from mf_e2e import EXEC_OPTIONS, job_state, put_tree, sign_in, submit_job
from playwright.sync_api import expect

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

CSRF_COOKIE = "mf_csrf"
CSRF_HEADER = "x-meshforge-csrf"


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    return dict(EXEC_OPTIONS)


def _tree(fabric: Fabric, root: Path) -> str:
    project = root / "tiny"
    project.mkdir()
    (project / "main.py").write_text("import time\ntime.sleep(120)\n", "utf-8")
    return put_tree(fabric, project)


# One agent, as when this test was written (the harness ran one before G7):
# the admin's job holds it, so alice's job is still queued when the admin
# cancels it, and a queued job is cancelled at once - which the test asserts.
# With 2 agents alice's job is already `preparing` there and cancel is async.
@pytest.mark.parametrize("fabric_options", [{**EXEC_OPTIONS, "agents": 1}])
def test_cancel_authz_user_vs_admin_v08_g4_tester_r1(
    fabric: Fabric, tmp_path: Path
) -> None:
    tree = _tree(fabric, tmp_path)
    admins = submit_job(fabric, tree, "admins-job", ADMIN_USER)
    alices = submit_job(fabric, tree, "alices-job", NORMAL_USER)
    # alice cannot see / cancel the admin's job: 404 like a missing one, job untouched.
    assert (
        fabric.call("GET", f"/v1/jobs/{admins}", as_user=NORMAL_USER).status_code == 404
    )
    assert (
        fabric.call(
            "POST", f"/v1/jobs/{admins}/cancel", as_user=NORMAL_USER
        ).status_code
        == 404
    )
    assert (
        fabric.call(
            "GET", f"/v1/jobs/{admins}/decisions", as_user=NORMAL_USER
        ).status_code
        == 404
    )
    assert job_state(fabric, admins) not in {"cancelled"}
    ids = {
        j["job_id"]
        for j in fabric.call("GET", "/v1/jobs", as_user=NORMAL_USER).json()["jobs"]
    }
    assert alices in ids and admins not in ids
    # the admin may cancel alice's; cancel is idempotent (second call 200, state unchanged)
    first = fabric.call("POST", f"/v1/jobs/{alices}/cancel")
    assert first.status_code in (200, 202), first.text
    again = fabric.call("POST", f"/v1/jobs/{alices}/cancel")
    assert again.status_code in (200, 202)
    assert job_state(fabric, alices) == "cancelled"
    # cancel of an unknown / malformed id
    assert (
        fabric.call(
            "POST", "/v1/jobs/job_00000000000000000000000000/cancel"
        ).status_code
        == 404
    )
    assert fabric.call("POST", "/v1/jobs/not-a-job/cancel").status_code in (400, 404)


def test_cancel_without_csrf_header_is_refused_v08_g4_tester_r1(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    tree = _tree(fabric, tmp_path)
    job = submit_job(fabric, tree, "csrf-job", NORMAL_USER)
    page, watch = new_page()
    # The two refused fetches below (this test's own probes) log a 403.
    watch.allow("status of 403", f"/v1/jobs/{job}/cancel", "the refused probes")
    sign_in(page, fabric, NORMAL_USER)
    path = f"/v1/jobs/{job}/cancel"
    send = """async ([path, headers]) => {
        const r = await fetch(path, {method: "POST", headers, credentials: "same-origin"});
        return r.status;
    }"""
    # Same-origin fetch carries the session cookie but we add no / a wrong CSRF header.
    assert page.evaluate(send, [path, {}]) == 403
    assert job_state(fabric, job) != "cancelled"
    assert page.evaluate(send, [path, {CSRF_HEADER: "0" * 43}]) == 403
    assert job_state(fabric, job) != "cancelled"
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    assert CSRF_COOKIE in cookies
    assert page.evaluate(send, [path, {CSRF_HEADER: cookies[CSRF_COOKIE]}]) in (
        200,
        202,
    )
    deadline = time.monotonic() + 60
    while job_state(fabric, job) != "cancelled" and time.monotonic() < deadline:
        time.sleep(0.3)
    assert job_state(fabric, job) == "cancelled"


def test_foreign_job_page_is_an_honest_not_found_v08_g4_tester_r1(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    tree = _tree(fabric, tmp_path)
    admins = submit_job(fabric, tree, "secret-admin-job", ADMIN_USER)
    page, watch = new_page()
    watch.allow("status of 404", f"/v1/jobs/{admins}", "another user's job")
    # The garbage id below is read too and answered 404 (Chromium logs it;
    # whether before or after the test ends is timing - so always allowed).
    garbage = "%3Cscript%3Ealert(1)%3C%2Fscript%3E"
    watch.allow(
        "status of 404",
        f"/v1/jobs/{garbage}",
        "the garbage id (logged before or after the test ends)",
        may_not_occur=True,
    )
    sign_in(page, fabric, NORMAL_USER)
    page.goto(f"{fabric.http_base}/jobs/{admins}")
    expect(page.get_by_text("secret-admin-job")).to_have_count(0)
    expect(page.get_by_role("button", name="Cancel job")).to_have_count(0)
    # some visible not-found message, no blank page
    expect(page.locator("main")).to_contain_text(
        re.compile("not found|no such|does not exist", re.I)
    )
    page.goto(f"{fabric.http_base}/jobs")
    expect(page.get_by_text("secret-admin-job")).to_have_count(0)
    # a garbage id in the URL does not crash the page
    page.goto(f"{fabric.http_base}/jobs/{garbage}")
    expect(page.locator("main")).to_be_visible()


# --------------------------------------------------------------- real browser UX

import hashlib  # noqa: E402
import textwrap  # noqa: E402

from mf_dashboard import axe_violations  # noqa: E402


def _chatty_project(root: Path, lines: int, pause_s: float) -> Path:
    project = root / "chatty"
    project.mkdir()
    (project / "main.py").write_text(
        textwrap.dedent(
            f"""\
            import time
            for i in range({lines}):
                print(f"step {{i}} of {lines}", flush=True)
                time.sleep({pause_s})
            print("done", flush=True)
            """
        ),
        "utf-8",
    )
    (project / "compute.yaml").write_text(
        "entrypoint: python main.py\nenv:\n  base: python-cpu\nresources:\n  gpu: none\n  cpus: 1\nlimits:\n  timeout: 5m\n",
        "utf-8",
    )
    return project


def _submit(page: Page, fabric: Fabric, project: Path) -> str:
    page.goto(fabric.http_base + "/jobs/new")
    page.get_by_label("Project folder").set_input_files(project)
    expect(page.get_by_label("Project summary")).to_contain_text(project.name)
    page.get_by_role("button", name="Upload and submit").click()
    expect(page).to_have_url(re.compile(r"/jobs/job_[0-9A-Z]{26}$"))
    return page.url.rsplit("/", 1)[-1]


@pytest.mark.docker
def test_log_follower_reconnects_without_gaps_or_duplicates_v08_g4_tester_r1(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    """Drop the live log WebSocket twice mid-job (real Chromium, the real controller behind it): the
    page reconnects from its byte offsets, so every line appears exactly once, in order."""
    project = _chatty_project(tmp_path, lines=60, pause_s=0.25)
    page, watch = new_page()
    # Chromium may log the socket this test drops (how, and whether, is timing).
    watch.allow("WebSocket", "logs/ws", "the dropped log socket", may_not_occur=True)
    watch.allow("ERR_", "logs/ws", "the dropped log socket", may_not_occur=True)
    sign_in(page, fabric, NORMAL_USER)
    opened: list[int] = []

    def handler(ws: Any) -> None:
        server = ws.connect_to_server()
        opened.append(1)
        mine = len(opened)
        seen = [0]

        def from_server(message: Any) -> None:
            seen[0] += 1
            if mine <= 2 and seen[0] > 6:
                ws.close(code=1011, reason="chaos")  # cut this connection mid-job
                return
            ws.send(message)

        server.on_message(from_server)
        ws.on_message(lambda message: server.send(message))
        ws.on_close(lambda code, reason: server.close(code=code, reason=reason))

    page.route_web_socket(re.compile(r".*/logs/ws.*"), handler)
    _submit(page, fabric, project)
    log = page.get_by_role("log", name="Job output")
    expect(log).to_contain_text("done", timeout=90_000)
    text = log.inner_text()
    steps = re.findall(r"step (\d+) of 60", text)
    assert steps == [str(i) for i in range(60)], steps
    assert len(opened) >= 3, f"the page never reconnected ({len(opened)} sockets)"


@pytest.mark.docker
def test_cancel_dialog_escape_and_axe_v08_g4_tester_r1(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    project = _chatty_project(tmp_path, lines=400, pause_s=0.2)
    page, watch = new_page()
    sign_in(page, fabric, NORMAL_USER)
    job_id = _submit(page, fabric, project)
    expect(page.get_by_role("log", name="Job output")).to_contain_text(
        "step 1 of", timeout=60_000
    )
    page.get_by_role("button", name="Cancel job").click()
    dialog = page.get_by_role("dialog", name="Cancel this job?")
    expect(dialog).to_be_visible()
    violations = axe_violations(page)
    assert violations == [], f"axe violations with the cancel dialog open: {violations}"
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)
    # focus returns to the button that opened it (keyboard users keep their place)
    expect(page.get_by_role("button", name="Cancel job")).to_be_focused()
    assert job_state(fabric, job_id) not in {"cancelled"}
    # cancel through the page, then retry-cancel via the API: idempotent, terminal
    page.get_by_role("button", name="Cancel job").click()
    page.get_by_role("dialog").get_by_role("button", name="Cancel job").click()
    deadline = time.monotonic() + 60
    while job_state(fabric, job_id) != "cancelled" and time.monotonic() < deadline:
        time.sleep(0.3)
    assert job_state(fabric, job_id) == "cancelled"
    assert (
        fabric.call(
            "POST", f"/v1/jobs/{job_id}/cancel", as_user=NORMAL_USER
        ).status_code
        == 200
    )
    _ = (hashlib, watch)


def test_axe_on_empty_list_missing_job_and_submit_errors_v08_g4_tester_r1(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, watch = new_page()
    # The missing job's read is a 404 (Chromium logs it). Before G7 this
    # allowance named the suffix "/v1/jobs/", which no URL ends with, and the
    # test never checked the watch; the shared guard now does.
    watch.allow(
        "status of 404",
        "/v1/jobs/job_00000000000000000000000000",
        "the missing job's read",
    )
    sign_in(page, fabric, NORMAL_USER)
    page.goto(fabric.http_base + "/jobs")
    expect(page.get_by_role("heading", name="No jobs yet")).to_be_visible()
    assert axe_violations(page) == []
    page.goto(fabric.http_base + "/jobs/job_00000000000000000000000000")
    expect(page.get_by_role("heading", name="No such job")).to_be_visible()
    assert axe_violations(page) == []
    # an empty folder pick does not crash the page
    empty = tmp_path / "empty"
    empty.mkdir()
    page.goto(fabric.http_base + "/jobs/new")
    page.get_by_label("Project folder").set_input_files(empty)
    expect(page.get_by_role("heading", level=1, name="Submit a job")).to_be_visible()


# T-16 fixed in G4 fix round 1 (status-failed tokens: >= 4.5:1 on the soft tone over every surface).
def test_axe_on_the_refused_project_alert_v08_g4_tester_r1(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    page, _watch = new_page()
    sign_in(page, fabric, NORMAL_USER)
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "readme.txt").write_text("x", "utf-8")
    page.goto(fabric.http_base + "/jobs/new")
    page.get_by_label("Project folder").set_input_files(notes)
    expect(page.get_by_role("alert")).to_contain_text("Cannot submit this project")
    assert axe_violations(page) == []
