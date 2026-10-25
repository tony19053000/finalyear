"""V0.17 G2 tester: the setup page's HTTP surface under attack.

Real controller runtime in setup mode (no CA, no users, a token), real HTTP on
loopback, real PostgreSQL. Derived from brief 5b item 6: loopback-only,
token-gated (401), one-time (410), one fabric even under concurrent submits,
failure rolls the CA back, nothing but the setup page is served in setup mode.
"""

from __future__ import annotations

# ruff: noqa: ASYNC240, S104
import asyncio
import os
import socket
import stat
from pathlib import Path

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_controller_harness import make_settings

from meshforge.common.errors import FailureType
from meshforge.controller.fabric_ca import fabric_present
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.setup.token import ensure_setup_token
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

PW = "correct horse battery staple 42"


def _settings(pg: DatabaseSettings, tmp_path: Path) -> ControllerSettings:
    s = make_settings(pg, fabric=False, setup={"bind": "127.0.0.1:0"})
    return s.model_copy(update={"data_dir": tmp_path / "data"})


def _token(settings: ControllerSettings) -> str:
    return ensure_setup_token(settings.setup_token_path)[0].get_secret_value()


def _client(port: int, **kw: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        timeout=60.0,
        headers={"origin": f"http://127.0.0.1:{port}"},
        **kw,  # type: ignore[arg-type]
    )


async def _session(client: httpx.AsyncClient, token: str) -> None:
    r = await client.post("/v1/setup/session", json={"token": token})
    assert r.status_code == 200, r.text


