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
from mf_e2e import EXEC_OPTIONS, sign_in
from playwright.sync_api import expect

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]


@pytest.fixture
def fabric_options() -> dict[str, Any]:
    return dict(EXEC_OPTIONS)


def _project(root: Path) -> Path:
    project = root / "prio-proj"
    project.mkdir()
    (project / "main.py").write_text("print('hi')\n", "utf-8")
    return project


@pytest.mark.parametrize(
    "typed",
    [
        "+5",  # valid: submitted as 5
        "5.",  # not a whole-number text: blocked
        "5e",
        "-",
        "1e400",
        "--5",
    ],
)
def test_unparseable_priority_text_never_silently_becomes_the_default_v08_g4_tester_r2(
    fabric: Fabric, new_page: NewPage, tmp_path: Path, typed: str
) -> None:
    """A browser number input hands the page "" for text it cannot parse (validity.badInput).

    The page must then either block the submit or tell the user; it must not submit a job whose
    priority silently differs from what the field shows.
    """
    page, _watch = new_page()
    sign_in(page, fabric, NORMAL_USER)
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
    # D197: the field is type=text now, so the page sees exactly what the user typed.
    assert shown["bad"] is False and shown["v"] == typed
    blocked = button.is_disabled() or field.get_attribute("aria-invalid") == "true"
    if not blocked:
        button.click()
        page.wait_for_timeout(1500)
    assert blocked or not any("priority" not in p.get("manifest", {}) for p in posts), (
        f"{typed!r} is shown in the field (badInput) but the job was submitted WITHOUT it: {posts}"
    )
