"""V0.8 G1 tester r1: the real controller's front door - raw-byte requests
(no client normalisation) against the SPA mount, the loopback-only plain
listener, headers on every answer, and the metrics endpoint's input handling."""

# ruff: noqa: PT012

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.web.spa import CSP

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
    pytest.mark.timeout(180),
]

ALICE_PW = "alice-password-1"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        http={"loopback_plain": "127.0.0.1:0"},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def alice(runtime: ControllerRuntime, engine: AsyncEngine) -> str:
    await create_user(engine, "alice", ALICE_PW)
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as client:
        return await login(client, "alice", ALICE_PW)


async def _raw(
    port: int, request: bytes, *, tls: bool
) -> tuple[int, dict[str, str], bytes]:
    ctx = None
    if tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
    writer.write(request)
    await writer.drain()
    data = b""
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(65536), 5)
            if not chunk:
                break
            data += chunk
    except (TimeoutError, ConnectionError, ssl.SSLError):
        pass
    finally:
        writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    if not lines or not lines[0]:
        return 0, {}, b""
    status = int(lines[0].split()[1])
    headers = {
        k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:] if ": " in ln)
    }
    return status, headers, body


def _req(target: str, method: str = "GET", extra: str = "") -> bytes:
    return (
        f"{method} {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n{extra}\r\n"
    ).encode("latin-1", "replace")


RAW_TARGETS = [
    "/../../../../etc/passwd",
    "/%2e%2e/%2e%2e/etc/passwd",
    "/%2E%2E%2F%2E%2E%2Fetc%2Fpasswd",
    "/assets/../../../etc/passwd",
    "/assets/%2e%2e/%2e%2e/%2e%2e/etc/passwd",
    "/..%5c..%5cetc%5cpasswd",
    "/%00",
    "/%00.js",
    "//etc/passwd",
    "/./././etc/passwd",
    "/.git/config",
    "/web/dist/../../../../../etc/passwd",
    "/src/meshforge/controller/settings.py",
    "/pyproject.toml",
    "/\x7f",
    "/" + "A" * 20000,
]


@pytest.mark.parametrize("target", RAW_TARGETS)
@pytest.mark.parametrize("tls", [True, False])
async def test_raw_traversal_targets_never_leak_and_never_500(
    runtime: ControllerRuntime, target: str, tls: bool
) -> None:
    port = runtime.http_port if tls else runtime.plain_port
    assert port
    status, headers, body = await _raw(port, _req(target), tls=tls)
    assert status != 500, (target[:50], status)
    assert b"root:" not in body and b"[project]" not in body and b"[core]" not in body
    assert b"class ControllerSettings" not in body
    # (a request uvicorn's own parser refuses - a bare 400 text/plain - never
    # reaches the app, so it has no app headers; everything else does)
    refused_by_parser = status == 400 and headers.get("content-type", "").startswith(
        "text/plain"
    )
    if status and not refused_by_parser:
        assert headers.get("content-security-policy") == CSP
        assert headers.get("x-content-type-options") == "nosniff"


@pytest.mark.parametrize("tls", [True, False])
async def test_the_dashboard_root_is_never_a_500_built_or_not(
    runtime: ControllerRuntime, tls: bool
) -> None:
    port = runtime.http_port if tls else runtime.plain_port
    assert port
    for method in ("GET", "HEAD"):
        status, headers, body = await _raw(port, _req("/", method), tls=tls)
        assert status == 200, (method, status)
        assert headers["cache-control"] == "no-cache"
        assert headers.get("content-security-policy") == CSP
        assert headers.get("referrer-policy") == "no-referrer"
        assert headers.get("x-frame-options") == "DENY"
        if method == "HEAD":
            assert body == b""
    status, _, body = await _raw(port, _req("/jobs/job_1/logs"), tls=tls)
    assert status == 200 and b"<html" in body.lower()