async def test_setup_listener_is_loopback_only_and_serves_nothing_else(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    _token(settings)
    async with ControllerRuntime.start(settings) as rt:
        port = rt.setup_port
        assert port
        # not reachable on any non-loopback address of this host
        lan: set[str] = set()
        try:  # the default-route address (no packet is sent by a UDP connect)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("192.0.2.1", 9))
                lan.add(probe.getsockname()[0])
        except OSError:
            pass
        lan.discard("127.0.0.1")
        if not lan:
            pytest.skip("no non-loopback address on this host")
        for ip in lan:
            with pytest.raises(OSError):
                socket.create_connection((ip, port), timeout=2).close()
        # no gRPC / enrol / TLS API in setup mode: the REST API is not served
        async with _client(port) as c:
            for path in ("/v1/auth/login", "/v1/jobs", "/v1/nodes", "/v1/users",
                         "/docs", "/openapi.json", "/metrics"):  # fmt: skip
                r = await c.get(path)
                assert r.status_code in (401, 404, 405, 307, 200), (path, r.status_code)
                assert '"jobs"' not in r.text  # no API payload leaks
            login = await c.post(
                "/v1/auth/login", json={"name": "admin", "password": PW}
            )
            assert login.status_code in (404, 405, 401, 410, 503), login.text
            assert login.status_code != 200
        assert rt.grpc_port == 0


async def test_forged_and_replayed_sessions_are_refused(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    token = _token(settings)
    async with ControllerRuntime.start(settings) as rt, _client(rt.setup_port) as c:
        body = {"admin": "asha", "password": PW}
        for forged in ("", "x", "A" * 43, "A" * 10_000, token, "../../etc/passwd"):
            hdr = {"cookie": f"mf_setup={forged}"}
            r = await c.post("/v1/setup/complete", json=body, headers=hdr)
            assert r.status_code == 401, (forged[:10], r.status_code)
            assert (await c.get("/v1/setup/info", headers=hdr)).status_code == 401
        # the token itself in a header / query is not a session
        r = await c.post(
            "/v1/setup/complete?token=" + token,
            json=body,
            headers={"authorization": f"Bearer {token}", "x-setup-token": token},
        )
        assert r.status_code == 401
        assert not fabric_present(settings.ca_dir)
        # wrong tokens: 401 each, also near-misses, huge, unicode, wrong JSON shapes
        for bad in (token[:-1], token + "x", token.upper(), "é" * 43, "z" * 5000):
            r = await c.post("/v1/setup/session", json={"token": bad})
            assert r.status_code in (401, 422, 400), (bad[:10], r.status_code)
            assert r.status_code != 200
        for shape in ({}, {"token": None}, {"token": 5}, {"token": ["a"]}, []):
            r = await c.post("/v1/setup/session", json=shape)
            assert r.status_code in (400, 422), shape
        assert (
            await c.post("/v1/setup/session", content=b"\xff\xfe{")
        ).status_code in (
            400,
            415,
            422,
        )
        # the controller still works after all that
        await _session(c, token)


async def test_origin_and_host_variants(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    token = _token(settings)
    async with ControllerRuntime.start(settings) as rt:
        port = rt.setup_port
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=30
        ) as c:
            for origin in (
                "http://evil.example",
                "http://127.0.0.1.evil.example",
                "http://localhost.evil.example:1",
                "null",
                "https://attacker.test",
                "http://192.168.1.10",
            ):
                r = await c.post(
                    "/v1/setup/session",
                    json={"token": token},
                    headers={"origin": origin},
                )
                assert r.status_code in (400, 403), (origin, r.status_code)
                assert "set-cookie" not in r.headers
            for host in (
                "evil.example",
                "127.0.0.1.evil.example",
                "localhost.",
                "127.0.0.1:9",
                "127.0.0.1@evil.example",
                "0.0.0.0",
                "[::ffff:127.0.0.1]",
                "",
            ):
                r = await c.get("/v1/setup/state", headers={"host": host})
                assert r.status_code == 400, (host, r.status_code)
            for host in (f"localhost:{port}", f"127.0.0.1:{port}", "LOCALHOST"):
                r = await c.get("/v1/setup/state", headers={"host": host})
                assert r.status_code == 200, (host, r.status_code)
            # content types that a cross-site <form> can send are not JSON
            for ctype in ("text/plain", "application/x-www-form-urlencoded",
                          "multipart/form-data; boundary=x"):  # fmt: skip
                r = await c.post(
                    "/v1/setup/session",
                    content=f'{{"token":"{token}"}}',
                    headers={"content-type": ctype},
                )
                assert r.status_code in (400, 415, 422), (ctype, r.status_code)
                assert "set-cookie" not in r.headers


async def test_invalid_admin_names_and_oversized_bodies_write_nothing(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    token = _token(settings)
    async with ControllerRuntime.start(settings) as rt, _client(rt.setup_port) as c:
        await _session(c, token)
        names = ["", " ", "Admin", "a b", "x" * 65, "ünï", "a/b", "../x", "a\x00b",
                 "-lead", ".dot", "ad;min", "名前"]  # fmt: skip
        for name in names:
            r = await c.post("/v1/setup/complete", json={"admin": name, "password": PW})
            assert r.status_code in (400, 422), (name, r.status_code, r.text)
            assert PW not in r.text
        for pw in ("", "x" * 1025, "a\x00bcdefghijklmn", "aaaaaaaaaaaaaaaa", PW[:11]):
            r = await c.post(
                "/v1/setup/complete", json={"admin": "asha", "password": pw}
            )
            assert r.status_code in (400, 422), (pw[:8], r.status_code)
            assert pw not in r.text or pw == ""
        big = await c.post(
            "/v1/setup/complete", json={"admin": "asha", "password": "p" * 5_000_000}
        )
        assert big.status_code in (400, 413, 422)
        assert not fabric_present(settings.ca_dir)
        assert not settings.tls_dir.exists() or not any(settings.tls_dir.iterdir())
        assert settings.setup_token_path.exists()
        # and the page still works afterwards
        ok = await c.post("/v1/setup/complete", json={"admin": "asha", "password": PW})
        assert ok.status_code == 200, ok.text
        await asyncio.wait_for(rt.wait_for_stop(), 15)


async def test_concurrent_completes_create_exactly_one_fabric(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    from meshforge.controller.auth.users import UserStore
    from meshforge.controller.store.engine import create_engine

    settings = _settings(pg_settings, tmp_path)
    token = _token(settings)
    async with ControllerRuntime.start(settings) as rt:
        port = rt.setup_port
        clients = [_client(port) for _ in range(6)]
        try:
            for c in clients:
                await _session(c, token)
            results = await asyncio.gather(
                *(
                    c.post(
                        "/v1/setup/complete",
                        json={"admin": f"admin{i}", "password": PW + str(i)},
                    )
                    for i, c in enumerate(clients)
                )
            )
        finally:
            for c in clients:
                await c.aclose()
        codes = sorted(r.status_code for r in results)
        assert codes.count(200) == 1, codes
        assert all(code in (200, 401, 409, 410) for code in codes), codes
        winner = next(r.json()["admin"] for r in results if r.status_code == 200)
        engine = create_engine(pg_settings)
        try:
            users = [u.name for u in await UserStore(engine).list_users()]
        finally:
            await engine.dispose()
        assert users == [winner], users
        assert fabric_present(settings.ca_dir)
        assert not settings.setup_token_path.exists()
        await asyncio.wait_for(rt.wait_for_stop(), 15)


async def test_admin_creation_failure_rolls_the_ca_back_and_page_is_retryable(
    pg_settings: DatabaseSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meshforge.common.errors import MeshForgeError
    from meshforge.controller.auth.users import UserStore

    settings = _settings(pg_settings, tmp_path)
    token = _token(settings)
    real = UserStore.create_user
    calls = {"n": 0}

    async def flaky(self: UserStore, *a: object, **k: object) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise MeshForgeError(
                "database went away", failure_type=FailureType.NETWORK_ERROR
            )
        return await real(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(UserStore, "create_user", flaky)
    async with ControllerRuntime.start(settings) as rt, _client(rt.setup_port) as c:
        await _session(c, token)
        first = await c.post(
            "/v1/setup/complete", json={"admin": "asha", "password": PW}
        )
        assert first.status_code >= 400 and first.status_code != 410, first.text
        assert PW not in first.text
        assert not fabric_present(settings.ca_dir), "CA left behind after failure"
        assert not settings.tls_dir.exists() or not any(settings.tls_dir.iterdir())
        assert settings.setup_token_path.exists(), "token must survive a failed try"
        state = await c.get("/v1/setup/state")
        assert state.status_code == 200 and state.json()["state"] == "pending"
        second = await c.post(
            "/v1/setup/complete", json={"admin": "asha", "password": PW}
        )
        assert second.status_code == 200, second.text
        assert fabric_present(settings.ca_dir)
        await asyncio.wait_for(rt.wait_for_stop(), 15)


async def test_unreadable_token_file_refuses_setup_rather_than_opening_it(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    _token(settings)
    settings.setup_token_path.chmod(0o644)  # another user could read the link
    from meshforge.common.errors import MeshForgeError

    with pytest.raises(MeshForgeError):
        async with ControllerRuntime.start(settings):
            pytest.fail("a world-readable setup token must not start setup mode")
    assert stat.S_IMODE(settings.setup_token_path.stat().st_mode) == 0o644


async def test_everything_is_410_after_setup_and_token_stays_deleted(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    token = _token(settings)
    async with ControllerRuntime.start(settings) as rt:
        async with _client(rt.setup_port) as c:
            await _session(c, token)
            r = await c.post(
                "/v1/setup/complete", json={"admin": "asha", "password": PW}
            )
            assert r.status_code == 200
            # the cookie that completed it, and the old token, are dead at once
            for method, path, body in (
                ("GET", "/v1/setup/state", None),
                ("GET", "/v1/setup/info", None),
                ("POST", "/v1/setup/session", {"token": token}),
                ("POST", "/v1/setup/complete", {"admin": "z", "password": PW}),
            ):
                resp = await c.request(method, path, json=body)
                assert resp.status_code == 410, (path, resp.status_code)
        await asyncio.wait_for(rt.wait_for_stop(), 15)
    assert not settings.setup_token_path.exists()
    # a restart (as the service manager would do) is the normal mode, never setup
    async with ControllerRuntime.start(settings) as normal:
        assert not normal.setup_pending
    # an attacker (or an admin) putting a token file back does not reopen setup
    ensure_setup_token(settings.setup_token_path)
    async with ControllerRuntime.start(settings) as again:
        assert not again.setup_pending
        assert os.path.exists(settings.setup_token_path)  # stale file is harmless
