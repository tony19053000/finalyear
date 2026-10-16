"""V0.8 G5 tester r1 (e2e): adversarial checks derived from the brief 5e, not
from the implementation. Real controller, real agents, real Chromium.

Each test is named after the criterion it attacks.
"""

from __future__ import annotations

import hashlib
import json
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
from mf_e2e import (
    EXEC_OPTIONS,
    TOKEN_PASSWORD_REFUSED_401,
    sign_in,
    tab_to,
    user_client,
)
from playwright.sync_api import expect
from qr_decode import decode_matrix, matrix_from_svg_path

from meshforge.cli.transfer import upload_tree
from meshforge.common import join_code
from meshforge.common.tree import build_tree

if TYPE_CHECKING:
    from mf_cluster import Fabric
    from mf_e2e import NewPage
    from playwright.sync_api import Page

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.docker,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX agent runtime"),
]

LOGIN_REFUSED_401 = TOKEN_PASSWORD_REFUSED_401


def _mutations(page: Page) -> list[str]:
    seen: list[str] = []
    page.on(
        "request",
        lambda r: (
            seen.append(f"{r.method} {r.url}")
            if "/v1/" in r.url and r.method not in ("GET", "HEAD")
            else None
        ),
    )
    return seen


def _push_dataset(
    fabric: Fabric, tmp_path: Path, name: str, files: int = 2
) -> dict[str, Any]:
    root = tmp_path / f"ds-{name}"
    root.mkdir()
    for i in range(files):
        (root / f"f{i}.txt").write_text(f"payload {i}\n")
    built = build_tree(root)
    client = user_client(fabric, ADMIN_USER)
    try:
        upload_tree(client, built)
        return dict(client.push_input(name, str(built.tree.tree_id), pinned=False))
    finally:
        client.close()


