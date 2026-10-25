# ruff: noqa: S104, S103, PERF401
"""V0.17 G3 tester: the console's access rules against a REAL uvicorn on a real
loopback socket (raw HTTP, so odd Host / Origin / header shapes are sent
exactly as an attacker's browser or script could).

Criteria (brief 5c.2, 5c.6): Host = 127.0.0.1:47130 only (421); Origin on writes
(403); HttpOnly SameSite=Strict cookie; wrong tries throttled (429); launch code
single use, 60 s; token file 0640/0600 and removed on exit; bind 127.0.0.1 only.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the agent extra")

from meshforge.agent.console.auth import MAX_FAILURES_PER_MINUTE
from meshforge.agent.console.server import bind_console_socket, run_with_console
from meshforge.agent.console.supervisor import Supervisor
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.common.errors import ConfigError, ExitCode

pytestmark = [pytest.mark.component, pytest.mark.security, pytest.mark.asyncio]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Live:
    port: int
    token_dir: Path
    token: str
    settings: AgentSettings
    supervisor: Supervisor
    launch_ttl: float

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"


async def raw(
    port: int,
    method: str,
    path: str,
    *,
    headers: list[tuple[str, str]] | None = None,
    body: bytes = b"",
    host: str | None = "default",
) -> tuple[int, dict[str, str], bytes, list[str]]:
    """One HTTP/1.1 request over a bare socket; returns status, headers
    (lower-cased, last wins), body and the raw Set-Cookie values."""
    lines = [f"{method} {path} HTTP/1.1"]
    if host == "default":
        host = f"127.0.0.1:{port}"
    if host is not None:
        lines.append(f"Host: {host}")
    for k, v in headers or []:
        lines.append(f"{k}: {v}")
    if body:
        lines.append(f"Content-Length: {len(body)}")
        lines.append("Content-Type: application/json")
    lines.append("Connection: close")
    data = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(data)
    await writer.drain()
    blob = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    head, _, payload = blob.partition(b"\r\n\r\n")
    status_line, *hlines = head.decode("latin-1").split("\r\n")
    hdrs: dict[str, str] = {}
    cookies: list[str] = []
    for h in hlines:
        k, _, v = h.partition(":")
        hdrs[k.strip().lower()] = v.strip()
        if k.strip().lower() == "set-cookie":
            cookies.append(v.strip())
    return int(status_line.split()[1]), hdrs, payload, cookies


@pytest_asyncio.fixture
async def live(tmp_path: Path) -> AsyncIterator[Live]:
    port = _free_port()
    token_dir = tmp_path / "console"
    settings = AgentSettings(
        data_dir=tmp_path / "data",
        agent=AgentSection(
            controller_address="127.0.0.1:1",
            console={
                "enabled": True,
                "port": port,
                "token_dir": token_dir,
                "launch_code_ttl_s": 5.0,
            },
        ),
    )
    sup = Supervisor(settings, install_signals=False, poll_s=0.2)
    task = asyncio.create_task(run_with_console(settings, supervisor=sup))
    for _ in range(200):
        if (token_dir / "token").is_file():
            try:
                await raw(port, "GET", "/console/v1/health")
                break
            except OSError:
                pass
        await asyncio.sleep(0.05)
    token = (token_dir / "token").read_text().strip()
    try:
        yield Live(port, token_dir, token, settings, sup, 5.0)
    finally:
        sup.request_shutdown()
        assert await asyncio.wait_for(task, 60) == ExitCode.OK
    assert not (token_dir / "token").exists()


async def _code(live: Live) -> str:
    st, _, body, _ = await raw(
        live.port,
        "POST",
        "/console/v1/launch-code",
        headers=[("Authorization", f"Bearer {live.token}")],
    )
    assert st == 200, body
    return str(json.loads(body)["code"])


async def _session(live: Live) -> str:
    code = await _code(live)
    st, _, body, cookies = await raw(
        live.port,
        "POST",
        "/console/v1/session",
        headers=[("Origin", live.origin)],
        body=json.dumps({"code": code}).encode(),
    )
    assert st == 200, body
    return cookies[0].split(";")[0]


# ---------------------------------------------------------------- Host guard


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:47130",
        "localhost:47130",
        "127.0.0.1",  # no port
        "127.0.0.1:1",
        "127.0.0.1:47130.evil.example",
        "127.0.0.1.evil.example:47130",
        "[::1]:47130",
        "0.0.0.0:47130",
        "127.0.0.2:47130",
        "127.1:47130",
        "2130706433:47130",
        "127.0.0.1:47130@evil.example",
        "evil.example#127.0.0.1:47130",
        "127.0.0.1:047130",
        "127.0.0.1:47130,evil.example",
        "",
    ],
)
async def test_rebinding_host_values_are_421_on_every_route(
    live: Live, host: str
) -> None:
    cookie = await _session(live)
    # the real port number is random here: map the template to it
    host = (
        host.replace("47130", str(live.port)) if host.startswith("127.0.0.1:") else host
    )
    for path in ("/", "/console/v1/status", "/console/v1/health", "/assets/x.js"):
        st, _, body, _ = await raw(
            live.port, "GET", path, host=host, headers=[("Cookie", cookie)]
        )
        if host == f"127.0.0.1:{live.port}":
            continue
        assert st == 421, (host, path, st, body[:100])


async def test_missing_host_header_is_refused(live: Live) -> None:
    st, *_ = await raw(live.port, "GET", "/console/v1/health", host=None)
    assert st in (400, 421)


async def test_duplicate_host_headers_cannot_smuggle_the_good_one(live: Live) -> None:
    cookie = await _session(live)
    st, *_ = await raw(
        live.port,
        "GET",
        "/console/v1/status",
        host="evil.example",
        headers=[("Host", f"127.0.0.1:{live.port}"), ("Cookie", cookie)],
    )
    assert st in (400, 421)
    st, *_ = await raw(
        live.port,
        "GET",
        "/console/v1/status",
        host=f"127.0.0.1:{live.port}",
        headers=[("Host", "evil.example"), ("Cookie", cookie)],
    )
    assert st in (400, 421), "second Host header ignored"


async def test_absolute_form_request_target_does_not_bypass_host(live: Live) -> None:
    cookie = await _session(live)
    st, *_ = await raw(
        live.port,
        "GET",
        f"http://127.0.0.1:{live.port}/console/v1/status",
        host="evil.example",
        headers=[("Cookie", cookie)],
    )
    assert st == 421


async def test_x_forwarded_and_x_original_host_do_not_change_the_check(
    live: Live,
) -> None:
    st, *_ = await raw(
        live.port,
        "GET",
        "/console/v1/health",
        host="evil.example",
        headers=[
            ("X-Forwarded-Host", f"127.0.0.1:{live.port}"),
            ("Forwarded", f"host=127.0.0.1:{live.port}"),
        ],
    )
    assert st == 421


async def test_websocket_upgrade_is_refused(live: Live) -> None:
    st, *_ = await raw(
        live.port,
        "GET",
        "/console/v1/status",
        headers=[
            ("Upgrade", "websocket"),
            ("Connection", "Upgrade"),
            ("Sec-WebSocket-Key", "dGhlIHNhbXBsZSBub25jZQ=="),
            ("Sec-WebSocket-Version", "13"),
        ],
    )
    assert st in (400, 401, 426)


# --------------------------------------------------------------- Origin/CSRF


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://evil.example",
        "http://localhost:{port}",
        "http://127.0.0.1",
        "https://127.0.0.1:{port}",
        "http://127.0.0.1:{port}.evil.example",
        "http://127.0.0.1:{port}/",
        "",
    ],
)
async def test_every_write_needs_exactly_our_origin(
    live: Live, origin: str | None
) -> None:
    cookie = await _session(live)
    base_headers = [("Cookie", cookie)]
    if origin is not None:
        base_headers.append(("Origin", origin.replace("{port}", str(live.port))))
    writes = [
        ("PUT", "/console/v1/policy", {"values": {"accept_jobs": True}}),
        ("PUT", "/console/v1/node-name", {"name": "x"}),
        ("POST", "/console/v1/join", {"code": "mf1-x"}),
        ("POST", "/console/v1/leave", {"confirm": "leave"}),
        ("POST", "/console/v1/restart", {}),
        ("POST", "/console/v1/setup-complete", {}),
        ("DELETE", "/console/v1/session", {}),
    ]
    for method, path, payload in writes:
        st, _, body, _ = await raw(
            live.port,
            method,
            path,
            headers=base_headers,
            body=json.dumps(payload).encode(),
        )
        assert st == 403, (method, path, origin, st, body[:120])
    # nothing changed
    assert not (live.settings.data_dir / "node_name").exists()


async def test_session_exchange_without_or_with_foreign_origin_is_403(
    live: Live,
) -> None:
    code = await _code(live)
    for hdrs in ([], [("Origin", "http://evil.example")], [("Origin", "null")]):
        st, *_ = await raw(
            live.port,
            "POST",
            "/console/v1/session",
            headers=hdrs,
            body=json.dumps({"code": code}).encode(),
        )
        assert st == 403
    # the code survived the refused attempts (not burned by a foreign page)
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/session",
        headers=[("Origin", live.origin)],
        body=json.dumps({"code": code}).encode(),
    )
    assert st == 200


async def test_launch_code_endpoint_refuses_a_browser_origin(live: Live) -> None:
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/launch-code",
        headers=[
            ("Authorization", f"Bearer {live.token}"),
            ("Origin", "http://evil.example"),
        ],
    )
    assert st == 403


async def test_reads_with_foreign_origin_still_need_the_cookie(live: Live) -> None:
    st, *_ = await raw(
        live.port,
        "GET",
        "/console/v1/status",
        headers=[("Origin", "http://evil.example")],
    )
    assert st == 401


# -------------------------------------------------------------- cookie flags


async def test_cookie_flags(live: Live) -> None:
    code = await _code(live)
    st, hdrs, _, cookies = await raw(
        live.port,
        "POST",
        "/console/v1/session",
        headers=[("Origin", live.origin)],
        body=json.dumps({"code": code}).encode(),
    )
    assert st == 200 and len(cookies) == 1
    low = cookies[0].lower()
    assert low.startswith("mf_console=")
    assert "httponly" in low
    assert "samesite=strict" in low
    assert "path=/" in low
    assert "max-age=" in low
    assert "domain=" not in low  # host-only
    assert hdrs["cache-control"] == "no-store"
    # the cookie value is not the token, nor the launch code
    value = cookies[0].split(";")[0].split("=", 1)[1]
    assert value not in (live.token, code) and len(value) >= 32


async def test_sign_out_kills_the_session_server_side(live: Live) -> None:
    cookie = await _session(live)
    st, *_ = await raw(
        live.port, "GET", "/console/v1/status", headers=[("Cookie", cookie)]
    )
    assert st == 200
    st, *_ = await raw(
        live.port,
        "DELETE",
        "/console/v1/session",
        headers=[("Cookie", cookie), ("Origin", live.origin)],
    )
    assert st == 200
    st, *_ = await raw(
        live.port, "GET", "/console/v1/status", headers=[("Cookie", cookie)]
    )
    assert st == 401, "a signed-out cookie replayed still works"


async def test_forged_and_garbage_cookies(live: Live) -> None:
    for c in (
        "mf_console=",
        "mf_console=abc",
        "mf_console=" + "A" * 5000,
        "mf_console=\x01",
    ):
        st, *_ = await raw(
            live.port, "GET", "/console/v1/status", headers=[("Cookie", c)]
        )
        assert st in (400, 401)
    # the token is not a session cookie
    st, *_ = await raw(
        live.port,
        "GET",
        "/console/v1/status",
        headers=[("Cookie", f"mf_console={live.token}")],
    )
    assert st == 401


# ------------------------------------------------------------ launch code


async def test_launch_code_is_single_use_and_replay_fails(live: Live) -> None:
    code = await _code(live)
    body = json.dumps({"code": code}).encode()
    h = [("Origin", live.origin)]
    assert (await raw(live.port, "POST", "/console/v1/session", headers=h, body=body))[
        0
    ] == 200
    for _ in range(3):
        assert (
            await raw(live.port, "POST", "/console/v1/session", headers=h, body=body)
        )[0] == 401


async def test_launch_code_expires(live: Live) -> None:
    code = await _code(live)  # ttl is 5 s in this fixture (the minimum allowed)
    await asyncio.sleep(live.launch_ttl + 0.6)
    st, _, body, cookies = await raw(
        live.port,
        "POST",
        "/console/v1/session",
        headers=[("Origin", live.origin)],
        body=json.dumps({"code": code}).encode(),
    )
    assert st == 401 and not cookies, body


async def test_launch_code_needs_the_token_and_wrong_tokens_never_succeed(
    live: Live,
) -> None:
    for auth in (None, "Bearer ", "Bearer wrong", f"Basic {live.token}", live.token):
        hdrs = [] if auth is None else [("Authorization", auth)]
        st, *_ = await raw(live.port, "POST", "/console/v1/launch-code", headers=hdrs)
        assert st in (401, 429)


async def test_code_and_token_in_url_query_are_not_accepted(live: Live) -> None:
    code = await _code(live)
    st, *_ = await raw(
        live.port, "GET", f"/console/v1/status?code={code}&token={live.token}"
    )
    assert st == 401


async def test_token_is_never_a_browser_credential(
    live: Live,
) -> None:
    # fix round 1 (Lead item 3): the `#t=` sign-in path is gone; the token only
    # buys launch codes (POST /launch-code with Bearer, no browser Origin)
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/session",
        headers=[("Origin", live.origin)],
        body=json.dumps({"token": live.token}).encode(),
    )
    assert st == 422
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/session",
        headers=[("Origin", live.origin)],
        body=json.dumps({"token": live.token[:-1] + "x"}).encode(),
    )
    assert st == 422


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"null",
        b"[]",
        b'{"code": 1}',
        b'{"code": null, "token": null}',
        b'{"x": 1}',
        b"\xff\xfe",
    ],
)
async def test_exchange_malformed_bodies_never_sign_in(
    live: Live, payload: bytes
) -> None:
    st, _, _, cookies = await raw(
        live.port,
        "POST",
        "/console/v1/session",
        headers=[("Origin", live.origin)],
        body=payload,
    )
    assert st in (400, 401, 422) and not cookies


async def test_wrong_tries_are_throttled_but_the_right_one_still_works(
    live: Live,
) -> None:
    h = [("Origin", live.origin)]
    seen: list[int] = []
    for i in range(MAX_FAILURES_PER_MINUTE + 5):
        st, *_ = await raw(
            live.port,
            "POST",
            "/console/v1/session",
            headers=h,
            body=json.dumps({"code": f"bad-{i}"}).encode(),
        )
        seen.append(st)
    assert seen[0] == 401
    assert 429 in seen, f"no throttle after {len(seen)} wrong tries: {set(seen)}"
    assert seen.index(429) <= MAX_FAILURES_PER_MINUTE + 1
    # fix round 1 (G3-D1): the credential is checked first; only failed tries
    # meet the throttle, so the owner's right token still gets a code
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/launch-code",
        headers=[("Authorization", "Bearer still-wrong")],
    )
    assert st == 429
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/launch-code",
        headers=[("Authorization", f"Bearer {live.token}")],
    )
    assert st == 200


# ---------------------------------------------------------- token file / bind


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
async def test_token_file_is_owner_only_in_a_plain_folder(live: Live) -> None:
    mode = stat.S_IMODE((live.token_dir / "token").stat().st_mode)
    assert mode == 0o600, oct(mode)
    assert stat.S_IMODE(live.token_dir.stat().st_mode) == 0o700
    assert live.token and len(live.token) >= 40


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
async def test_token_file_inherits_group_only_when_folder_is_setgid(
    tmp_path: Path,
) -> None:
    from meshforge.agent.console.auth import write_token_file

    d = tmp_path / "shared"
    d.mkdir(mode=0o750)
    os.chmod(d, 0o2750)
    p = write_token_file(d, "tok")
    assert stat.S_IMODE(p.stat().st_mode) == 0o640  # never world-readable
    d2 = tmp_path / "plain"
    d2.mkdir(mode=0o755)  # a permissive folder must not leak the file
    p2 = write_token_file(d2, "tok")
    assert stat.S_IMODE(p2.stat().st_mode) & 0o077 == 0


async def test_token_rotates_on_every_start(tmp_path: Path) -> None:
    tokens = []
    for _ in range(2):
        port = _free_port()
        d = tmp_path / "console"
        settings = AgentSettings(
            data_dir=tmp_path / "data",
            agent=AgentSection(
                controller_address="127.0.0.1:1",
                console={"enabled": True, "port": port, "token_dir": d},
            ),
        )
        sup = Supervisor(settings, install_signals=False, poll_s=0.2)
        task = asyncio.create_task(run_with_console(settings, supervisor=sup))
        for _ in range(200):
            if (d / "token").is_file():
                break
            await asyncio.sleep(0.05)
        tokens.append((d / "token").read_text().strip())
        sup.request_shutdown()
        assert await asyncio.wait_for(task, 60) == ExitCode.OK
    assert tokens[0] != tokens[1]


async def test_listens_on_loopback_only(live: Live) -> None:
    import psutil

    me = psutil.Process()
    listens = [
        c
        for c in me.net_connections(kind="inet")
        if c.status == "LISTEN" and c.laddr.port == live.port
    ]
    assert listens, "no listener found"
    assert {c.laddr.ip for c in listens} == {"127.0.0.1"}
    # and a connection to a non-loopback address of this machine is refused
    for ip in _other_local_ips():
        with pytest.raises(OSError):
            socket.create_connection((ip, live.port), timeout=2).close()


def _other_local_ips() -> list[str]:
    import psutil

    out = []
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith("127."):
                out.append(a.address)
    return out


async def test_bind_guard_rejects_non_loopback_config() -> None:
    for bad in ("0.0.0.0", "localhost", "::1", "192.168.1.5", "", "127.0.0.2"):
        with pytest.raises(ValueError):
            AgentSection(console={"bind": bad})


async def test_port_taken_is_a_config_error_not_a_crash(live: Live) -> None:
    with pytest.raises(ConfigError):
        bind_console_socket(live.port)


async def test_agent_survives_an_unwritable_token_dir(tmp_path: Path) -> None:
    ro = tmp_path / "ro"
    ro.mkdir()
    (ro / "token").mkdir()  # a directory where the file should go
    port = _free_port()
    settings = AgentSettings(
        data_dir=tmp_path / "data",
        agent=AgentSection(
            controller_address="127.0.0.1:1",
            console={"enabled": True, "port": port, "token_dir": ro},
        ),
    )
    sup = Supervisor(settings, install_signals=False, poll_s=0.2)
    task = asyncio.create_task(run_with_console(settings, supervisor=sup))
    await asyncio.sleep(1.0)
    assert not task.done(), "agent died because the console could not publish its token"
    # and the port was released (no half-open console)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    sup.request_shutdown()
    assert await asyncio.wait_for(task, 60) == ExitCode.OK


# --------------------------------------------------------- misc hardening


async def test_security_headers_on_errors_and_ui(live: Live) -> None:
    for path in (
        "/",
        "/console/v1/status",
        "/nope/../../etc/passwd",
        "/console/v1/zzz",
    ):
        st, hdrs, *_ = await raw(live.port, "GET", path)
        assert "content-security-policy" in hdrs, (path, st)
        assert hdrs.get("x-frame-options") == "DENY"


@pytest.mark.parametrize(
    "path",
    [
        "/..%2f..%2fetc/passwd",
        "/%2e%2e/%2e%2e/etc/passwd",
        "/assets/..%2f..%2f..%2fetc%2fpasswd",
        "/%00",
        "/.env",
        "/assets/.hidden",
    ],
)
async def test_static_serving_has_no_traversal(live: Live, path: str) -> None:
    st, _, body, _ = await raw(live.port, "GET", path)
    assert b"root:" not in body
    assert st in (200, 400, 404)


async def test_oversized_body_does_not_take_the_console_down(live: Live) -> None:
    cookie = await _session(live)
    big = json.dumps({"name": "x" * 5_000_000}).encode()
    st, *_ = await raw(
        live.port,
        "PUT",
        "/console/v1/node-name",
        headers=[("Cookie", cookie), ("Origin", live.origin)],
        body=big,
    )
    assert st in (413, 422, 400)
    t0 = time.monotonic()
    st, *_ = await raw(live.port, "GET", "/console/v1/health")
    assert st == 200 and time.monotonic() - t0 < 5


async def test_wrong_tries_by_a_stranger_do_not_lock_the_owner_out(live: Live) -> None:
    for i in range(MAX_FAILURES_PER_MINUTE + 5):
        await raw(
            live.port,
            "POST",
            "/console/v1/launch-code",
            headers=[("Authorization", f"Bearer wrong-{i}")],
        )
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/launch-code",
        headers=[("Authorization", f"Bearer {live.token}")],
    )
    assert st == 200, f"owner locked out: {st}"
    for i in range(MAX_FAILURES_PER_MINUTE + 5):
        await raw(
            live.port,
            "POST",
            "/console/v1/session",
            headers=[("Origin", live.origin)],
            body=json.dumps({"code": f"wrong-{i}"}).encode(),
        )
    code_st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/session",
        headers=[("Origin", live.origin)],
        body=json.dumps({"code": await _code(live)}).encode(),
    )
    assert code_st == 200, f"owner locked out of the session exchange: {code_st}"
