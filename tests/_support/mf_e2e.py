"""Shared helpers of the dashboard e2e tier (V0.8 G7 consolidation, D179).

Before G7 every group's e2e file carried its own ``_page`` / ``_login`` /
``_sign_in`` / ``_no_axe`` / ``_tab_to`` / ``_job`` / ``_wait_state`` /
``_client`` copies. They live here once; the fixtures (``fabric``,
``dashboard_browser``, ``new_page`` with its end-of-test console + axe guard)
are in ``tests/e2e/conftest.py``. Browser-agnostic primitives (``PageWatch``,
``axe_violations``, ``spki_pin``, the build stamp) stay in ``mf_dashboard``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING, Any, Final

import pytest
from mf_cluster import ADMIN_USER
from mf_dashboard import axe_violations
from playwright.sync_api import expect

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mf_cluster import Fabric
    from mf_dashboard import PageWatch
    from playwright.sync_api import Locator, Page

    from meshforge.cli.client import ControllerClient

    #: The e2e ``new_page`` fixture (tests/e2e/conftest.py):
    #: ``new_page(init=None, timeout_ms=..., **context) -> (page, watch)``.
    NewPage = Callable[..., tuple[Page, PageWatch]]

#: Fabric options for tests that run jobs: the agents execute them with the
#: subprocess runtime (no Docker), dispatch is quick.
EXEC_OPTIONS: Final[dict[str, Any]] = {
    "agent_execution": {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0},
    "controller_jobs": {"dispatch_interval_s": 0.1},
    "agent_env": {"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
}

TERMINAL_JOB_STATES: Final = frozenset({"succeeded", "failed", "cancelled"})

#: Chromium's console line for a refused password (`POST /v1/session` or a
#: token create's `POST /v1/auth/login` -> 401): the browser reports every 4xx.
LOGIN_REFUSED_401: Final = ("status of 401", "/v1/session")
TOKEN_PASSWORD_REFUSED_401: Final = ("status of 401", "/v1/auth/login")
#: What Chromium logs while the controller is DOWN (G7 tester r1 T-2): refused
#: or dropped connections (``net::ERR_...`` - not ERR_HTTP_RESPONSE_CODE_FAILURE,
#: which is an HTTP status) and a WebSocket that could not connect - but NOT a
#: WebSocket handshake answered with an HTTP error code. An HTTP 4xx/5xx line is
#: never outage noise: allow it per resource with ``watch.allow``.
OUTAGE_NOISE: Final = (
    r"net::ERR_(?!HTTP_RESPONSE_CODE_FAILURE)[A-Z_]+"
    r"|WebSocket connection to '[^']*' failed:(?!\s*Error during WebSocket handshake)"
)


# --------------------------------------------------------------- browser


def fill_login(page: Page, user: str, password: str) -> None:
    """Type into the login form and press Sign in (nothing awaited)."""
    page.get_by_label("User name").fill(user)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()


def sign_in(
    page: Page, fabric: Fabric, user: str = ADMIN_USER, *, path: str | None = "/"
) -> None:
    """Sign ``user`` in and wait until the shell is up with a live event
    stream. ``path`` is opened first (``None``: the login form is already
    showing, e.g. after a session ended); from ``/`` the Overview is awaited."""
    if path is not None:
        page.goto(fabric.http_base + path)
    fill_login(page, user, fabric.password(user))
    expect(page.get_by_test_id("current-user")).to_be_visible()
    if path == "/":
        expect(page.get_by_role("heading", level=1, name="Overview")).to_be_visible()
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )


def assert_axe_clean(page: Page, where: str) -> None:
    violations = axe_violations(page)
    assert violations == [], f"axe violations on {where}: {violations}"


def focused_name(page: Page) -> str:
    """The focused control's aria-label, else its text (trimmed)."""
    return str(
        page.evaluate(
            "(document.activeElement?.getAttribute('aria-label') "
            "|| document.activeElement?.textContent || '').trim()"
        )
    )


def tab_to(page: Page, name: str, limit: int = 120) -> None:
    """Move focus with Tab only until the focused control's accessible name
    (aria-label or text) is ``name``."""
    for _ in range(limit):
        if focused_name(page) == name:
            return
        page.keyboard.press("Tab")
    if focused_name(page) != name:
        pytest.fail(f"Tab never reached {name!r}")


