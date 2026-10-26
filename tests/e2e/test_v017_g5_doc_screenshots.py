"""V0.17 G5 (0.17.7, Q41): the user pages' Linux/console screenshots, taken
from the real UIs in Chromium while walking the path the pages describe.

A real controller (cluster harness, one agent) and a real ``meshforge-agent
run`` with its local console, not yet joined:

* console: welcome -> join step -> join with an invite code -> "what this
  laptop shares" -> checks -> home -> settings;
* dashboard: Nodes (the agent's version column), Settings > Invites with a new
  invite (code + QR), Jobs > Submit job.

Each step is asserted as the docs describe it; with
``MESHFORGE_DOCS_SCREENSHOTS=docs/user/img`` the screenshots are (re)written
there (light theme, desktop width). Windows installer screenshots are the 🧑
lab's (``TODO-SCREENSHOT`` placeholders in the pages).
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")
pytest.importorskip("fastapi", reason="the console needs the agent extra")

from mf_cluster import ADMIN_USER
from mf_e2e import doc_screenshot, sign_in
from playwright.sync_api import expect

from meshforge.agent.console.launcher import launch_url

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

CONSOLE_DIST = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "meshforge"
    / "agent"
    / "console"
    / "dist"
)
VIEWPORT = {"width": 1100, "height": 760}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _light(page: object) -> None:
    from playwright.sync_api import Page

    assert isinstance(page, Page)
    if page.evaluate("document.documentElement.dataset.theme") != "light":
        page.get_by_role("button", name="Switch to light theme").click()


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_console_and_dashboard_screens_of_the_user_docs(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    if not (CONSOLE_DIST / "index.html").is_file():
        pytest.skip("the console UI is not built - run `cd frontend && npm run build`")
    port = _free_port()
    token_dir = tmp_path / "console"
    config = tmp_path / "agent.toml"
    config.write_text(
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n'
        "[agent]\n"
        f'controller_address = "{fabric.grpc_target}"\n'
        "[agent.console]\n"
        f"enabled = true\nport = {port}\n"
        f'token_dir = "{token_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    log_file = (tmp_path / "agent.log").open("w")
    agent = subprocess.Popen(
        [sys.executable, "-m", "meshforge.agent", "run", "--config", str(config)],
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 60
        while not (token_dir / "token").is_file():
            assert agent.poll() is None, (tmp_path / "agent.log").read_text()
            assert time.monotonic() < deadline, "the console never wrote its token"
            time.sleep(0.2)
        url = launch_url(token_dir / "token", port, wait_s=30)

        # ---------------------------------------------------- the console
        page, _ = new_page(viewport=VIEWPORT, color_scheme="light")
        page.goto(url)
        expect(page.get_by_role("heading", name="Welcome")).to_be_visible()
        _light(page)
        doc_screenshot(page, "console-welcome.png")
        page.get_by_role("button", name="Start").click()
        expect(page.get_by_label("Join code")).to_be_visible()
        doc_screenshot(page, "console-join.png")
        created = fabric.call("POST", "/v1/fabric/invites", json={})
        assert created.status_code == 201, created.text
        page.get_by_label("Join code").fill(created.json()["code"])
        page.get_by_role("button", name="Join", exact=True).click()
        expect(
            page.get_by_role("heading", name="What this laptop shares")
        ).to_be_visible(timeout=30_000)
        page.get_by_label("Name of this laptop").fill("Asha's laptop")
        doc_screenshot(page, "console-policy.png")
        page.get_by_role("button", name="Save and continue").click()
        expect(page.get_by_role("heading", name="Checks")).to_be_visible()
        expect(page.locator("[data-check='disk']")).to_be_visible(timeout=30_000)
        doc_screenshot(page, "console-checks.png")
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Finish").click()
        nav = page.get_by_role("navigation", name="Console")
        expect(page.get_by_text("Connected to the fabric")).to_be_visible(
            timeout=30_000
        )
        doc_screenshot(page, "console-home.png")
        nav.get_by_role("link", name="Settings").click()
        expect(
            page.get_by_role("checkbox", name="Run jobs from the fabric on this laptop")
        ).to_be_visible()
        doc_screenshot(page, "console-settings.png")

        # -------------------------------------------------- the dashboard
        dash, _ = new_page(viewport=VIEWPORT, color_scheme="light")
        sign_in(dash, fabric, ADMIN_USER)
        _light(dash)
        dash.goto(fabric.http_base + "/nodes")
        expect(dash.get_by_role("link", name="Asha's laptop")).to_be_visible(
            timeout=30_000
        )
        expect(dash.get_by_role("columnheader", name="Agent")).to_be_visible()
        doc_screenshot(dash, "dashboard-nodes.png")
        dash.goto(fabric.http_base + "/jobs/new")
        expect(dash.get_by_text("Project folder", exact=True)).to_be_visible()
        doc_screenshot(dash, "dashboard-submit.png")
    finally:
        agent.send_signal(signal.SIGTERM)
        try:
            code = agent.wait(60)
        finally:
            log_file.close()
        assert code == 0, (tmp_path / "agent.log").read_text()[-4000:]