# ------------------------------------------------- invites: QR == join code


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_invite_qr_decodes_to_a_code_the_real_parser_accepts_for_this_controller(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = new_page()
    sign_in(page, fabric)
    page.goto(fabric.http_base + "/settings/invites")
    posts: list[str] = []
    page.on(
        "response",
        lambda r: (
            posts.append(r.url)
            if r.request.method == "POST" and r.url.endswith("/v1/fabric/invites")
            else None
        ),
    )
    page.get_by_role("button", name="Create invite").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Code expires after").select_option(label="5 minutes")
    btn = dialog.get_by_role("button", name="Create invite")
    btn.dblclick()  # a double click must mint ONE invite
    issued = dialog.get_by_test_id("issued-invite")
    expect(issued).to_be_visible()
    code = issued.locator("pre").first.inner_text().strip()

    qr = issued.get_by_role("img", name="QR code of the join code")
    d = qr.locator("path").get_attribute("d")
    modules = int(qr.get_attribute("data-modules") or "0")
    margin = int(qr.get_attribute("data-margin") or "0")
    assert d
    from_qr = decode_matrix(matrix_from_svg_path(d, modules, margin))
    assert from_qr == code

    # What `compute join`/`meshforge-agent join` parses: the real decoder.
    parsed = join_code.decode(from_qr)
    assert fabric.http_base.split("//")[1].split(":")[0] in str(parsed)
    assert join_code.encode(parsed) == code  # canonical, nothing lost or added

    # TTL shown in the dialog == the API's expiry (item 26: 5 minutes).
    listed = fabric.call("GET", "/v1/fabric/invites").json()["invites"]
    assert len(listed) == 1, "double click minted more than one invite"
    assert len(posts) == 1, posts
    exp = listed[0]["expires_at"]
    created = listed[0]["created_at"]
    from datetime import datetime

    ttl = (
        datetime.fromisoformat(exp.replace("Z", "+00:00"))
        - datetime.fromisoformat(created.replace("Z", "+00:00"))
    ).total_seconds()
    assert 299 <= ttl <= 301, ttl

    # Close with Escape (not the Done button): code must be gone as well.
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    html = page.content()
    assert code not in html
    storage = page.evaluate(
        "JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage),"
        " document.cookie, location.href, history.state, window.name])"
    )
    assert code not in storage
    # not in any later /v1 response either (re-listing invites)
    page.reload()
    page.goto(fabric.http_base + "/settings/invites")
    expect(page.get_by_role("table")).to_be_visible()
    assert code not in page.content()
    assert page.get_by_role("img", name="QR code of the join code").count() == 0
    watch.assert_clean()


# --------------------------------------------- tokens: 401 on real API use


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_revoked_token_is_401_everywhere_and_closes_its_live_stream(
    fabric: Fabric, new_page: NewPage
) -> None:
    from websockets.sync.client import connect

    page, watch = new_page()
    sign_in(page, fabric)
    page.goto(fabric.http_base + "/settings/tokens")
    page.get_by_role("button", name="Create API token").click()
    dialog = page.get_by_role("dialog")
    dialog.get_by_label("Token name").fill("türk \U0001f680 token")
    dialog.get_by_label("Your password").fill(fabric.password(ADMIN_USER))
    dialog.get_by_role("button", name="Create token").click()
    shown = dialog.get_by_test_id("new-token")
    expect(shown).to_be_visible()
    token = shown.locator("pre").first.inner_text().strip()
    dialog_text = dialog.inner_text()
    assert fabric.password(ADMIN_USER) not in dialog_text
    # Esc closes without Done: token gone.
    page.keyboard.press("Escape")
    expect(dialog).to_be_hidden()
    assert token not in page.content()

    bearer = {"authorization": f"Bearer {token}"}
    ws_url = fabric.http_base.replace("https://", "wss://") + "/v1/events/ws"
    paths = ["/v1/auth/whoami", "/v1/nodes", "/v1/jobs", "/v1/secrets", "/v1/data"]
    assert fabric.ssl_context is not None
    with httpx.Client(verify=fabric.ssl_context, timeout=10) as http:
        for p in paths:
            assert http.get(fabric.http_base + p, headers=bearer).status_code == 200, p
        who = http.get(fabric.http_base + "/v1/auth/whoami", headers=bearer).json()
        with connect(
            ws_url, additional_headers=bearer, ssl=fabric.ssl_context, open_timeout=10
        ) as sock:
            sock.recv(timeout=10)  # live
            row = page.get_by_role("row").filter(has_text=who["token_id"])
            expect(row).to_contain_text("active")
            row.get_by_role(
                "button", name=f"Revoke türk \U0001f680 token ({who['token_id']})"
            ).click()
            page.get_by_role("dialog").get_by_role("button", name="Revoke").click()
            expect(row).to_contain_text("revoked")
            closed = False
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    sock.recv(timeout=2)
                except TimeoutError:
                    continue
                except Exception:  # noqa: BLE001 - ConnectionClosed
                    closed = True
                    break
            assert closed, "revoked token's event stream stayed open"
        for p in paths:
            assert http.get(fabric.http_base + p, headers=bearer).status_code == 401, p
        # a write with the revoked token is refused too
        assert (
            http.put(
                fabric.http_base + "/v1/secrets/NOPE",
                headers={**bearer, "content-type": "application/octet-stream"},
                content=b"abcdefgh",
            ).status_code
            == 401
        )
        assert (
            http.get(fabric.http_base + "/v1/events/ws", headers=bearer).status_code
            != 200
        )
        # revoking twice via the API is idempotent and honest
        again = fabric.call("DELETE", f"/v1/auth/tokens/{who['token_id']}")
        assert again.status_code in (200, 404), again.text
    watch.assert_clean()


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_revoking_own_session_signs_the_browser_out_for_real(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = new_page()
    # Revoking the session this page runs on: the reads in flight at that
    # moment are refused (401) before the page shows the sign-in form. The
    # watch judges each console line as it arrives, so say it up front.
    # Which reads are in flight at that moment is timing: each may not happen.
    for path in ("/v1/auth/whoami", "/v1/auth/tokens", "/v1/nodes", "/v1/overview"):
        watch.allow(
            "status of 401", path, "a read in flight at revocation", may_not_occur=True
        )
    sign_in(page, fabric, NORMAL_USER)
    # a second tab of the same session
    other = page.context.new_page()
    other.goto(fabric.http_base + "/")
    expect(other.get_by_role("heading", level=1, name="Overview")).to_be_visible()

    page.goto(fabric.http_base + "/settings/tokens")
    row = page.get_by_role("row").filter(has_text="this session")
    expect(row).to_have_count(1)
    row.get_by_role("button").first.click()
    confirm = page.get_by_role("dialog")
    expect(confirm).to_contain_text("signed out")
    confirm.get_by_role("button", name="Sign out").click()
    # Honest outcome: the sign-in form (not a stale authenticated shell).
    expect(page.get_by_label("User name")).to_be_visible(timeout=20_000)
    # The other tab must not stay logged in forever either.
    other.reload()
    expect(other.get_by_label("User name")).to_be_visible(timeout=20_000)
    # And the cookie really is dead server-side.
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    assert fabric.ssl_context is not None
    r = httpx.get(
        fabric.http_base + "/v1/auth/whoami",
        cookies={k: v for k, v in cookies.items() if k.startswith("mf_")},
        verify=fabric.ssl_context,
    )
    assert r.status_code == 401
    assert not watch.page_errors, watch.page_errors


# ------------------------------------------------ CSRF parity (item 0a)


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_no_mutation_is_sent_without_the_csrf_cookie(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    ref = str(_push_dataset(fabric, tmp_path, "csrf-data")["ref"])
    page, watch = new_page()
    sign_in(page, fabric)
    sent = _mutations(page)
    names = {c["name"] for c in page.context.cookies()}
    assert "mf_csrf" in names, names
    page.context.clear_cookies(name="mf_csrf")
    assert "mf_csrf" not in {c["name"] for c in page.context.cookies()}

    page.goto(fabric.http_base + "/settings/data")
    # the page reload may rotate the cookie? clear again after load
    expect(page.get_by_role("row").filter(has_text=ref)).to_be_visible()
    page.context.clear_cookies(name="mf_csrf")
    page.get_by_role("button", name=f"Pin {ref}").click()
    page.wait_for_timeout(800)
    page.get_by_role("button", name=f"Delete {ref}").click()
    page.get_by_role("dialog").get_by_role("button", name="Delete version").click()
    page.wait_for_timeout(800)
    assert not [s for s in sent if "/v1/data" in s], sent

    page.goto(fabric.http_base + "/settings/secrets")
    page.context.clear_cookies(name="mf_csrf")
    page.get_by_role("button", name="Set a secret").click()
    d = page.get_by_role("dialog")
    d.get_by_label("Name", exact=True).fill("csrf_probe")
    d.get_by_label("Value", exact=True).fill("abcdefgh")
    d.get_by_role("button", name="Store secret").click()
    page.wait_for_timeout(800)
    assert not [s for s in sent if "/v1/secrets" in s], sent
    # an honest error, not a silent success
    assert fabric.call("GET", "/v1/secrets").json()["secrets"] == []
    page.keyboard.press("Escape")

    page.goto(fabric.http_base + "/settings/invites")
    page.context.clear_cookies(name="mf_csrf")
    page.get_by_role("button", name="Create invite").click()
    d = page.get_by_role("dialog")
    d.get_by_role("button", name="Create invite").click()
    page.wait_for_timeout(800)
    assert not [s for s in sent if "invites" in s], sent
    assert fabric.call("GET", "/v1/fabric/invites").json()["invites"] == []
    page.keyboard.press("Escape")

    page.goto(fabric.http_base + "/settings/tokens")
    page.context.clear_cookies(name="mf_csrf")
    page.get_by_role("button", name="Create API token").click()
    d = page.get_by_role("dialog")
    d.get_by_label("Your password").fill(fabric.password(ADMIN_USER))
    d.get_by_role("button", name="Create token").click()
    page.wait_for_timeout(800)
    assert not [s for s in sent if "/v1/auth/login" in s], sent
    # data untouched on the server
    inputs = fabric.call("GET", "/v1/data").json()["inputs"]
    assert [i["pinned"] for i in inputs if i["ref"] == ref] == [False]
    assert not watch.page_errors, watch.page_errors


# -------------------------------- secrets: binary file, no mangling, write-only


@pytest.mark.parametrize("fabric_options", [EXEC_OPTIONS])
def test_secret_read_from_a_file_reaches_the_job_byte_for_byte(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    """Brief: secret set 'value write-only'; the UI offers 'read from a file
    (keys, certificates, ...)'. The stored bytes must equal the file bytes."""
    payload = b"\xef\xbb\xbfBOM-start\r\nline2\x00nul\xff\xfe\x80tail\n"
    secret_file = tmp_path / "blob.bin"
    secret_file.write_bytes(payload)
    page, _watch = new_page()
    sign_in(page, fabric, NORMAL_USER)
    page.goto(fabric.http_base + "/settings/secrets")
    page.get_by_role("button", name="Set a secret").click()
    d = page.get_by_role("dialog")
    d.get_by_label("Name", exact=True).fill("BLOBBY")
    d.get_by_label("Or read the value from a file", exact=False).set_input_files(
        str(secret_file)
    )
    d.get_by_role("button", name="Store secret").click()
    expect(page.get_by_text("Secret BLOBBY stored", exact=True)).to_be_visible()

    root = tmp_path / "job"
    root.mkdir()
    (root / "main.py").write_text(
        "import hashlib, os\n"
        "v = open(os.path.join(os.environ['MESHFORGE_SECRETS_DIR'], 'BLOBBY'), 'rb').read()\n"
        "print('len=%d' % len(v), flush=True)\n"
        "print('sha=' + hashlib.sha256(v).hexdigest(), flush=True)\n"
    )
    built = build_tree(root)
    client = user_client(fabric, NORMAL_USER)
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
                "name": "blob-secret",
                "entrypoint": ["python", "main.py"],
                "secrets": ["BLOBBY"],
                "retry": {"max_attempts": 1},
            },
            "tree_id": str(built.tree.tree_id),
        },
    )
    assert posted.status_code == 201, posted.text
    job_id = posted.json()["job_id"]
    deadline = time.monotonic() + 120
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        job = fabric.call("GET", f"/v1/jobs/{job_id}", as_user=NORMAL_USER).json()
        if job["state"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.25)
    assert job["state"] == "succeeded", job
    log = fabric.call(
        "GET",
        f"/v1/jobs/{job_id}/logs",
        as_user=NORMAL_USER,
        params={"stream": "stdout"},
    ).text
    assert f"len={len(payload)}" in log, log
    assert "sha=" + hashlib.sha256(payload).hexdigest() in log, log


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_secret_value_is_in_no_response_header_log_or_bundle(
    fabric: Fabric, new_page: NewPage
) -> None:
    value = "Zq9-tester-r1-" + "x" * 40
    page, watch = new_page()
    seen: list[str] = []
    consoles: list[str] = []
    page.on("console", lambda m: consoles.append(m.text))

    def grab(r: Any) -> None:
        try:
            seen.append(json.dumps(dict(r.headers)) + r.text())
        except Exception:  # noqa: BLE001
            seen.append(json.dumps(dict(r.headers)))

    page.on("response", grab)
    sign_in(page, fabric, NORMAL_USER)
    page.goto(fabric.http_base + "/settings/secrets")
    page.get_by_role("button", name="Set a secret").click()
    d = page.get_by_role("dialog")
    d.get_by_label("Name", exact=True).fill("Unicode name é")
    d.get_by_label("Value", exact=True).fill(value)
    # an invalid name must not enable the button nor send anything
    expect(d.get_by_role("button", name="Store secret")).to_be_disabled()
    d.get_by_label("Name", exact=True).fill("tester_r1")
    # keyboard-only submit with Enter inside the value field
    d.get_by_label("Value", exact=True).press("Enter")
    expect(page.get_by_text("Secret TESTER_R1 stored", exact=True)).to_be_visible()
    # The password field kept nothing: reopen the dialog
    page.get_by_role("button", name="Set a secret").click()
    d = page.get_by_role("dialog")
    assert d.get_by_label("Value", exact=True).input_value() == ""
    assert d.get_by_label("Name", exact=True).input_value() == ""
    page.keyboard.press("Escape")
    page.reload()
    expect(page.get_by_role("cell", name="TESTER_R1", exact=True)).to_be_visible()
    assert value not in page.content()
    assert value not in "".join(seen)
    assert value not in "\n".join(consoles)
    assert value not in page.evaluate(
        "JSON.stringify([Object.entries(localStorage), Object.entries(sessionStorage),"
        " location.href, performance.getEntries().map(e => e.name)])"
    )
    # the secret list API has no value field anywhere
    listed = fabric.call("GET", "/v1/secrets", as_user=NORMAL_USER).text
    assert value not in listed
    watch.assert_clean()


# --------------------------------------------- data: names, honest 409/empty


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_data_values_match_the_endpoint_and_keyboard_only_pin_delete(
    fabric: Fabric, new_page: NewPage, tmp_path: Path
) -> None:
    pushed = _push_dataset(fabric, tmp_path, "kb-data-1.v2", files=3)
    ref = str(pushed["ref"])
    page, watch = new_page()
    sign_in(page, fabric)
    page.goto(fabric.http_base + "/settings/data")
    api = fabric.call("GET", "/v1/data").json()["inputs"]
    row = page.get_by_role("row").filter(has_text=ref)
    mine = next(i for i in api if i["ref"] == ref)
    expect(row).to_contain_text(str(mine["files"]))
    expect(row).to_contain_text("not pinned")

    # keyboard only: pin
    tab_to(page, f"Pin {ref}")
    page.keyboard.press("Enter")
    expect(row.get_by_role("button", name=f"Unpin {ref}")).to_be_visible()
    assert [
        i["pinned"]
        for i in fabric.call("GET", "/v1/data").json()["inputs"]
        if i["ref"] == ref
    ] == [True]
    # keyboard only: unpin
    tab_to(page, f"Unpin {ref}")
    page.keyboard.press("Enter")
    expect(row.get_by_role("button", name=f"Pin {ref}")).to_be_visible()

    # delete: Cancel keeps it (Escape), confirm removes
    tab_to(page, f"Delete {ref}")
    page.keyboard.press("Enter")
    dlg = page.get_by_role("dialog")
    expect(dlg).to_be_visible()
    page.keyboard.press("Escape")
    expect(dlg).to_be_hidden()
    assert [i["ref"] for i in fabric.call("GET", "/v1/data").json()["inputs"]] == [ref]
    # focus returns to the trigger (keyboard users are not dumped on <body>)
    expect(page.get_by_role("button", name=f"Delete {ref}")).to_be_focused()
    page.keyboard.press("Enter")
    expect(dlg).to_be_visible()
    expect(dlg.get_by_role("button", name="Cancel")).to_be_focused()
    page.keyboard.press("Tab")
    page.keyboard.press("Enter")
    expect(page.get_by_text("No named inputs yet.")).to_be_visible()
    assert fabric.call("GET", "/v1/data").json()["inputs"] == []
    watch.assert_clean()


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_users_never_see_other_users_or_admin_data(
    fabric: Fabric, new_page: NewPage
) -> None:
    # admin makes a secret + token; alice must not see them anywhere
    admin_client = user_client(fabric, ADMIN_USER)
    try:
        admin_client.put_secret("ADMIN_ONLY_SECRET", b"adminvalue123")
    finally:
        admin_client.close()
    admin_tokens = fabric.call("GET", "/v1/auth/tokens").json()["tokens"]
    admin_token_ids = [t["id"] for t in admin_tokens]
    assert admin_token_ids

    # API level: alice cannot list all, invites, or revoke admin's tokens
    for path in ("/v1/fabric/invites",):
        assert fabric.call("GET", path, as_user=NORMAL_USER).status_code == 403
    r = fabric.call(
        "GET", "/v1/auth/tokens", as_user=NORMAL_USER, params={"all": "true"}
    )
    if r.status_code == 200:
        assert not set(admin_token_ids) & {t["id"] for t in r.json()["tokens"]}
    else:
        assert r.status_code == 403
    r = fabric.call(
        "DELETE", f"/v1/auth/tokens/{admin_token_ids[0]}", as_user=NORMAL_USER
    )
    assert r.status_code in (403, 404), r.text
    assert fabric.call("GET", "/v1/auth/whoami").status_code == 200  # admin still valid
    r = fabric.call(
        "POST", "/v1/fabric/invites", as_user=NORMAL_USER, json={"ttl_s": 60}
    )
    assert r.status_code == 403

    page, watch = new_page()
    bodies: list[str] = []
    page.on("response", lambda r: bodies.append(r.url) if "/v1/" in r.url else None)
    texts: list[str] = []
    sign_in(page, fabric, NORMAL_USER)
    for path in ("", "/tokens", "/secrets", "/data", "/invites", "/users"):
        page.goto(fabric.http_base + "/settings" + path)
        expect(page.get_by_role("heading", level=1, name="Settings")).to_be_visible()
        expect(page.get_by_text("Loading…")).to_have_count(0)
        texts.append(page.inner_text("body"))
    blob = "\n".join(texts)
    assert "ADMIN_ONLY_SECRET" not in blob
    for tid in admin_token_ids:
        assert tid not in blob
    assert not [u for u in bodies if "/v1/fabric/invites" in u or "all=true" in u]
    # The admin-only section's forbidden state is honest, not an empty list
    page.goto(fabric.http_base + "/settings/invites")
    assert "No invites yet" not in page.inner_text("body")
    watch.assert_clean()


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_displayed_tokens_invites_and_controller_card_match_the_endpoints(
    fabric: Fabric, new_page: NewPage
) -> None:
    # two invites (one revoked) + a second token
    ids = [
        fabric.call("POST", "/v1/fabric/invites", json={"ttl_s": 600}).json()[
            "invite_id"
        ]
        for _ in range(2)
    ]
    assert fabric.call("POST", f"/v1/fabric/invites/{ids[0]}/revoke").status_code == 200
    page, watch = new_page()
    sign_in(page, fabric)
    page.goto(fabric.http_base + "/settings/invites")
    api = {
        i["id"]: i for i in fabric.call("GET", "/v1/fabric/invites").json()["invites"]
    }
    assert set(api) >= set(ids)
    for iid in ids:
        row = page.get_by_role("row").filter(has_text=iid)
        expect(row).to_contain_text(api[iid]["state"])
        if api[iid]["state"] != "pending":
            expect(row.get_by_role("button")).to_have_count(0)
    page.goto(fabric.http_base + "/settings/tokens")
    tok = fabric.call("GET", "/v1/auth/tokens").json()["tokens"]
    for t in tok:
        row = page.get_by_role("row").filter(has_text=t["id"])
        expect(row).to_contain_text(t["name"])
        expect(row).to_contain_text(t["state"])
    expect(page.get_by_role("row").filter(has_text="this session")).to_have_count(
        sum(1 for t in tok if t["current"])
    )
    page.goto(fabric.http_base + "/settings")
    health = fabric.get("/healthz")
    assert health is not None
    card = page.get_by_test_id("controller-card")
    expect(card).to_contain_text(str(health["status"]))
    who = fabric.call("GET", "/v1/auth/whoami").json()
    expect(page.get_by_test_id("account-card")).to_contain_text(who["user"]["name"])
    expect(page.get_by_test_id("account-card")).to_contain_text(who["user"]["role"])
    watch.assert_clean()


# --------------------------- item 0e: revoked agent exits clean (all paths)


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_revoked_agent_that_was_paused_exits_4_without_traceback_on_reconnect(
    fabric: Fabric,
) -> None:
    """The revoke arrives while the agent cannot hear it (SIGSTOP); on
    resume it must discover revocation on its own and exit cleanly, code 4."""
    (node,) = fabric.nodes
    node.pause()
    r = fabric.call("POST", f"/v1/nodes/{node.node_id}/revoke")
    assert r.status_code in (200, 202), r.text
    node.resume()
    code = node.process.wait_exit(60)
    node.process.expected_alive = False
    assert code == 4, code
    raw = "\n".join(node.process.logs[-1].raw_lines())
    assert "Traceback" not in raw
    events = [rec.get("event") for rec in node.process.logs[-1].records()]
    assert "agent.exited_with_error" not in events, events


# ------------------------------------------------ keyboard-only (item 27)


@pytest.mark.parametrize("fabric_options", [{"agents": 1}])
def test_every_settings_action_has_a_keyboard_only_path(
    fabric: Fabric, new_page: NewPage
) -> None:
    page, watch = new_page()
    sign_in(page, fabric)
    kb = page.keyboard

    # --- secret: open, type name+value, submit with Enter, delete with Enter
    page.goto(fabric.http_base + "/settings/secrets")
    expect(page.get_by_text("No secrets yet.")).to_be_visible()
    tab_to(page, "Set a secret")
    kb.press("Enter")
    expect(page.get_by_role("dialog")).to_be_visible()
    # focus is already inside the dialog, on the first field
    kb.type("kb_secret")
    kb.press("Tab")
    kb.type("keyboard-value-1")
    kb.press("Enter")
    expect(page.get_by_text("Secret KB_SECRET stored", exact=True)).to_be_visible()
    expect(page.get_by_role("dialog")).to_be_hidden()
    tab_to(page, "Delete secret KB_SECRET")
    kb.press("Enter")
    expect(
        page.get_by_role("dialog").get_by_role("button", name="Cancel")
    ).to_be_focused()
    kb.press("Tab")
    kb.press("Enter")
    expect(page.get_by_text("No secrets yet.")).to_be_visible()

    # --- invite: create with Enter, close with Escape, then revoke by keyboard
    page.goto(fabric.http_base + "/settings/invites")
    tab_to(page, "Create invite")
    kb.press("Enter")
    dlg = page.get_by_role("dialog")
    expect(dlg).to_be_visible()
    tab_to(page, "Create invite")  # the submit inside the dialog
    kb.press("Enter")
    expect(dlg.get_by_test_id("issued-invite")).to_be_visible()
    kb.press("Escape")
    expect(dlg).to_be_hidden()
    invite_id = fabric.call("GET", "/v1/fabric/invites").json()["invites"][0]["id"]
    tab_to(page, f"Revoke invite {invite_id}")
    kb.press("Enter")
    expect(
        page.get_by_role("dialog").get_by_role("button", name="Cancel")
    ).to_be_focused()
    kb.press("Tab")
    kb.press("Enter")
    expect(page.get_by_role("row").filter(has_text=invite_id)).to_contain_text(
        "revoked"
    )
    assert (
        fabric.call("GET", "/v1/fabric/invites").json()["invites"][0]["state"]
        == "revoked"
    )

    # --- token create: name + password by keyboard, Enter submits, Escape closes
    page.goto(fabric.http_base + "/settings/tokens")
    tab_to(page, "Create API token")
    kb.press("Enter")
    expect(page.get_by_role("dialog")).to_be_visible()
    kb.press("Control+A")
    kb.type("kb-token")
    kb.press("Tab")
    kb.type(fabric.password(ADMIN_USER))
    kb.press("Enter")
    expect(page.get_by_test_id("new-token")).to_be_visible()
    kb.press("Escape")
    expect(page.get_by_role("dialog")).to_be_hidden()
    names = [t["name"] for t in fabric.call("GET", "/v1/auth/tokens").json()["tokens"]]
    assert "kb-token" in names
    watch.assert_clean()
