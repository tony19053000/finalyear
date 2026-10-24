"""V0.17 G2 (D392, Q20): the controller's one-time "create fabric" setup, end to end.

A real controller runtime in ``mtls`` mode with no fabric CA and a setup token
serves only the setup page on loopback; over real HTTP: the token gate, the
session cookie, the same-origin and Host guards, the password rule, then
"create fabric" (CA + server cert + admin + first invite), the 410 after it, the
restart into the normal mode where the admin can sign in. The ``_serve`` restart
loop is checked with the real ``meshforge-controller`` process. Real PostgreSQL
(the shared fixture), no mocks.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_controller_harness import make_settings
from mf_session_client import free_port_pair

from meshforge.common.errors import ConfigError
from meshforge.controller.fabric_ca import fabric_present
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.setup.token import ensure_setup_token
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

PASSWORD = "correct horse battery staple 42"


def _settings(pg_settings: DatabaseSettings, tmp_path: Path) -> ControllerSettings:
    return make_settings(
        pg_settings, fabric=False, setup={"bind": "127.0.0.1:0"}
    ).model_copy(update={"data_dir": tmp_path / "data"})


def _token(settings: ControllerSettings) -> str:
    token, created = ensure_setup_token(settings.setup_token_path)
    assert created
    assert settings.setup_token_path.stat().st_mode & 0o777 == 0o600
    return token.get_secret_value()


def _client(port: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        timeout=30.0,
        headers={"origin": f"http://127.0.0.1:{port}"},
    )


async def test_setup_page_creates_the_fabric_then_is_gone(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    token = _token(settings)
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.setup_pending and runtime.setup_port
        assert runtime.grpc_port == 0  # nothing but the setup page listens
        port = runtime.setup_port
        async with _client(port) as client:
            # the page itself (SPA) and the redirect from /
            root = await client.get("/", follow_redirects=False)
            assert root.status_code == 307 and root.headers["location"] == "/setup"
            assert (await client.get("/healthz")).json() == {"status": "setup"}
            state = await client.get("/v1/setup/state")
            assert state.json() == {"state": "pending", "session": False}
            # gates: no session, wrong token, other site, other Host
            assert (await client.get("/v1/setup/info")).status_code == 401
            wrong = await client.post("/v1/setup/session", json={"token": "x" * 43})
            assert wrong.status_code == 401
            assert "setup url" in wrong.json()["error"]["message"]
            cross = await client.post(
                "/v1/setup/session",
                json={"token": token},
                headers={"origin": "http://evil.example"},
            )
            assert cross.status_code == 403
            not_json = await client.post("/v1/setup/session", content=f"token={token}")
            assert not_json.status_code in (400, 415)  # never a session
            rebound = await client.get(
                "/v1/setup/state", headers={"host": "evil.example"}
            )
            assert rebound.status_code == 400
            # the right token -> HttpOnly cookie
            opened = await client.post("/v1/setup/session", json={"token": token})
            assert opened.status_code == 200, opened.text
            cookie = opened.headers["set-cookie"].lower()
            assert "httponly" in cookie and "samesite=strict" in cookie
            assert "path=/v1/setup" in cookie
            info = (await client.get("/v1/setup/info")).json()
            assert info["enroll_address"] is None  # port 0: named at start only
            assert info["address_problem"]
            assert {c["name"] for c in info["checks"]["checks"]} >= {"fabric CA"}
            # a weak password writes nothing
            weak = await client.post(
                "/v1/setup/complete", json={"admin": "asha", "password": "short"}
            )
            assert weak.status_code == 400
            assert "short" not in weak.text
            same = await client.post(
                "/v1/setup/complete",
                json={"admin": "asha", "password": "asha-is-the-admin-2026"},
            )
            assert same.status_code == 400 and "user name" in same.text
            assert not fabric_present(settings.ca_dir)
            # create the fabric
            done = await client.post(
                "/v1/setup/complete", json={"admin": "asha", "password": PASSWORD}
            )
            assert done.status_code == 200, done.text
            body = done.json()
            assert body["admin"] == "asha" and body["restarting"] is True
            # The harness binds enrolment to port 0, so no invite can name a
            # port: the result says how to make one instead (never crashes).
            assert body["invite"] is None and "Invites page" in body["invite_problem"]
            assert body["fabric_id"].startswith("fab_")
            assert fabric_present(settings.ca_dir)
            assert not settings.setup_token_path.exists()
            # replay: 410 forever
            again = await client.post(
                "/v1/setup/complete", json={"admin": "ravi", "password": PASSWORD}
            )
            assert again.status_code == 410
            assert (await client.get("/v1/setup/state")).status_code == 410
        await asyncio.wait_for(runtime.wait_for_stop(), 10)
        assert runtime.restart_requested
        assert runtime.failed_task is None

    # The normal mode: TLS listeners, the admin signs in, setup is 410.
    async with ControllerRuntime.start(settings) as normal:
        assert not normal.setup_pending and normal.grpc_port
        async with httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{normal.http_port}",
            timeout=30.0,
            verify=ssl.create_default_context(cafile=str(settings.ca_dir / "ca.crt")),
        ) as client:
            login = await client.post(
                "/v1/auth/login", json={"name": "asha", "password": PASSWORD}
            )
            assert login.status_code in (200, 201), login.text
            gone = await client.get("/v1/setup/state")
            assert gone.status_code == 410
            assert (await client.post("/v1/setup/complete", json={})).status_code == 410


async def test_setup_with_an_enrolment_address_issues_the_first_invite(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    _grpc_port, enroll_port = free_port_pair()
    settings = make_settings(
        pg_settings,
        fabric=False,
        setup={"bind": "127.0.0.1:0"},
        enroll={"bind": f"127.0.0.1:{enroll_port}"},
    ).model_copy(update={"data_dir": tmp_path / "data"})
    token = _token(settings)
    async with (
        ControllerRuntime.start(settings) as runtime,
        _client(runtime.setup_port) as client,
    ):
        await client.post("/v1/setup/session", json={"token": token})
        info = (await client.get("/v1/setup/info")).json()
        assert info["enroll_address"] == f"127.0.0.1:{enroll_port}"
        done = await client.post(
            "/v1/setup/complete", json={"admin": "asha", "password": PASSWORD}
        )
        body = done.json()
    invite = body["invite"]
    assert invite is not None and invite["code"].startswith("mf1-")
    assert invite["address"] == f"127.0.0.1:{enroll_port}"
    assert body["join_fingerprint"]


async def test_users_without_a_ca_refuse_setup_mode(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    from meshforge.controller.auth.users import UserStore
    from meshforge.controller.store.engine import create_engine
    from meshforge.controller.store.migrate import upgrade_to_head
    from meshforge.controller.store.models.auth import UserRole

    settings = _settings(pg_settings, tmp_path)
    _token(settings)
    await asyncio.to_thread(upgrade_to_head, pg_settings)
    engine = create_engine(pg_settings)
    try:
        await UserStore(engine).create_user(
            "old", pw_hash="x" * 20, role=UserRole.ADMIN, actor="test"
        )
    finally:
        await engine.dispose()
    with pytest.raises(ConfigError, match="fabric create"):
        async with ControllerRuntime.start(settings):
            pytest.fail("must not start")


async def test_no_ca_and_no_token_still_refuses_to_start(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    with pytest.raises(ConfigError, match="setup url"):
        async with ControllerRuntime.start(settings):
            pytest.fail("must not start")


# ------------------------------------------------- the real process restarts
def _get_json(url: str, **kw: Any) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=2, **kw) as resp:  # noqa: S310
            return resp.status, dict(json.loads(resp.read()))
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return 0, {}


async def test_controller_process_restarts_itself_after_setup(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    grpc_port, http_port = free_port_pair()
    enroll_port, setup_port = free_port_pair()
    data_dir = tmp_path / "data"
    cfg = tmp_path / "controller.toml"
    cfg.write_text(
        f"data_dir = {str(data_dir)!r}\n"
        '[log]\nformat = "json"\nlevel = "INFO"\n'
        "[controller]\n"
        f'grpc_bind = "127.0.0.1:{grpc_port}"\n'
        f'http_bind = "127.0.0.1:{http_port}"\n'
        "shutdown_drain_s = 1.0\n"
        "[controller.discovery]\nadvertise = false\n"
        f'[controller.enroll]\nbind = "127.0.0.1:{enroll_port}"\n'
        f'[controller.setup]\nbind = "127.0.0.1:{setup_port}"\n',
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_DATABASE__HOST": pg_settings.host,
            "MESHFORGE_DATABASE__PORT": str(pg_settings.port),
            "MESHFORGE_DATABASE__NAME": pg_settings.name,
            "MESHFORGE_DATABASE__USER": pg_settings.user,
            "MESHFORGE_DATABASE__PASSWORD": pg_settings.password.get_secret_value(),
            "PYTHONUNBUFFERED": "1",
        }
    )
    cli = [sys.executable, "-c", "from meshforge.controller.app import main; main()"]
    done_url = await asyncio.to_thread(
        subprocess.run,
        [*cli, "setup", "url", "--config", str(cfg)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    url = done_url.stdout.strip()
    assert url.startswith(f"http://127.0.0.1:{setup_port}/setup#token=")
    token = url.split("#token=", 1)[1]
    proc = subprocess.Popen(  # noqa: ASYNC220 - the controller under test
        [*cli, "--config", str(cfg)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while _get_json(f"http://127.0.0.1:{setup_port}/healthz")[0] != 200:
            assert proc.poll() is None, proc.stdout.read() if proc.stdout else ""
            assert time.monotonic() < deadline
            await asyncio.sleep(0.1)
        async with _client(setup_port) as client:
            await client.post("/v1/setup/session", json={"token": token})
            done = await client.post(
                "/v1/setup/complete", json={"admin": "asha", "password": PASSWORD}
            )
            assert done.status_code == 200, done.text
            assert done.json()["invite"]["code"].startswith("mf1-")
        context = ssl.create_default_context(
            cafile=str(data_dir / "controller/ca/ca.crt")
        )
        deadline = time.monotonic() + 60
        while (
            _get_json(f"https://127.0.0.1:{http_port}/healthz", context=context)[0]
            != 200
        ):
            assert proc.poll() is None, proc.stdout.read() if proc.stdout else ""
            assert time.monotonic() < deadline
            await asyncio.sleep(0.2)
        # the setup listener is gone with the setup mode
        assert _get_json(f"http://127.0.0.1:{setup_port}/healthz")[0] == 0
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout is not None:
            proc.stdout.close()
