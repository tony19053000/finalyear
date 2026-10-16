"""V0.8 G5 (0.8.6, D176 rest, D199): the Settings pages end to end - a real
controller and real agents (cluster harness), the built SPA, real Chromium.

* An admin creates an invite in the browser; a laptop (a real, not yet
  enrolled agent) joins with exactly the code the page shows, whose QR - read
  back from the page's SVG and decoded by an independent decoder - is the same
  string; the agent connects and the invite turns "used" and the node appears
  without a reload.
* An API token created in the browser works, and after "Revoke" in the
  browser it is refused with 401 (keyboard-only revoke).
* A user stores a secret in the browser; a job reads it; the value is never
  in the DOM, any network response, the console or web storage.
* Named inputs: pin and delete from the browser (confirm dialog).
* User vs admin views; axe clean on every Settings page; zero console errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

pytest.importorskip(
    "playwright", reason="needs the dev dependency group (pytest-playwright)"
)
pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="the controller needs the controller extra")

from mf_cluster import ADMIN_USER, NORMAL_USER
from mf_dashboard import PageWatch, axe_violations
from playwright.sync_api import Browser, Page, Response, expect
from pydantic import SecretStr
from qr_decode import decode_matrix, matrix_from_svg_path

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.tree import build_tree

if TYPE_CHECKING:
    from mf_cluster import Fabric

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

#: Chromium's console line for the refused password of a token create (a real 401).
LOGIN_REFUSED_401 = ("status of 401", "/v1/auth/login")
SETTINGS_PAGES = ("", "/tokens", "/secrets", "/data")
ADMIN_PAGES = ("/invites", "/users")
EXEC_OPTIONS = {
    "agent_execution": {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0},
    "controller_jobs": {"dispatch_interval_s": 0.1},
    "agent_env": {"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
}


def _page(browser: Browser, **context: Any) -> tuple[Page, PageWatch]:
    made = browser.new_context(reduced_motion="reduce", **context)
    page = made.new_page()
    page.set_default_timeout(20_000)
    return page, PageWatch.attach(page)


def _sign_in(page: Page, fabric: Fabric, user: str = ADMIN_USER) -> None:
    page.goto(fabric.http_base + "/")
    page.get_by_label("User name").fill(user)
    page.get_by_label("Password").fill(fabric.password(user))
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("heading", level=1, name="Overview")).to_be_visible()
    expect(page.get_by_test_id("stream-status")).to_have_attribute(
        "data-status", "live"
    )


def _no_axe(page: Page, where: str) -> None:
    violations = axe_violations(page)
    assert violations == [], f"axe violations on {where}: {violations}"


def _storage(page: Page) -> str:
    return str(
        page.evaluate(
            "JSON.stringify([Object.entries(localStorage), "
            "Object.entries(sessionStorage), document.cookie, location.href, window.name])"
        )
    )


def _client(fabric: Fabric, user: str) -> ControllerClient:
    return ControllerClient(
        fabric.http_base,
        timeout_s=30,
        token=SecretStr(fabric.api_token(user)),
        ssl_context=fabric.ssl_context,
    )


def _agent_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["PYTHONUTF8"] = "1"
    return env


# --------------------------------------------------------------- invites


@pytest.mark.parametrize(
    "fabric_options",
    [{"agents": 1, "enroll_agents": False, "start_agents": False}],
)
def test_invite_in_the_browser_lets_a_laptop_join(
    fabric: Fabric, dashboard_browser: Browser
) -> None:
    page, watch = _page(dashboard_browser)
    page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"], origin=fabric.http_base
    )
    _sign_in(page, fabric)
    page.get_by_role("link", name="Settings", exact=True).click()
    settings_nav = page.get_by_role("navigation", name="Settings")
    settings_nav.get_by_role("link", name="Invites").click()
    expect(page.get_by_text("No invites yet.")).to_be_visible()
    _no_axe(page, "the empty invites page")

    page.get_by_role("button", name="Create invite").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Code expires after").select_option(label="1 hour")
    dialog.get_by_role("button", name="Create invite").click()
    issued = dialog.get_by_test_id("issued-invite")
    expect(issued).to_be_visible()
    code = issued.locator("pre").first.inner_text().strip()
    assert code.startswith("mf1-"), code
    _no_axe(page, "the issued invite dialog")

    # The QR says exactly the code: read back from the SVG, decoded independently.
    qr = issued.get_by_role("img", name="QR code of the join code")
    d = qr.locator("path").get_attribute("d")
    modules = int(qr.get_attribute("data-modules") or "0")
    margin = int(qr.get_attribute("data-margin") or "0")
    assert d is not None and modules >= 21
    assert decode_matrix(matrix_from_svg_path(d, modules, margin)) == code
    assert "style=" not in (qr.evaluate("e => e.outerHTML") or "")

    # Copy puts exactly the code on the clipboard.
    issued.get_by_role("button", name="Copy").click()
    expect(issued.get_by_text("Copied.")).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == code

    # The API lists the invite (never its code).
    listed = fabric.call("GET", "/v1/fabric/invites").json()["invites"]
    assert len(listed) == 1 and listed[0]["state"] == "pending"
    assert code not in json.dumps(listed)
    invite_id = listed[0]["id"]

    issued.get_by_role("button", name="Done").click()
    expect(dialog).to_be_hidden()
    assert code not in (page.content() or "")
    assert code not in _storage(page)
    row = page.get_by_role("row").filter(has_text=invite_id)
    expect(row).to_contain_text("pending")

    # A laptop joins with the code the page showed (as `meshforge-agent join`).
    (node,) = fabric.nodes
    joined = subprocess.run(
        [sys.executable, "-m", "meshforge.agent", "join", "-",
         "--config", str(node.config)],
        input=code + "\n", env=_agent_env(), capture_output=True, text=True,
        timeout=60, check=False,
    )  # fmt: skip
    assert joined.returncode == 0, joined.stderr
    fabric.start_agents()  # mutual TLS with the certificate just issued

    # Live (no reload): its connection refetches the invite list -> "used".
    expect(row).to_contain_text("used", timeout=15_000)
    expect(row.get_by_role("link", name=node.node_id)).to_be_visible()
    page.get_by_role("navigation", name="Main").get_by_role(
        "link", name="Nodes", exact=True
    ).click()
    detail = fabric.get(f"/v1/nodes/{node.node_id}")
    assert detail is not None
    expect(page.get_by_role("link", name=detail["hostname"])).to_be_visible()
    watch.assert_clean()


# --------------------------------------------------------------- tokens


def _focus_by_tab(page: Page, accessible_name: str, limit: int = 80) -> None:
    for _ in range(limit):
        label = page.evaluate(
            "document.activeElement?.getAttribute('aria-label') "
            "|| document.activeElement?.textContent"
        )
        if label == accessible_name:
            return
        page.keyboard.press("Tab")
    pytest.fail(f"Tab never reached {accessible_name!r}")


def test_a_browser_made_token_works_until_revoked_in_the_browser(
    fabric: Fabric, dashboard_browser: Browser
) -> None:
    page, watch = _page(dashboard_browser)
    _sign_in(page, fabric)
    page.goto(fabric.http_base + "/settings/tokens")
    expect(page.get_by_role("table")).to_contain_text("Browser session")
    _no_axe(page, "the tokens page")

    # A wrong password is an error in the dialog, never a sign-out.
    watch.allow(*LOGIN_REFUSED_401)
    page.get_by_role("button", name="Create API token").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Token name").fill("e2e-ci")
    dialog.get_by_label("Your password").fill("not-the-password")
    dialog.get_by_role("button", name="Create token").click()
    expect(dialog.get_by_role("alert")).to_be_visible()
    expect(page.get_by_test_id("current-user")).to_be_visible()

    dialog.get_by_label("Your password").fill(fabric.password(ADMIN_USER))
    dialog.get_by_role("button", name="Create token").click()
    shown = dialog.get_by_test_id("new-token")
    expect(shown).to_be_visible()
    token = shown.locator("pre").first.inner_text().strip()
    assert len(token) >= 32
    _no_axe(page, "the new token dialog")
    shown.get_by_role("button", name="Done").click()
    expect(dialog).to_be_hidden()
    assert token not in (page.content() or "")
    assert token not in _storage(page)
    assert fabric.password(ADMIN_USER) not in (page.content() or "")

    bearer = {"authorization": f"Bearer {token}"}
    with httpx.Client(verify=fabric.ssl_context, timeout=10) as http:
        who = http.get(fabric.http_base + "/v1/auth/whoami", headers=bearer)
        assert who.status_code == 200, who.text
        token_id = who.json()["token_id"]

        # Revoke it in the browser, keyboard only.
        row = page.get_by_role("row").filter(has_text=token_id)
        expect(row).to_contain_text("active")
        _focus_by_tab(page, f"Revoke e2e-ci ({token_id})")
        page.keyboard.press("Enter")
        confirm = page.get_by_role("dialog")
        expect(confirm).to_be_visible()
        _no_axe(page, "the revoke confirm dialog")
        expect(confirm.get_by_role("button", name="Cancel")).to_be_focused()
        page.keyboard.press("Tab")
        expect(confirm.get_by_role("button", name="Revoke")).to_be_focused()
        page.keyboard.press("Enter")
        expect(page.get_by_text("e2e-ci revoked", exact=True)).to_be_visible()
        expect(row).to_contain_text("revoked")

        refused = http.get(fabric.http_base + "/v1/auth/whoami", headers=bearer)
        assert refused.status_code == 401, refused.text
    watch.assert_clean()


# --------------------------------------------------------------- secrets


JOB_MAIN = """\
import hashlib, os
value = open(os.path.join(os.environ["MESHFORGE_SECRETS_DIR"], "E2E_GREETING"), "rb").read()
print("sha=" + hashlib.sha256(value).hexdigest(), flush=True)
print("value=" + value.decode(), flush=True)
"""


@pytest.mark.parametrize("fabric_options", [EXEC_OPTIONS])
def test_a_secret_set_in_the_browser_reaches_a_job_and_never_the_browser(
    fabric: Fabric, dashboard_browser: Browser, tmp_path: Path
) -> None:
    value = "mf-e2e-" + secrets.token_hex(12)
    page, watch = _page(dashboard_browser)
    bodies: list[str] = []
    consoles: list[str] = []

    def keep(response: Response) -> None:
        if "/v1/" in response.url:
            try:
                bodies.append(response.text())
            except Exception:  # noqa: BLE001 - a body we cannot read holds nothing
                bodies.append("")

    page.on("response", keep)
    page.on("console", lambda m: consoles.append(m.text))
    _sign_in(page, fabric, NORMAL_USER)
    page.goto(fabric.http_base + "/settings/secrets")
    expect(page.get_by_text("No secrets yet.")).to_be_visible()
    _no_axe(page, "the secrets page")

    page.get_by_role("button", name="Set a secret").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Name", exact=True).fill("e2e_greeting")
    expect(dialog.get_by_text("Stored as E2E_GREETING.")).to_be_visible()
    dialog.get_by_label("Value", exact=True).fill(value)
    _no_axe(page, "the set-secret dialog")
    dialog.get_by_role("button", name="Store secret").click()
    expect(page.get_by_text("Secret E2E_GREETING stored")).to_be_visible()
    expect(page.get_by_role("cell", name="E2E_GREETING", exact=True)).to_be_visible()

    # A job of the same user reads it.
    root = tmp_path / "secret-job"
    root.mkdir()
    (root / "main.py").write_text(JOB_MAIN)
    built = build_tree(root)
    client = _client(fabric, NORMAL_USER)
    try:
        upload_tree(client, built)
    finally:
        client.close()
    posted = fabric.call(
        "POST",
        "/v1/jobs",
        as_user=NORMAL_USER,
        json={
            "manifest": {
                "name": "e2e-secret",
                "entrypoint": ["python", "main.py"],
                "secrets": ["E2E_GREETING"],
                "retry": {"max_attempts": 1},
            },
            "tree_id": str(built.tree.tree_id),
        },
    )
    assert posted.status_code == 201, posted.text
    job_id = posted.json()["job_id"]
    deadline = time.monotonic() + 120
    job = fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json()
    while job["state"] not in {"succeeded", "failed", "cancelled"}:
        assert time.monotonic() < deadline, job
        time.sleep(0.25)
        job = fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json()
    assert job["state"] == "succeeded", job
    log = fabric.call(
        "GET",
        f"/v1/jobs/{job_id}/logs",
        as_user=NORMAL_USER,
        params={"stream": "stdout"},
    ).text
    assert "sha=" + hashlib.sha256(value.encode()).hexdigest() in log
    assert "[secret:E2E_GREETING]" in log and value not in log

    # The browser shows the job's log - masked - and never the value anywhere.
    page.goto(fabric.http_base + f"/jobs/{job_id}")
    expect(page.get_by_text("sha=", exact=False).first).to_be_visible()
    page.goto(fabric.http_base + "/settings/secrets")
    expect(page.get_by_role("cell", name="E2E_GREETING", exact=True)).to_be_visible()
    assert value not in (page.content() or "")
    assert value not in _storage(page)
    assert all(value not in body for body in bodies), "a response carried the value"
    assert all(value not in line for line in consoles)

    # Delete, behind a confirm.
    page.get_by_role("button", name="Delete secret E2E_GREETING").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete secret").click()
    expect(page.get_by_text("Secret E2E_GREETING deleted")).to_be_visible()
    expect(page.get_by_text("No secrets yet.")).to_be_visible()
    listed = fabric.call("GET", "/v1/secrets", as_user=NORMAL_USER).json()
    assert listed["secrets"] == []
    watch.assert_clean()


# --------------------------------------------------------------- data


def test_data_pin_and_delete_from_the_browser(
    fabric: Fabric, dashboard_browser: Browser, tmp_path: Path
) -> None:
    root = tmp_path / "dataset"
    root.mkdir()
    (root / "a.txt").write_text("alpha\n")
    (root / "b.txt").write_text("beta\n")
    built = build_tree(root)
    client = _client(fabric, NORMAL_USER)
    try:
        upload_tree(client, built)
        pushed = client.push_input("e2e-data", str(built.tree.tree_id), pinned=False)
    finally:
        client.close()
    ref = str(pushed["ref"])

    page, watch = _page(dashboard_browser)
    _sign_in(page, fabric, NORMAL_USER)
    page.goto(fabric.http_base + "/settings/data")
    row = page.get_by_role("row").filter(has_text=ref)
    expect(row).to_contain_text("not pinned")
    expect(row).to_contain_text(str(pushed["files"]))
    _no_axe(page, "the data page")

    row.get_by_role("button", name=f"Pin {ref}").click()
    expect(page.get_by_text(f"{ref} pinned", exact=True)).to_be_visible()
    expect(row.get_by_role("button", name=f"Unpin {ref}")).to_be_visible()
    listed = fabric.call("GET", "/v1/data", as_user=NORMAL_USER).json()["inputs"]
    assert [i["pinned"] for i in listed if i["ref"] == ref] == [True]

    row.get_by_role("button", name=f"Delete {ref}").click()
    confirm = page.get_by_role("dialog")
    _no_axe(page, "the delete confirm dialog")
    confirm.get_by_role("button", name="Delete version").click()
    expect(page.get_by_text(f"{ref} deleted", exact=True)).to_be_visible()
    expect(page.get_by_text("No named inputs yet.")).to_be_visible()
    listed = fabric.call("GET", "/v1/data", as_user=NORMAL_USER).json()["inputs"]
    assert listed == []
    watch.assert_clean()


# --------------------------------------------------------------- views


def test_user_and_admin_views_and_axe_on_every_settings_page(
    fabric: Fabric, dashboard_browser: Browser
) -> None:
    # A user: no admin sections, no admin calls, axe clean.
    page, watch = _page(dashboard_browser)
    requested: list[str] = []
    page.on("request", lambda r: requested.append(r.url))
    _sign_in(page, fabric, NORMAL_USER)
    page.get_by_role("link", name="Settings", exact=True).click()
    nav = page.get_by_role("navigation", name="Settings")
    expect(nav.get_by_role("link")).to_have_text(
        ["Account", "API tokens", "Secrets", "Data"]
    )
    for path in SETTINGS_PAGES:
        page.goto(fabric.http_base + "/settings" + path)
        expect(page.get_by_role("heading", level=1, name="Settings")).to_be_visible()
        expect(page.get_by_text("Loading…")).to_have_count(0)
        _no_axe(page, f"user /settings{path}")
    expect(page.get_by_label("Every user")).to_have_count(0)
    page.goto(fabric.http_base + "/settings/invites")
    expect(page.get_by_text("Invites are made by admins")).to_be_visible()
    _no_axe(page, "user /settings/invites")
    page.goto(fabric.http_base + "/settings/users")
    expect(page.get_by_text("Users are managed by admins")).to_be_visible()
    assert not [u for u in requested if "/v1/fabric/invites" in u]
    assert not [u for u in requested if "all=true" in u]
    watch.assert_clean()
    page.context.close()

    # An admin: every section, the controller card shows /healthz's figures.
    page, watch = _page(dashboard_browser)
    _sign_in(page, fabric, ADMIN_USER)
    page.get_by_role("link", name="Settings", exact=True).click()
    nav = page.get_by_role("navigation", name="Settings")
    expect(nav.get_by_role("link")).to_have_text(
        ["Account", "API tokens", "Secrets", "Data", "Invites", "Users"]
    )
    health = fabric.get("/healthz")
    assert health is not None
    controller = page.get_by_test_id("controller-card")
    expect(controller).to_contain_text(str(health["status"]))
    expect(controller).to_contain_text(fabric.http_base)
    for path in (*SETTINGS_PAGES, *ADMIN_PAGES):
        page.goto(fabric.http_base + "/settings" + path)
        expect(page.get_by_role("heading", level=1, name="Settings")).to_be_visible()
        expect(page.get_by_text("Loading…")).to_have_count(0)
        _no_axe(page, f"admin /settings{path}")
    expect(page.get_by_test_id("users-card")).to_contain_text(
        "meshforge-controller user create"
    )
    page.goto(fabric.http_base + "/settings/tokens")
    page.get_by_label("Every user").check()
    expect(page.get_by_role("table")).to_contain_text(NORMAL_USER)
    for theme in ("light", "dark"):
        if page.evaluate("document.documentElement.dataset.theme") != theme:
            page.get_by_role("button", name="Switch to light theme").or_(
                page.get_by_role("button", name="Switch to dark theme")
            ).click()
        _no_axe(page, f"admin tokens ({theme})")
    watch.assert_clean()
