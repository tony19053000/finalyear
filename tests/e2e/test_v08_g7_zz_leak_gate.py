"""V0.8 G7 (checklist 25): no credential reaches the browser beyond the
HttpOnly session cookie - one gate over the whole e2e run.

The session-wide ``leak_watch`` (tests/e2e/conftest.py,
tests/_support/mf_leaks.py) records, for every page every e2e test opens:
each console message, each URL requested or visited (WebSockets too), each
storage write as it happens and the storage the page kept (localStorage,
sessionStorage, IndexedDB, Cache Storage, history state, script-readable
cookies); it learns every password, API token, join code and secret value
from the traffic. Its verdict does not depend on this file (fix round 1, T-1):
the fixture's session teardown and ``pytest_sessionfinish`` fail the run on any
finding, whichever tests ran, in whatever order. This test adds one flow that
makes each kind of credential in the browser (sign in, API token, invite code,
secret), visits every page, signs out - and asserts that no place recorded so
far holds a known credential or a credential shape, and that the built bundle
holds none either (the bundle grep needs the dist, so it lives here). The
``zz`` in the name only makes it judge (and print) most of the run's evidence
when the whole tier runs in file order.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import ADMIN_USER
from mf_e2e import EXEC_OPTIONS, put_tree, sign_in, submit_job, tab_to
from mf_leaks import SHAPES, LeakWatch
from playwright.sync_api import expect

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.security,
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

#: Text files of the built SPA a credential could be compiled into.
BUNDLE_SUFFIXES = frozenset({".js", ".mjs", ".css", ".html", ".json", ".map", ".svg"})


def _make_api_token(page: Page, fabric: Fabric) -> str:
    page.goto(fabric.http_base + "/settings/tokens")
    page.get_by_role("button", name="Create API token").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Token name").fill("leak-gate")
    dialog.get_by_label("Your password").fill(fabric.password(ADMIN_USER))
    dialog.get_by_role("button", name="Create token").click()
    shown = dialog.get_by_test_id("new-token")
    expect(shown).to_be_visible()
    token = shown.locator("pre").first.inner_text().strip()
    shown.get_by_role("button", name="Done").click()
    expect(dialog).to_be_hidden()
    return token


def _make_invite(page: Page, fabric: Fabric) -> str:
    page.goto(fabric.http_base + "/settings/invites")
    page.get_by_role("button", name="Create invite").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_role("button", name="Create invite").click()
    issued = dialog.get_by_test_id("issued-invite")
    expect(issued).to_be_visible()
    code = issued.locator("pre").first.inner_text().strip()
    issued.get_by_role("button", name="Done").click()
    expect(dialog).to_be_hidden()
    return code


def _set_secret(page: Page, fabric: Fabric, value: str) -> None:
    page.goto(fabric.http_base + "/settings/secrets")
    page.get_by_role("button", name="Set a secret").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Name", exact=True).fill("leak_gate")
    dialog.get_by_label("Value", exact=True).fill(value)
    dialog.get_by_role("button", name="Store secret").click()
    expect(page.get_by_text("Secret LEAK_GATE stored", exact=True)).to_be_visible()


def _bundle_text(dist: Path) -> list[tuple[str, str]]:
    return [
        (f"bundle {p.relative_to(dist)}", p.read_text("utf-8", errors="replace"))
        for p in sorted(dist.rglob("*"))
        if p.is_file() and p.suffix in BUNDLE_SUFFIXES
    ]


@pytest.mark.parametrize("fabric_options", [{**EXEC_OPTIONS, "agents": 1}])
def test_no_credential_reaches_browser_storage_urls_console_or_bundle(
    fabric: Fabric,
    new_page: NewPage,
    leak_watch: LeakWatch,
    dashboard_dist: Path,
    tmp_path: Path,
) -> None:
    pages_before = leak_watch.pages
    secret_value = "mf-gate-" + secrets.token_hex(16)
    page, _watch = new_page()
    sign_in(page, fabric)
    token = _make_api_token(page, fabric)
    code = _make_invite(page, fabric)
    _set_secret(page, fabric, secret_value)

    # Every page of the dashboard, one job and one node included.
    project = tmp_path / "gate"
    project.mkdir()
    (project / "main.py").write_text("print('gate')\n", "utf-8")
    job_id = submit_job(fabric, put_tree(fabric, project), "leak-gate")
    node_id = fabric.nodes[0].node_id
    for path in (
        "/",
        "/nodes",
        f"/nodes/{node_id}",
        "/jobs",
        "/jobs/new",
        f"/jobs/{job_id}",
        "/settings",
        "/settings/tokens",
        "/settings/secrets",
        "/settings/data",
        "/settings/invites",
        "/settings/users",
    ):
        page.goto(fabric.http_base + path)
        expect(page.get_by_role("heading", level=1).first).to_be_visible()
    tab_to(page, "Sign out", limit=60)
    page.keyboard.press("Enter")
    expect(page.get_by_text("You are signed out.")).to_be_visible()

    # The watch learnt each credential from the traffic itself.
    assert {token, code, secret_value, fabric.password(ADMIN_USER)} <= leak_watch.known
    # ... and its shapes match the real formats.
    assert SHAPES[0].fullmatch(token), token[:5]
    assert SHAPES[2].fullmatch(code), code[:4]

    # Self-check: the same judging finds a planted copy in each kind of place.
    probe = LeakWatch(known={token, code, secret_value})
    for where, text in (
        ("localStorage", f'[["k","{token}"]]'),
        ("request URL", f"https://x/v1/jobs?c={code.replace('-', '%2D')}"),
        ("console.log", f"value {secret_value}"),
        ("IndexedDB", '{"x":"mfbs_' + "A" * 43 + '"}'),
    ):
        probe.places.append((where, text))
    assert len(probe.findings()) >= 4, probe.findings()

    # The gate: the whole run's evidence, and the built bundle.
    snapshot_page, _ = new_page()  # an unused page: storage after sign-out
    snapshot_page.goto(fabric.http_base + "/")
    leak_watch.snapshot(snapshot_page, "leak gate (fresh page)")
    leak_watch.snapshot(page, "leak gate (signed out)")
    findings = leak_watch.findings(extra=_bundle_text(dashboard_dist))
    assert not findings, (
        f"credentials reached the browser ({len(findings)} places):\n  "
        + "\n  ".join(findings[:50])
    )
    assert leak_watch.pages > pages_before
    print(  # noqa: T201 - the gate's coverage, for the hand-off
        f"\nleak gate: {leak_watch.pages} pages, {leak_watch.snapshots} storage "
        f"snapshots, {len(leak_watch.places)} places, {len(leak_watch.known)} "
        "known credentials checked"
    )
