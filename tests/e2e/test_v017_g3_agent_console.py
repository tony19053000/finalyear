"""V0.17 G3 (0.17.4/0.17.5, D391/D392): the agent's local console in real Chromium.

A real controller (cluster harness) and a real ``meshforge-agent run`` process
with its console on, not yet joined. The laptop owner's whole path, at phone
width (375 px) and in both themes, through the built console UI:

* the launcher's one-time URL opens the wizard; the code leaves the address bar;
* step 2: an invite code from the controller (as the Invites page shows it) is
  pasted; a wrong code first gets a plain-language error;
* step 3 (cannot be skipped): the safe defaults are pre-set (jobs off, GPU off,
  50 % CPU); a node name is given; step 4 checks; step 5 done;
* the node is in the controller's ``GET /v1/nodes`` with its display name;
* Settings: turning jobs on says a restart is needed and "Restart now" does it;
  Logs shows the agent's own log; light and dark are axe-clean.
"""

from __future__ import annotations

import json
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

from mf_e2e import assert_axe_clean, storage_dump
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _agent_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["PYTHONUTF8"] = "1"
    return env


@pytest.mark.parametrize("fabric_options", [{"agents": 0}])
def test_owner_sets_up_the_laptop_in_the_console(
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
        f'token_dir = "{token_dir.as_posix()}"\n'
        "[agent.policy]\nreload_interval_s = 0.5\n",
        encoding="utf-8",
    )
    log_file = (tmp_path / "agent.log").open("w")
    agent = subprocess.Popen(
        [sys.executable, "-m", "meshforge.agent", "run", "--config", str(config)],
        env=_agent_env(),
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

        page, watch = new_page(viewport={"width": 375, "height": 812})
        page.goto(url)
        expect(page.get_by_role("heading", name="Welcome")).to_be_visible()
        assert "#" not in page.url  # the one-time code left the address bar
        assert_axe_clean(page, "the wizard welcome step (375 px, dark)")
        page.get_by_role("button", name="Start").click()

        # --- join: a wrong code first, then the controller's invite code
        code_box = page.get_by_label("Join code")
        watch.allow("400", "/console/v1/join", "the wrong code below is refused")
        code_box.fill("mf1-not-a-real-code")
        page.get_by_role("button", name="Join", exact=True).click()
        alert = page.get_by_role("alert")
        expect(alert).to_contain_text("This laptop did not join")
        expect(alert).to_contain_text("mf1-")
        created = fabric.call("POST", "/v1/fabric/invites", json={})
        assert created.status_code == 201, created.text
        code = created.json()["code"]
        code_box.fill(code)
        page.get_by_role("button", name="Join", exact=True).click()

        # --- what this laptop shares: safe defaults, cannot be skipped
        expect(
            page.get_by_role("heading", name="What this laptop shares")
        ).to_be_visible(timeout=30_000)
        assert page.get_by_role("button", name="Back").count() == 0
        expect(
            page.get_by_role("checkbox", name="Run jobs from the fabric on this laptop")
        ).not_to_be_checked()
        expect(
            page.get_by_role("checkbox", name="Let jobs use this laptop's GPU")
        ).not_to_be_checked()
        expect(page.get_by_label("Most CPU one job may use")).to_have_value("50")
        assert_axe_clean(page, "the wizard policy step")
        page.get_by_label("Name of this laptop").fill("Lab console")
        page.get_by_role("button", name="Save and continue").click()

        expect(page.get_by_role("heading", name="Checks")).to_be_visible()
        expect(page.locator("[data-check='disk']")).to_be_visible(timeout=30_000)
        assert_axe_clean(page, "the wizard checks step")
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Finish").click()

        # --- home: connected, and the controller knows the name
        nav = page.get_by_role("navigation", name="Console")
        expect(nav).to_be_visible(timeout=15_000)
        expect(page.get_by_text("Connected to the fabric")).to_be_visible(
            timeout=30_000
        )
        nodes = fabric.get("/v1/nodes")
        assert nodes is not None
        assert [n["display_name"] for n in nodes["nodes"]] == ["Lab console"]
        assert_axe_clean(page, "home (dark)")

        # --- settings: jobs on -> restart needed -> restart
        nav.get_by_role("link", name="Settings").click()
        page.get_by_role(
            "checkbox", name="Run jobs from the fabric on this laptop"
        ).check()
        page.get_by_role("button", name="Save settings").click()
        notice = page.get_by_text("One restart is needed")
        expect(notice).to_be_visible()
        page.get_by_role("button", name="Restart now").click()
        expect(notice).to_be_hidden()

        # --- logs, and the light theme
        nav.get_by_role("link", name="Logs").click()
        expect(page.get_by_role("list", name="Log lines")).to_contain_text(
            "agent.console"
        )
        page.get_by_role("button", name="Switch to light theme").click()
        assert page.evaluate("document.documentElement.dataset.theme") == "light"
        nav.get_by_role("link", name="Home").click()
        expect(page.get_by_text("Connected to the fabric")).to_be_visible(
            timeout=30_000
        )
        assert_axe_clean(page, "home (light)")

        # nothing secret is kept by the page
        dump = storage_dump(page)
        assert code not in dump
        assert (token_dir / "token").read_text().strip() not in dump
        assert code not in json.dumps(page.context.cookies())
        watch.assert_clean()
    finally:
        agent.send_signal(signal.SIGTERM)
        try:
            code_ = agent.wait(60)
        finally:
            log_file.close()
        assert code_ == 0, (tmp_path / "agent.log").read_text()[-4000:]
