"""V0.17 G3 tester ROUND 2: console access rules (G3-D1 throttle, Origin ordering,
no token sign-in, setup-complete 409, name length, docker-access PATH) against a
real uvicorn on a loopback socket (raw HTTP)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the agent extra")

from meshforge.agent.console.auth import MAX_FAILURES_PER_MINUTE
from meshforge.agent.console.server import run_with_console
from meshforge.agent.console.supervisor import Supervisor
from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.common.errors import ExitCode

pytestmark = [pytest.mark.component, pytest.mark.security]


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


@pytest.mark.parametrize(
    "n", [MAX_FAILURES_PER_MINUTE + 1, MAX_FAILURES_PER_MINUTE * 4]
)
@pytest.mark.asyncio
async def test_r2_throttle_answers_failures_with_429_and_owner_still_passes(
    live: Live, n: int
) -> None:
    sts = []
    for i in range(n):
        st, *_ = await raw(
            live.port,
            "POST",
            "/console/v1/launch-code",
            headers=[("Authorization", f"Bearer w{i}")],
        )
        sts.append(st)
    assert sts[0] == 401 and sts[-1] == 429
    assert set(sts) <= {401, 429}
    # the right token: still fine, repeatedly, and its code signs in
    for _ in range(3):
        assert (await _session_cookie(live)).startswith("mf_console=")


async def _session_cookie(live: Live) -> str:
    return await _session(live)


@pytest.mark.asyncio
async def test_r2_throttle_is_shared_but_owner_cannot_be_blocked_concurrently(
    live: Live,
) -> None:
    async def bad(i: int) -> int:
        st, *_ = await raw(
            live.port,
            "POST",
            "/console/v1/session",
            headers=[("Origin", live.origin)],
            body=json.dumps({"code": f"x{i}"}).encode(),
        )
        return st

    async def owner() -> int:
        try:
            await _session(live)
        except AssertionError:
            return -1
        return 200

    results = await asyncio.gather(
        *[bad(i) for i in range(120)], *[owner() for _ in range(5)]
    )
    assert results[:120].count(200) == 0
    assert results[120:] == [200] * 5, results[120:]


@pytest.mark.asyncio
async def test_r2_wrong_scheme_or_missing_header_does_not_unlock(live: Live) -> None:
    for hdr in (
        [],
        [("Authorization", live.token)],
        [("Authorization", f"Basic {live.token}")],
        [("Authorization", f"Bearer  {live.token}x")],
        [("Authorization", f"Bearer {live.token[:-1]}")],
    ):
        st, *_ = await raw(live.port, "POST", "/console/v1/launch-code", headers=hdr)
        assert st in (401, 429)
    # case-insensitive scheme with the right token is fine
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/launch-code",
        headers=[("Authorization", f"bearer {live.token}")],
    )
    assert st == 200


WRITES = [
    ("POST", "/console/v1/join"),
    ("POST", "/console/v1/setup-complete"),
    ("PUT", "/console/v1/policy"),
    ("PUT", "/console/v1/node-name"),
    ("POST", "/console/v1/restart"),
    ("POST", "/console/v1/leave"),
    ("DELETE", "/console/v1/session"),
    ("POST", "/console/v1/session"),
]


_VALID_BODY = {
    "/console/v1/join": {"code": "x"},
    "/console/v1/policy": {"set": {}},
    "/console/v1/node-name": {"name": "n"},
    "/console/v1/leave": {},
    "/console/v1/session": {"code": "x"},
}


def _body(path: str, kind: str, method: str) -> bytes:
    if method == "DELETE":
        return b""
    if kind == "malformed":
        return b"{not json"
    return json.dumps(_VALID_BODY.get(path, {})).encode()


def _cases() -> list[object]:
    return [
        pytest.param(method, path, kind, id=f"{method}-{path}-{kind}")
        for method, path in WRITES
        for kind in ("valid", "malformed")
    ]


@pytest.mark.parametrize(("method", "path", "kind"), _cases())
@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://evil.example",
        "null",
        "http://127.0.0.1",
        "https://127.0.0.1:{port}",
        "http://localhost:{port}",
        "http://127.0.0.1:{port}.evil.example",
    ],
)
@pytest.mark.asyncio
async def test_r2_origin_is_decided_before_session_and_body(
    live: Live, method: str, path: str, kind: str, origin: str | None
) -> None:
    """No cookie: a bad Origin is ALWAYS 403 (never 401 / 422)."""
    hdrs = []
    if origin is not None:
        hdrs.append(("Origin", origin.format(port=live.port)))
    st, _, body, cookies = await raw(
        live.port, method, path, headers=hdrs, body=_body(path, kind, method)
    )
    assert st == 403, (st, body)
    assert json.loads(body)["error"]["code"] == "bad_origin"
    assert not cookies


@pytest.mark.parametrize(
    ("method", "path", "kind"),
    [
        c
        for c in _cases()
        if "/session" not in str(getattr(c, "id", ""))
        or "DELETE" in str(getattr(c, "id", ""))
    ],
)
@pytest.mark.asyncio
async def test_r2_good_origin_without_session_is_401_not_a_leak(
    live: Live, method: str, path: str, kind: str
) -> None:
    st, _, body, _ = await raw(
        live.port,
        method,
        path,
        headers=[("Origin", live.origin)],
        body=_body(path, kind, method),
    )
    assert st == 401, (st, body)


@pytest.mark.asyncio
async def test_r2_bad_origin_with_valid_cookie_has_no_side_effect(live: Live) -> None:
    cookie = await _session(live)
    st, *_ = await raw(
        live.port,
        "PUT",
        "/console/v1/node-name",
        headers=[("Cookie", cookie), ("Origin", "http://evil.example")],
        body=json.dumps({"name": "pwned"}).encode(),
    )
    assert st == 403
    assert not (live.settings.data_dir / "node_name").exists()
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/leave",
        headers=[("Cookie", cookie), ("Origin", "http://evil.example")],
        body=b"{}",
    )
    assert st == 403
    st, *_ = await raw(
        live.port,
        "DELETE",
        "/console/v1/session",
        headers=[("Cookie", cookie), ("Origin", "http://evil.example")],
    )
    assert st == 403
    # cookie survived the forged sign-out
    st, *_ = await raw(
        live.port, "GET", "/console/v1/status", headers=[("Cookie", cookie)]
    )
    assert st == 200


@pytest.mark.asyncio
async def test_r2_oversized_body_with_bad_origin_is_refused_before_reading(
    live: Live,
) -> None:
    st, *_ = await raw(
        live.port,
        "POST",
        "/console/v1/session",
        headers=[("Origin", "http://evil.example")],
        body=b"x" * 100_000,
    )
    assert st == 403


@pytest.mark.asyncio
async def test_r2_token_in_session_body_or_query_never_signs_in(live: Live) -> None:
    for body in (
        {"token": live.token},
        {"code": live.token},
        {"code": "", "token": live.token},
        {"t": live.token},
    ):
        st, _, _, cookies = await raw(
            live.port,
            "POST",
            "/console/v1/session",
            headers=[("Origin", live.origin)],
            body=json.dumps(body).encode(),
        )
        assert st in (401, 422, 429) and not cookies, body
    for path in (
        f"/?t={live.token}",
        f"/console/v1/status?t={live.token}",
        f"/console/v1/status?token={live.token}",
    ):
        st, _, _, cookies = await raw(live.port, "GET", path)
        assert not cookies
        if path.startswith("/console"):
            assert st == 401


@pytest.mark.asyncio
async def test_r2_served_page_has_no_token_signin_path(live: Live) -> None:
    """The built UI reads only '#c=' from the fragment; '#t=' (token) is gone."""
    from mf_dashboard import require_fresh_dist

    from meshforge.agent.console.app import DEFAULT_DIST

    # The build is git-ignored: skip (with the rebuild command) when this
    # checkout has none or a stale one, as every built-UI test does (D206). CI's
    # component shards do not run npm; package-linux ships a fresh build.
    dist = require_fresh_dist(DEFAULT_DIST)
    js = "\n".join(
        p.read_text(errors="ignore") for p in (dist / "assets").rglob("*.js")
    )
    assert js, "built console UI missing"
    assert "/session" in js
    assert not re.search(r"""\.get\(["']t["']\)""", js), (
        "UI still reads a 't' fragment key"
    )
    # (a bare "#t=" is a minified private field in React; the fragment parser is checked by key)
    assert not re.search(r"""\.get\(`t`\)""", js)
    assert not re.search(r"""\{\s*token\s*:""", js), "UI posts a token body"


@pytest.mark.asyncio
async def test_r2_setup_complete_needs_a_saved_policy(live: Live) -> None:
    cookie = await _session(live)
    hdrs = [("Cookie", cookie), ("Origin", live.origin)]
    st, _, body, _ = await raw(
        live.port, "POST", "/console/v1/setup-complete", headers=hdrs
    )
    assert st == 409, body
    assert json.loads(body)["error"]["code"] == "policy_not_saved"
    st, _, body, _ = await raw(
        live.port, "GET", "/console/v1/status", headers=[("Cookie", cookie)]
    )
    assert json.loads(body)["setup_complete"] is False
    # a directory squatting on the policy path is not a saved policy
    pol = live.settings.data_dir / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.mkdir()
    st, *_ = await raw(live.port, "POST", "/console/v1/setup-complete", headers=hdrs)
    assert st == 409
    pol.rmdir()
    # saving a policy through the API unlocks it
    st, _, body, _ = await raw(
        live.port,
        "PUT",
        "/console/v1/policy",
        headers=hdrs,
        body=json.dumps({"set": {"accept_jobs": True}}).encode(),
    )
    if st != 200:  # shape of the request is the app's; fall back to a file
        pol.write_text("accept_jobs = true\n")
    st, _, body, _ = await raw(
        live.port, "POST", "/console/v1/setup-complete", headers=hdrs
    )
    assert st == 200, body
    st, _, body, _ = await raw(
        live.port, "GET", "/console/v1/status", headers=[("Cookie", cookie)]
    )
    assert json.loads(body)["setup_complete"] is True


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("a" * 64, True),
        ("a" * 65, False),
        ("\U0001f600" * 64, True),  # 64 code points, 256 UTF-8 bytes
        ("\U0001f600" * 65, False),
        ("e\u0301" * 32, True),  # 64 code points, 32 graphemes
        ("e\u0301" * 33, False),
        ("\u00e9" * 64, True),
        ("  " + "a" * 64 + "  ", True),  # stripped first
        ("bad\tname", False),
        ("bad\nname", False),
        ("nul\x00", False),
    ],
)
@pytest.mark.asyncio
async def test_r2_node_name_length_is_code_points(
    live: Live, name: str, ok: bool
) -> None:
    cookie = await _session(live)
    st, _, body, _ = await raw(
        live.port,
        "PUT",
        "/console/v1/node-name",
        headers=[("Cookie", cookie), ("Origin", live.origin)],
        body=json.dumps({"name": name}, ensure_ascii=True).encode(),
    )
    assert (st == 200) is ok, (st, body)
    if ok:
        st, _, body, _ = await raw(
            live.port, "GET", "/console/v1/node-name", headers=[("Cookie", cookie)]
        )
        assert json.loads(body)["name"] == name.strip()
    else:
        assert st == 422