def tab_to_locator(page: Page, target: Locator, limit: int = 40) -> None:
    """Move focus with Tab only until ``target`` has it."""
    for _ in range(limit):
        if target.evaluate("el => el === document.activeElement"):
            break
        page.keyboard.press("Tab")
    expect(target).to_be_focused()


def storage_dump(page: Page) -> str:
    """Every place a script could keep a value: web storage, cookies it can
    read, the URL and window.name (checklist 25)."""
    return str(
        page.evaluate(
            "JSON.stringify([Object.entries(localStorage), "
            "Object.entries(sessionStorage), document.cookie, location.href, window.name])"
        )
    )


# --------------------------------------------------------------- the API


def api_nodes(fabric: Fabric) -> list[dict[str, Any]]:
    listed = fabric.get("/v1/nodes")
    assert listed is not None
    return list(listed["nodes"])


def wait_all_reporting(fabric: Fabric, n: int) -> list[dict[str, Any]]:
    """Wait until ``n`` nodes are connected and each reported a sample."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        nodes = api_nodes(fabric)
        live = [x for x in nodes if x["connected"] and x["cpu_percent"] is not None]
        if len(nodes) == n and len(live) == n:
            return nodes
        time.sleep(0.2)
    pytest.fail(f"the {n} agents did not all report a sample: {api_nodes(fabric)}")


def get_job(fabric: Fabric, job_id: str, user: str | None = None) -> dict[str, Any]:
    resp = fabric.call("GET", f"/v1/jobs/{job_id}", as_user=user or ADMIN_USER)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def job_state(fabric: Fabric, job_id: str, user: str | None = None) -> str:
    return str(get_job(fabric, job_id, user)["state"])


def wait_job_state(
    fabric: Fabric,
    job_id: str,
    states: set[str] | frozenset[str],
    timeout_s: float,
    user: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    job = get_job(fabric, job_id, user)
    while job["state"] not in states and time.monotonic() < deadline:
        time.sleep(0.25)
        job = get_job(fabric, job_id, user)
    assert job["state"] in states, job
    return job


def put_tree(fabric: Fabric, project: Path, user: str | None = None) -> str:
    """Upload ``project`` as a tree through the blob API; its tree id."""
    from meshforge.common.tree import build_tree

    built = build_tree(project)
    for hex_, path in built.sources.items():
        resp = fabric.call(
            "PUT",
            f"/v1/blobs/{hex_}",
            as_user=user or ADMIN_USER,
            content=path.read_bytes(),
        )
        assert resp.status_code in (200, 201), resp.text
    resp = fabric.call(
        "PUT",
        f"/v1/blobs/{built.tree.tree_id.hex}",
        as_user=user or ADMIN_USER,
        content=built.manifest,
    )
    assert resp.status_code in (200, 201), resp.text
    return str(built.tree.tree_id)


def submit_job(
    fabric: Fabric,
    tree_id: str,
    name: str,
    user: str | None = None,
    **manifest: Any,
) -> str:
    """``POST /v1/jobs`` (entrypoint ``python main.py`` unless given)."""
    body = {
        "manifest": {"name": name, "entrypoint": ["python", "main.py"], **manifest},
        "tree_id": tree_id,
    }
    resp = fabric.call("POST", "/v1/jobs", as_user=user or ADMIN_USER, json=body)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"])


def user_client(fabric: Fabric, user: str) -> ControllerClient:
    """The CLI's own client, authenticated as ``user``."""
    from pydantic import SecretStr

    from meshforge.cli.client import ControllerClient

    return ControllerClient(
        fabric.http_base,
        timeout_s=30,
        token=SecretStr(fabric.api_token(user)),
        ssl_context=fabric.ssl_context,
    )


def run_compute(
    fabric: Fabric, *args: str, cwd: Path, user: str = ADMIN_USER
) -> subprocess.CompletedProcess[str]:
    """The real ``compute`` CLI (a subprocess) against ``fabric`` as ``user``:
    what a terminal user sees, for browser/CLI parity checks."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__CA_FILE": str(fabric.ca_dir / "ca.crt"),
            "MESHFORGE_CLI__TOKEN": fabric.api_token(user),
            "MESHFORGE_DATA_DIR": str(cwd / "cli-data"),
            "XDG_CONFIG_HOME": str(cwd / "cli-config"),
            "NO_COLOR": "1",
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "meshforge.cli", *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
