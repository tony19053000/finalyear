"""V0.17 G3 (D391): the local console's HTTP surface, in process (httpx + ASGI).

* every request: ``Host`` must be ``127.0.0.1:<port>`` (DNS rebinding) - else 421;
  the dashboard's security headers on every answer, ``no-store`` on the API;
* access: the launcher's ``POST /launch-code`` needs the token (Bearer) and no
  browser Origin; ``POST /session`` trades a one-time code (never the token) for an
  ``HttpOnly; SameSite=Strict`` cookie and needs our Origin; codes are single
  use; wrong tries are throttled; every other route needs the cookie, writes
  also our Origin (CSRF);
* each endpoint's validation: policy (same validator and file as
  ``meshforge-agent policy``, both ways), node name, join (already joined, a
  non-LAN controller pick, plain-language errors), leave (confirm), logs
  (redacted), the SPA (no traversal, no HTML for a missing file).
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the agent extra")

import httpx
from typer.testing import CliRunner

from meshforge.agent.console import app as console_app
from meshforge.agent.console.app import ConsoleContext, create_app
from meshforge.agent.console.auth import (
    COOKIE_NAME,
    MAX_FAILURES_PER_MINUTE,
    ConsoleAuth,
)
from meshforge.agent.console.joining import JoinProblem, JoinProblemError
from meshforge.agent.console.logbuffer import LogBuffer
from meshforge.agent.console.supervisor import Supervisor
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.common.web_security import CSP

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

PORT = 47130
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"origin": BASE}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(
        data_dir=tmp_path / "data",
        agent=AgentSection(controller_address="127.0.0.1:47100"),
    )


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def ctx(settings: AgentSettings, clock: Clock, tmp_path: Path) -> ConsoleContext:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>console</title>")
    (dist / "assets" / "app-1.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside dist")
    logs = LogBuffer(100)
    return ConsoleContext(
        supervisor=Supervisor(settings, install_signals=False),
        auth=ConsoleAuth(
            port=PORT, session_ttl_s=600.0, launch_code_ttl_s=60.0, clock=clock
        ),
        logs=logs,
        dist=dist,
        cpu_threads=lambda: 8,
    )


@pytest_asyncio.fixture
async def http(ctx: ConsoleContext) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=create_app(ctx))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        yield client


async def _sign_in(http: httpx.AsyncClient, ctx: ConsoleContext) -> None:
    code = ctx.auth.new_launch_code()
    r = await http.post("/console/v1/session", json={"code": code}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    http.cookies.set(COOKIE_NAME, r.cookies[COOKIE_NAME], domain="127.0.0.1")


# ------------------------------------------------------------------ guards


async def test_wrong_host_is_refused_before_anything_else(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    await _sign_in(http, ctx)
    for host in ("evil.example", "localhost:47130", "127.0.0.1", "127.0.0.1:1"):
        for path in ("/console/v1/status", "/", "/console/v1/health"):
            r = await http.get(path, headers={"host": host})
            assert r.status_code == 421, (host, path)
            assert r.json()["error"]["code"] == "wrong_host"
            assert r.headers["content-security-policy"] == CSP
    assert (await http.get("/console/v1/status")).status_code == 200


async def test_security_headers_and_no_store(http: httpx.AsyncClient) -> None:
    r = await http.get("/console/v1/health")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    ui = await http.get("/")
    assert ui.headers["content-security-policy"] == CSP
    assert "no-store" not in ui.headers.get("cache-control", "")


async def test_everything_but_health_needs_the_session(http: httpx.AsyncClient) -> None:
    for method, path in (
        ("GET", "/console/v1/status"),
        ("GET", "/console/v1/policy"),
        ("GET", "/console/v1/checks"),
        ("GET", "/console/v1/logs"),
        ("GET", "/console/v1/node-name"),
        ("GET", "/console/v1/discover"),
        ("GET", "/console/v1/docker-access"),
        ("POST", "/console/v1/join"),
        ("POST", "/console/v1/leave"),
        ("POST", "/console/v1/restart"),
        ("POST", "/console/v1/setup-complete"),
        ("PUT", "/console/v1/policy"),
        ("PUT", "/console/v1/node-name"),
        ("DELETE", "/console/v1/session"),
    ):
        r = await http.request(method, path, json={}, headers=ORIGIN)
        assert r.status_code == 401, (method, path, r.status_code)
        assert r.json()["error"]["code"] == "signed_out"
    http.cookies.set(COOKIE_NAME, "forged", domain="127.0.0.1")
    assert (await http.get("/console/v1/status")).status_code == 401


async def test_writes_need_our_origin(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    await _sign_in(http, ctx)
    for origin in (None, "http://evil.example", "http://localhost:47130", "null"):
        headers = {} if origin is None else {"origin": origin}
        r = await http.post("/console/v1/restart", headers=headers)
        assert r.status_code == 403, origin
        r = await http.put("/console/v1/node-name", json={"name": "x"}, headers=headers)
        assert r.status_code == 403
    assert (await http.get("/console/v1/status")).status_code == 200  # reads: no Origin


# ------------------------------------------------------------------ access


async def test_launch_code_needs_the_token_and_no_browser(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    bearer = {"authorization": f"Bearer {ctx.auth.token}"}
    r = await http.post("/console/v1/launch-code", headers={**bearer, **ORIGIN})
    assert r.status_code == 403  # a page can never mint one
    r = await http.post(
        "/console/v1/launch-code", headers={"authorization": "Bearer no"}
    )
    assert r.status_code == 401
    r = await http.post("/console/v1/launch-code")
    assert r.status_code == 401
    r = await http.post("/console/v1/launch-code", headers=bearer)
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == f"{BASE}/#c={body['code']}"
    assert ctx.auth.token not in body["url"]


async def test_code_is_single_use_and_expires(
    http: httpx.AsyncClient, ctx: ConsoleContext, clock: Clock
) -> None:
    code = ctx.auth.new_launch_code()
    ok = await http.post("/console/v1/session", json={"code": code}, headers=ORIGIN)
    assert ok.status_code == 200
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    again = await http.post("/console/v1/session", json={"code": code}, headers=ORIGIN)
    assert again.status_code == 401 and again.json()["error"]["code"] == "link_expired"
    late = ctx.auth.new_launch_code()
    clock.now += 61
    r = await http.post("/console/v1/session", json={"code": late}, headers=ORIGIN)
    assert r.status_code == 401
    # the token itself is never a browser credential (it only buys codes)
    r = await http.post(
        "/console/v1/session", json={"token": ctx.auth.token}, headers=ORIGIN
    )
    assert r.status_code == 422
    # Origin is checked before the body is even read
    r = await http.post("/console/v1/session", content=b"not json")
    assert r.status_code == 403


async def test_session_expires_and_sign_out_ends_it(
    http: httpx.AsyncClient, ctx: ConsoleContext, clock: Clock
) -> None:
    await _sign_in(http, ctx)
    assert (await http.get("/console/v1/status")).status_code == 200
    clock.now += 601
    assert (await http.get("/console/v1/status")).status_code == 401
    await _sign_in(http, ctx)
    assert (await http.delete("/console/v1/session", headers=ORIGIN)).status_code == 200
    assert (await http.get("/console/v1/status")).status_code == 401


async def test_guessing_is_throttled_but_never_locks_the_owner_out(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    for _ in range(MAX_FAILURES_PER_MINUTE):
        r = await http.post(
            "/console/v1/session", json={"code": "nope"}, headers=ORIGIN
        )
        assert r.status_code == 401
    r = await http.post("/console/v1/session", json={"code": "nope"}, headers=ORIGIN)
    assert r.status_code == 429  # failed tries are slowed down ...
    good = ctx.auth.new_launch_code()
    r = await http.post("/console/v1/session", json={"code": good}, headers=ORIGIN)
    assert r.status_code == 200  # ... the right credential always works (G3-D1)
    bad = {"authorization": "Bearer wrong"}
    assert (await http.post("/console/v1/launch-code", headers=bad)).status_code == 429
    bearer = {"authorization": f"Bearer {ctx.auth.token}"}
    assert (
        await http.post("/console/v1/launch-code", headers=bearer)
    ).status_code == 200


async def test_origin_is_checked_before_the_session(
    http: httpx.AsyncClient,
) -> None:
    # no cookie at all: a cross-site write gets 403, not 401 (learns nothing)
    r = await http.post("/console/v1/restart", headers={"origin": "http://evil"})
    assert r.status_code == 403


async def test_origin_and_session_are_checked_before_the_body(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    """G3-R2-D1: a malformed body never turns a 403/401 into a 422."""
    writes = console_app._session_writes(create_app(ctx))
    assert {
        ("POST", "/console/v1/join"),
        ("PUT", "/console/v1/policy"),
        ("PUT", "/console/v1/node-name"),
        ("POST", "/console/v1/leave"),
        ("POST", "/console/v1/restart"),
        ("POST", "/console/v1/setup-complete"),
        ("DELETE", "/console/v1/session"),
    } <= writes
    # the sign-in and the launcher's route have their own rule
    assert ("POST", "/console/v1/session") not in writes
    assert ("POST", "/console/v1/launch-code") not in writes
    bad = b"{not json"
    for method, path in sorted(writes):
        r = await http.request(
            method, path, headers={"origin": "http://evil.example"}, content=bad
        )
        assert r.status_code == 403, (method, path)
        assert r.json()["error"]["code"] == "bad_origin"
        r = await http.request(method, path, headers=ORIGIN, content=bad)
        assert r.status_code == 401, (method, path)
        assert r.json()["error"]["code"] == "signed_out"
    # signed in, the body is validated as before
    await _sign_in(http, ctx)
    r = await http.post("/console/v1/leave", headers=ORIGIN, content=bad)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid"


# ---------------------------------------------------------------- endpoints


async def test_status_before_the_join(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    await _sign_in(http, ctx)
    body = (await http.get("/console/v1/status")).json()
    assert body["phase"] == "waiting_for_join"
    assert body["joined"] is False and body["setup_complete"] is False
    assert body["node_id"] is None and body["connection"] is None
    assert body["install_mode"] in ("user", "system")


async def test_policy_round_trip_equals_the_cli(
    http: httpx.AsyncClient,
    ctx: ConsoleContext,
    settings: AgentSettings,
    tmp_path: Path,
) -> None:
    from meshforge.agent.main import app as cli

    await _sign_in(http, ctx)
    before = (await http.get("/console/v1/policy")).json()
    assert before["policy"]["accept_jobs"] is False and before["cpu_threads"] == 8
    r = await http.put(
        "/console/v1/policy",
        json={
            "values": {
                "accept_jobs": True,
                "max_cpu_percent": 50,
                "max_gpu_jobs": 0,
                "max_ram_bytes": 4 * 1024**3,
            }
        },
        headers=ORIGIN,
    )
    assert r.status_code == 200, r.text
    assert r.json()["restart_needed"] == ["accept_jobs"]
    config = tmp_path / "config.toml"
    config.write_text(
        f'data_dir = "{settings.data_dir.as_posix()}"\n'
        '[agent]\ncontroller_address = "127.0.0.1:47100"\n'
    )
    shown = CliRunner().invoke(
        cli, ["policy", "show", "--json", "--config", str(config)]
    )
    assert shown.exit_code == 0, shown.output
    cli_view = json.loads(shown.output)
    console_view = (await http.get("/console/v1/policy")).json()
    for key in ("policy", "max_cpu_jobs", "max_gpu_jobs", "live_keys", "invalid"):
        assert console_view[key] == cli_view[key], key
    assert console_view["policy"]["max_cpu_percent"] == 50
    # and the other way: the CLI writes, the console reads
    done = CliRunner().invoke(
        cli, ["policy", "set", "max_cpu_percent=none", "--config", str(config)]
    )
    assert done.exit_code == 0, done.output
    after = (await http.get("/console/v1/policy")).json()
    assert after["policy"]["max_cpu_percent"] is None
    assert after["policy"]["accept_jobs"] is True  # merged, not replaced


async def test_policy_validation(http: httpx.AsyncClient, ctx: ConsoleContext) -> None:
    await _sign_in(http, ctx)
    for values in (
        {"max_cpu_percent": 0},
        {"max_cpu_percent": 101},
        {"accept_jobs": None},
        {"max_cpu_jobs": None},
        {"no_such_key": True},
        {"max_ram_bytes": -1},
        {"max_cpu_jobs": 64, "max_gpu_jobs": 64},
    ):
        r = await http.put(
            "/console/v1/policy", json={"values": values}, headers=ORIGIN
        )
        assert r.status_code in (400, 422), (values, r.status_code)
        assert r.json()["error"]["message"]
    assert (await http.get("/console/v1/policy")).json()["live_keys"] == []


async def test_node_name(http: httpx.AsyncClient, ctx: ConsoleContext) -> None:
    await _sign_in(http, ctx)
    r = await http.get("/console/v1/node-name")
    assert r.json()["source"] == "none" and r.json()["name"] == ""
    for bad in ("tab\there", "line\nbreak", "x" * 65, "\x1b[31mred"):
        r = await http.put("/console/v1/node-name", json={"name": bad}, headers=ORIGIN)
        assert r.status_code == 422, bad
    r = await http.put(
        "/console/v1/node-name", json={"name": "  Lab A  "}, headers=ORIGIN
    )
    assert r.status_code == 200 and r.json() == {
        "name": "Lab A",
        "source": "console",
        "hostname": r.json()["hostname"],
        "applies_after_restart": False,
    }
    assert (await http.get("/console/v1/status")).json()["node_name"] == "Lab A"


async def test_join_validation_and_plain_errors(
    http: httpx.AsyncClient, ctx: ConsoleContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _sign_in(http, ctx)
    seen: list[tuple[str, str | None]] = []

    async def fake_join(settings: Any, code: str, *, host: str | None = None) -> Any:
        seen.append((code, host))
        raise JoinProblemError(JoinProblem("expired", "This code has expired.", "Ask."))

    monkeypatch.setattr(console_app, "join_from_console", fake_join)
    r = await http.post(
        "/console/v1/join",
        json={"code": "mf1-x", "controller_host": "192.168.1.5"},
        headers=ORIGIN,
    )
    assert r.status_code == 400
    assert r.json()["error"] == {
        "code": "expired",
        "message": "This code has expired.",
        "fix": "Ask.",
    }
    assert seen == [("mf1-x", "192.168.1.5")]
    r = await http.post("/console/v1/join", json={"code": ""}, headers=ORIGIN)
    assert r.status_code == 422
    r = await http.post("/console/v1/join", json={"code": "x" * 600}, headers=ORIGIN)
    assert r.status_code == 422
    # already enrolled -> 409, the code is never sent
    ident = ctx.settings.identity_dir
    ident.mkdir(parents=True)
    for name in ("node.key", "node.crt", "ca.crt"):
        (ident / name).write_text("x")
    r = await http.post("/console/v1/join", json={"code": "mf1-y"}, headers=ORIGIN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "already_joined"
    assert len(seen) == 1


async def test_join_refuses_a_non_lan_controller_pick(settings: AgentSettings) -> None:
    from meshforge.agent.console.joining import join_from_console

    with pytest.raises(JoinProblemError) as info:
        await join_from_console(settings, "mf1-x", host="8.8.8.8")
    assert info.value.problem.code == "unreachable"
    assert not settings.identity_dir.exists()  # nothing created


async def test_leave_needs_confirmation_and_a_fabric(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    await _sign_in(http, ctx)
    assert (
        await http.post("/console/v1/leave", json={}, headers=ORIGIN)
    ).status_code == 422
    r = await http.post("/console/v1/leave", json={"confirm": "yes"}, headers=ORIGIN)
    assert r.status_code == 422
    r = await http.post("/console/v1/leave", json={"confirm": "leave"}, headers=ORIGIN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "not_joined"


async def test_setup_complete_is_remembered(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    await _sign_in(http, ctx)
    # Q26 server-side: not before the owner's policy choice was saved
    r = await http.post("/console/v1/setup-complete", headers=ORIGIN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "policy_not_saved"
    assert (await http.get("/console/v1/status")).json()["setup_complete"] is False
    saved = await http.put(
        "/console/v1/policy", json={"values": {"accept_jobs": False}}, headers=ORIGIN
    )
    assert saved.status_code == 200
    r = await http.post("/console/v1/setup-complete", headers=ORIGIN)
    assert r.status_code == 200
    assert (await http.get("/console/v1/status")).json()["setup_complete"] is True
    marker = ctx.settings.data_dir / "console-setup-done"
    assert marker.is_file()
    if sys.platform != "win32":
        assert marker.stat().st_mode & 0o077 == 0


async def test_logs_are_redacted_and_incremental(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    assert ctx.logs is not None
    log = logging.getLogger("test.console")
    log.addHandler(ctx.logs)
    log.setLevel(logging.INFO)
    try:
        log.info("first", extra={"password": "hunter2"})
        log.warning("url http://user:s3cret@example.org token=abcdef")
    finally:
        log.removeHandler(ctx.logs)
    await _sign_in(http, ctx)
    body = (await http.get("/console/v1/logs?limit=10")).json()
    text = json.dumps(body)
    assert "hunter2" not in text and "s3cret" not in text and "abcdef" not in text
    assert [line["event"] for line in body["lines"]][-2:][0] == "first"
    last = body["lines"][-1]["seq"]
    newer = (await http.get(f"/console/v1/logs?after={last}")).json()
    assert newer["lines"] == []
    assert (await http.get("/console/v1/logs?limit=0")).status_code == 422


async def test_restart_is_handed_to_the_supervisor(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    await _sign_in(http, ctx)
    r = await http.post("/console/v1/restart", headers=ORIGIN)
    assert r.status_code == 200
    assert ctx.supervisor._pending == "restart"


async def test_docker_access_explains_instead_of_doing(
    http: httpx.AsyncClient, ctx: ConsoleContext
) -> None:
    await _sign_in(http, ctx)
    body = (await http.get("/console/v1/docker-access")).json()
    expected = (
        "net localgroup docker-users"
        if sys.platform == "win32"
        else "usermod -aG docker meshforge"
    )
    assert expected in body["command"]
    assert body["menu_action"]


# ---------------------------------------------------------------------- SPA


async def test_spa_files_routes_and_no_traversal(http: httpx.AsyncClient) -> None:
    index = await http.get("/")
    assert index.status_code == 200 and "console" in index.text
    assert (await http.get("/settings")).text == index.text  # client route
    asset = await http.get("/assets/app-1.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    for path in (
        "/assets/missing.js",
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/.hidden",
    ):
        r = await http.get(path)
        assert "outside dist" not in r.text, path
        if path.endswith((".js", ".txt")):
            assert r.status_code == 404, path
    assert (await http.post("/")).status_code == 405
    assert (await http.get("/console/v1/nope")).status_code == 404


async def test_not_built_page(ctx: ConsoleContext, tmp_path: Path) -> None:
    ctx.dist = tmp_path / "empty"
    transport = httpx.ASGITransport(app=create_app(ctx))
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        r = await client.get("/")
        assert r.status_code == 200 and "not built" in r.text
        assert (await client.get("/x.js")).status_code == 404


async def test_openapi_document_lists_the_console_api() -> None:
    doc = console_app.openapi_document()
    paths = set(doc["paths"])
    assert {
        "/console/v1/status",
        "/console/v1/join",
        "/console/v1/policy",
        "/console/v1/node-name",
        "/console/v1/checks",
        "/console/v1/logs",
        "/console/v1/leave",
        "/console/v1/restart",
        "/console/v1/launch-code",
        "/console/v1/session",
    } <= paths
    assert not any(p.startswith("/v1/") for p in paths)
