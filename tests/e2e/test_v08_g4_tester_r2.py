"""V0.8 G4 tester r2: real Chromium probes of the submit page's priority field (T-15 follow-up)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import NORMAL_USER
from mf_dashboard import PageWatch
from playwright.sync_api import Browser, Page, expect

if TYPE_CHECKING:
    from mf_cluster import Fabric

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    return {
        "agent_execution": {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0},
        "controller_jobs": {"dispatch_interval_s": 0.1},
        "agent_env": {"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    }


def _page(browser: Browser) -> tuple[Page, PageWatch]:
    context = browser.new_context(reduced_motion="reduce")
    page = context.new_page()
    page.set_default_timeout(20_000)
    watch = PageWatch.attach(page)
    watch.allow("status of 401", "/v1/session")
    return page, watch


def _login(page: Page, fabric: Fabric) -> None:
    page.goto(fabric.http_base + "/")
    page.get_by_label("User name").fill(NORMAL_USER)
    page.get_by_label("Password").fill(fabric.password(NORMAL_USER))
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", level=1, name="Overview")).to_be_visible()


def _project(root: Path) -> Path:
    project = root / "prio-proj"
    project.mkdir()
    (project / "main.py").write_text("print('hi')\n", "utf-8")
    return project


_T18 = pytest.mark.xfail(
    strict=True,
    reason="T-18: a number input with unparseable text reports value '' (validity.badInput); "
    "the page treats that as 'no priority' and submits without telling the user",
)


@pytest.mark.parametrize(
    "typed",
    [
        "+5",  # Chromium parses it as 5 (skipped as not a bad-input case)
        "5.",  # parsed as 5 (skipped)
        pytest.param("5e", marks=_T18),
        pytest.param("-", marks=_T18),
        pytest.param("1e400", marks=_T18),
        pytest.param("--5", marks=_T18),
    ],
)
def test_unparseable_priority_text_never_silently_becomes_the_default_v08_g4_tester_r2(
    fabric: Fabric, dashboard_browser: Browser, tmp_path: Path, typed: str
) -> None:
    """A browser number input hands the page "" for text it cannot parse (validity.badInput).

    The page must then either block the submit or tell the user; it must not submit a job whose
    priority silently differs from what the field shows.
    """
    page, _watch = _page(dashboard_browser)
    _login(page, fabric)
    posts: list[dict[str, Any]] = []
    page.on(
        "request",
        lambda r: (
            posts.append(json.loads(r.post_data or "{}"))
            if r.method == "POST" and r.url.endswith("/v1/jobs")
            else None
        ),
    )
    page.goto(fabric.http_base + "/jobs/new")
    page.get_by_label("Project folder").set_input_files(_project(tmp_path))
    expect(page.get_by_label("Project summary")).to_contain_text("prio-proj")
    field = page.get_by_label("Priority (-10 to 10)")
    field.click()
    field.press_sequentially(typed)
    shown = field.evaluate("el => ({v: el.value, bad: el.validity.badInput})")
    button = page.get_by_role("button", name="Upload and submit")
    if shown["bad"] is False:
        pytest.skip(
            f"Chromium parsed {typed!r} as {shown['v']!r}: not a bad-input case"
        )
    blocked = button.is_disabled() or field.get_attribute("aria-invalid") == "true"
    if not blocked:
        button.click()
        page.wait_for_timeout(1500)
    assert blocked or not any("priority" not in p.get("manifest", {}) for p in posts), (
        f"{typed!r} is shown in the field (badInput) but the job was submitted WITHOUT it: {posts}"
    )
