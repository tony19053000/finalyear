"""V0.17 G2 (D392, Q20): "create your fabric" in real Chromium, against the real
controller process in setup mode, then sign in on the HTTPS dashboard it
restarts into.

The browser opens exactly the link ``meshforge-controller setup url`` prints;
the token must leave the address bar at once and never reach storage or a
script-readable cookie; the page shows the checks, refuses a weak password,
creates the fabric, shows the join code + QR; the setup listener then goes
away and the admin signs in on the TLS dashboard (Chromium trusting exactly the
new controller key). No console errors, no CSP violations.
"""

from __future__ import annotations

import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mf_session_client import free_port_pair

if TYPE_CHECKING:
    from playwright.sync_api import BrowserType, ConsoleMessage

    from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(300), pytest.mark.docker]

PASSWORD = "correct horse battery staple 42"


def _status(url: str, **kw: Any) -> int:
    try:
        with urllib.request.urlopen(url, timeout=2, **kw) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, ConnectionError, TimeoutError, ssl.SSLError):
        return 0


def _wait(url: str, proc: subprocess.Popen[str], **kw: Any) -> None:
    deadline = time.monotonic() + 60
    while _status(url, **kw) != 200:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise AssertionError(f"controller exited {proc.returncode}:\n{out}")
        assert time.monotonic() < deadline, f"{url} never answered"
        time.sleep(0.2)


class SetupController:
    def __init__(self, database: DatabaseSettings, tmp_path: Path) -> None:
        self.grpc_port, self.http_port = free_port_pair()
        self.enroll_port, self.setup_port = free_port_pair()
        self.data_dir = tmp_path / "data"
        self.cfg = tmp_path / "controller.toml"
        self.cfg.write_text(
            f"data_dir = {str(self.data_dir)!r}\n"
            '[log]\nformat = "json"\nlevel = "INFO"\n'
            "[controller]\n"
            f'grpc_bind = "127.0.0.1:{self.grpc_port}"\n'
            f'http_bind = "127.0.0.1:{self.http_port}"\n'
            "shutdown_drain_s = 1.0\n"
            "[controller.discovery]\nadvertise = false\n"
            f'[controller.enroll]\nbind = "127.0.0.1:{self.enroll_port}"\n'
            f'[controller.setup]\nbind = "127.0.0.1:{self.setup_port}"\n',
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
        env.update(
            {
                "MESHFORGE_DATABASE__HOST": database.host,
                "MESHFORGE_DATABASE__PORT": str(database.port),
                "MESHFORGE_DATABASE__NAME": database.name,
                "MESHFORGE_DATABASE__USER": database.user,
                "MESHFORGE_DATABASE__PASSWORD": database.password.get_secret_value(),
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.env = env
        self.cli = [
            sys.executable,
            "-c",
            "from meshforge.controller.app import main; main()",
        ]
        self.proc: subprocess.Popen[str] | None = None

    def setup_url(self) -> str:
        done = subprocess.run(
            [*self.cli, "setup", "url", "--config", str(self.cfg)],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def start(self) -> subprocess.Popen[str]:
        self.proc = subprocess.Popen(
            [*self.cli, "--config", str(self.cfg)],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        return self.proc

    def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()


@pytest.fixture
def setup_controller(
    pg_settings: DatabaseSettings, tmp_path: Path, dashboard_dist: Path
) -> Iterator[SetupController]:
    pytest.importorskip(
        "sqlalchemy", reason="the controller needs the controller extra"
    )
    controller = SetupController(pg_settings, tmp_path)
    try:
        yield controller
    finally:
        controller.close()


def test_create_fabric_in_the_browser_then_sign_in(
    setup_controller: SetupController,
    browser_type: BrowserType,
    chromium_installed: None,
) -> None:
    from mf_dashboard import spki_pin

    ctl = setup_controller
    url = ctl.setup_url()
    token = url.split("#token=", 1)[1]
    proc = ctl.start()
    _wait(f"http://127.0.0.1:{ctl.setup_port}/healthz", proc)

    problems: list[str] = []

    def on_console(msg: ConsoleMessage) -> None:
        if msg.type == "error":
            problems.append(msg.text)

    browser = browser_type.launch()
    try:
        page = browser.new_page()
        page.on("console", on_console)
        page.on("pageerror", lambda exc: problems.append(str(exc)))
        page.goto(url)
        page.get_by_text("Laptops join at").wait_for()
        assert token not in page.url
        assert token not in page.evaluate("document.cookie")
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        assert page.get_by_text(f"127.0.0.1:{ctl.enroll_port}").is_visible()
        page.get_by_label("Admin user name").fill("asha")
        page.get_by_label("Password", exact=True).fill("short")
        assert page.get_by_role("button", name="Create the fabric").is_disabled()
        page.get_by_label("Password", exact=True).fill(PASSWORD)
        page.get_by_label("Repeat the password").fill(PASSWORD)
        page.get_by_role("button", name="Create the fabric").click()
        page.get_by_text("Your fabric is ready").wait_for(timeout=30_000)
        code = page.get_by_label("Join code - shown only now").inner_text()
        assert code.strip().startswith("mf1-")
        assert page.get_by_role("img", name="QR code of the join code").is_visible()
        assert PASSWORD not in page.content()
        assert not problems, problems
    finally:
        browser.close()

    # The controller restarted itself into the TLS dashboard; setup is gone.
    ca = ctl.data_dir / "controller" / "ca" / "ca.crt"
    context = ssl.create_default_context(cafile=str(ca))
    _wait(f"https://127.0.0.1:{ctl.http_port}/healthz", proc, context=context)
    assert _status(f"http://127.0.0.1:{ctl.setup_port}/healthz") == 0
    pin = spki_pin("127.0.0.1", ctl.http_port, context)
    browser = browser_type.launch(args=[f"--ignore-certificate-errors-spki-list={pin}"])
    try:
        page = browser.new_page()
        page.goto(f"https://127.0.0.1:{ctl.http_port}/setup")
        page.get_by_text("This controller is already set up").wait_for()
        page.get_by_role("link", name="Go to the dashboard").click()
        page.get_by_label("User name").fill("asha")
        page.get_by_label("Password").fill(PASSWORD)
        page.get_by_role("button", name="Sign in").click()
        page.wait_for_url(f"https://127.0.0.1:{ctl.http_port}/", timeout=30_000)
        whoami = page.evaluate(
            "fetch('/v1/session', {credentials: 'same-origin'}).then(r => r.json())"
        )
        assert json.dumps(whoami).find('"asha"') >= 0
    finally:
        browser.close()
