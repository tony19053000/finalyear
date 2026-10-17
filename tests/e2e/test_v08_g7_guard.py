"""V0.8 G7: the shared end-of-test page guard really catches what it promises.

``new_page`` (tests/e2e/conftest.py) checks every page a test opened with
``PageWatch.final_problems`` when the test passed. This test makes one page
dirty in each way the guard covers (console error, uncaught error, CSP
violation, axe violation on the final state) and checks each is reported -
and that ``allow`` / ``allow_matching`` / ``skip_final_axe`` / ``disarm``
remove exactly what they say. Every dirty page is disarmed at the end, so
the guard itself then passes this test.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from playwright.sync_api import expect

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_the_end_of_test_guard_reports_each_kind_of_problem(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = new_page()
    page.goto(fabric.http_base + "/")
    expect(page.get_by_label("User name")).to_be_visible()
    assert watch.final_problems() == []  # the sign-in page is clean

    page.evaluate("console.error('g7 guard probe: app error')")
    page.evaluate("setTimeout(() => { throw new Error('g7 guard probe: uncaught'); })")
    # A CSP violation: an inline <style> the policy refuses.
    page.evaluate(
        "document.head.appendChild(Object.assign(document.createElement('style'),"
        " {textContent: 'body{}'}))"
    )
    # An axe violation on the final state: an image with no text alternative.
    page.evaluate(
        "document.body.appendChild(Object.assign(document.createElement('img'),"
        " {src: '/favicon.svg'}))"
    )
    page.wait_for_function("(window.__mfCsp || []).length > 0")
    for _ in range(50):  # the uncaught error arrives on a later task
        if watch.page_errors:
            break
        page.wait_for_timeout(100)

    problems = watch.final_problems()
    assert any(p.startswith("console error:") and "app error" in p for p in problems), (
        problems
    )
    assert any(
        p.startswith("uncaught page error:") and "uncaught" in p for p in problems
    ), problems
    assert any(p.startswith("CSP violation on") for p in problems), problems
    assert any(
        p.startswith("axe violation on") and "image-alt" in p for p in problems
    ), problems

    # Each escape hatch removes exactly its own kind.
    watch.allow_matching(r"g7 guard probe: app error", "the probe's own console.error")
    left = watch.final_problems()
    assert not any("app error" in p for p in left), left
    # (Chromium also logs the refused <style> as a console error: still there.)
    assert any(p.startswith("console error:") and "style" in p for p in left), left
    watch.skip_final_axe("probe: the page holds an injected unlabeled image")
    assert not any(p.startswith("axe violation") for p in watch.final_problems())
    assert any(p.startswith("uncaught page error:") for p in watch.final_problems())
    watch.disarm("negative control: this page is dirty on purpose")
    assert watch.final_problems() == []

    # A second page: an `allow` (text + resource suffix) is judged as each
    # line arrives, so it is declared before the line. Signed out, both reads
    # below are refused (401); only the allowed one is not reported.
    other, other_watch = new_page()
    other_watch.allow(
        "status of 401",
        "/v1/jobs/job_00000000000000000000000000",
        "the signed-out read below, on purpose",
    )
    other.goto(fabric.http_base + "/")
    expect(other.get_by_label("User name")).to_be_visible()
    for job in ("job_00000000000000000000000000", "job_11111111111111111111111111"):
        status = other.evaluate(f"fetch('/v1/jobs/{job}').then((r) => r.status)")
        assert status == 401, (job, status)
    for _ in range(50):
        if other_watch.console_errors:
            break
        other.wait_for_timeout(100)
    reported = other_watch.final_problems()
    assert len(reported) == 1, reported
    assert "job_11111111111111111111111111" in reported[0], reported
    other_watch.disarm("negative control: a refused read on purpose")