async def test_api_and_health_are_never_shadowed_by_the_dashboard(
    runtime: ControllerRuntime, alice: str
) -> None:
    port = runtime.http_port
    for target in ("/healthz", "/v1/openapi.json"):
        status, headers, body = await _raw(port, _req(target), tls=True)
        assert status == 200 and headers["content-type"].startswith(
            "application/json"
        ), target
    status, headers, body = await _raw(port, _req("/v1/nope"), tls=True)
    assert status in (401, 404) and headers["content-type"].startswith(
        "application/json"
    )
    status, _, body = await _raw(port, _req("/v1/session", "POST"), tls=True)
    assert status in (400, 403, 415, 422) and b"<html" not in body.lower()
    # A path that merely starts with "v1" is the dashboard's, not the API's.
    status, headers, body = await _raw(port, _req("/v1x"), tls=True)
    assert status == 200 and headers["content-type"].startswith("text/html")


async def test_the_api_error_envelope_also_carries_the_security_headers(
    runtime: ControllerRuntime,
) -> None:
    for target in ("/v1/nodes", "/v1/nope", "/healthz"):
        _, headers, _ = await _raw(runtime.http_port, _req(target), tls=True)
        assert headers.get("content-security-policy") == CSP, target
        assert headers.get("x-content-type-options") == "nosniff", target


# --------------------------------------------------------------- loopback


def _lan_ip() -> str | None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packet is sent
        ip = str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()
    return None if ip.startswith("127.") else ip


async def test_the_plain_port_is_unreachable_from_a_non_loopback_address(
    runtime: ControllerRuntime,
) -> None:
    assert runtime.plain_port
    lan = _lan_ip()
    if lan is None:
        pytest.skip("this machine has no non-loopback IPv4 address")
    with pytest.raises(OSError):
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(lan, runtime.plain_port), 3
        )
        writer.close()
    # and it is bound to loopback only
    assert runtime._plain_sock is not None
    assert runtime._plain_sock.getsockname()[0] == "127.0.0.1"
    # the LAN-facing HTTPS port does not also answer plain HTTP
    status, _, _ = await _raw(runtime.http_port, _req("/healthz"), tls=False)
    assert status == 0 or status >= 400


async def test_plain_listener_still_refuses_cross_origin_logins_and_unauthed_reads(
    runtime: ControllerRuntime, alice: str
) -> None:
    plain = f"http://127.0.0.1:{runtime.plain_port}"
    async with httpx.AsyncClient(base_url=plain, timeout=10) as client:
        evil = await client.post(
            "/v1/session",
            json={"name": "alice", "password": ALICE_PW},
            headers={"origin": "http://evil.example"},
        )
        assert evil.status_code == 403 and "set-cookie" not in evil.headers
        https_origin = await client.post(  # the TLS origin is not this origin
            "/v1/session",
            json={"name": "alice", "password": ALICE_PW},
            headers={"origin": f"https://127.0.0.1:{runtime.http_port}"},
        )
        assert https_origin.status_code == 403
        for path in ("/v1/nodes", "/v1/jobs", "/v1/auth/tokens", "/v1/secrets"):
            assert (await client.get(path)).status_code == 401, path


# --------------------------------------------------------------- metrics


@pytest.mark.parametrize(
    "path",
    [
        "/v1/nodes/node_01J0000000000000000000000A/metrics",  # unknown node
        "/v1/nodes/x/metrics",
        "/v1/nodes/%00/metrics",
        "/v1/nodes/" + "n" * 300 + "/metrics",
        "/v1/nodes/node_01J0000000000000000000000A/metrics?window=0",
        "/v1/nodes/node_01J0000000000000000000000A/metrics?window=9",
        "/v1/nodes/node_01J0000000000000000000000A/metrics?window=301",
        "/v1/nodes/node_01J0000000000000000000000A/metrics?window=-1",
        "/v1/nodes/node_01J0000000000000000000000A/metrics?window=abc",
        "/v1/nodes/node_01J0000000000000000000000A/metrics?window=1e3",
        "/v1/nodes/node_01J0000000000000000000000A/metrics?window=99999999999999999999",
    ],
)
async def test_metrics_endpoint_rejects_bad_input_cleanly(
    runtime: ControllerRuntime, alice: str, path: str
) -> None:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as client:
        r = await client.get(path, headers=bearer(alice))
        assert r.status_code in (400, 404, 422), (path, r.status_code)
        assert r.json()["error"]["code"] in {"invalid_input", "not_found"}
        assert (await client.get(path)).status_code in (401, 404)