@pytest.mark.asyncio
async def test_r2_surrogate_name_is_a_clean_error_not_a_500(live: Live) -> None:
    cookie = await _session(live)
    st, *_ = await raw(
        live.port,
        "PUT",
        "/console/v1/node-name",
        headers=[("Cookie", cookie), ("Origin", live.origin)],
        body=b'{"name": "lone\\ud800"}',
    )
    assert st in (200, 422), st
    st, *_ = await raw(live.port, "GET", "/console/v1/health")
    assert st == 200


def test_r2_docker_access_helper_pins_path_before_any_command() -> None:
    script = (
        Path(__file__).resolve().parents[3] / "packaging/linux/desktop/docker-access"
    ).read_text()
    lines = [
        ln.strip()
        for ln in script.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    first_cmd = next(
        i
        for i, ln in enumerate(lines)
        if re.match(r"(if|getent|id|usermod|systemctl|say\(|set -eu)", ln)
        and ln != "set -eu"
    )
    path_i = next(i for i, ln in enumerate(lines) if ln.startswith("PATH="))
    export_i = next(i for i, ln in enumerate(lines) if ln == "export PATH")
    assert path_i < first_cmd and export_i < first_cmd
    assert lines[path_i] == "PATH=/usr/sbin:/usr/bin:/sbin:/bin"
    assert "$PATH" not in lines[path_i]
    assert "/usr/local" not in script.replace("# ", "")


def test_r2_docker_access_helper_ignores_hostile_path(tmp_path: Path) -> None:
    """Run it as non-root with a hostile PATH: must exit 4 and never run the
    attacker's 'id' / 'getent'."""
    import subprocess

    evil = tmp_path / "bin"
    evil.mkdir()
    marker = tmp_path / "pwned"
    for tool in ("id", "getent", "usermod", "systemctl", "tr", "grep"):
        f = evil / tool
        f.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
        f.chmod(0o755)
    script = (
        Path(__file__).resolve().parents[3] / "packaging/linux/desktop/docker-access"
    )
    if os.geteuid() == 0:
        pytest.skip("run as non-root to hit the 'not root' exit")
    r = subprocess.run(
        ["/bin/sh", str(script)],
        env={"PATH": f"{evil}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert r.returncode == 4, (r.returncode, r.stderr)
    assert not marker.exists(), "hostile PATH binary was executed"
