"""V0.8 G1 deliverable 5b (D169): the controller serves the dashboard SPA
outside ``/v1`` - never shadowing an API path, never leaving ``dist/``, with
cache and security headers; an unbuilt dashboard is a plain page, not a 500."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mf_users import ADMIN_TOKEN, bearer, static_auth

from meshforge.controller.http import create_http_app
from meshforge.controller.web.spa import CSP, NOT_BUILT_HTML, SpaMiddleware

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

INDEX = b"<!doctype html><title>MeshForge</title><div id=root></div>"
ASSET = b"console.log('app')"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_bytes(INDEX)
    (root / "assets" / "app.3f9c2a.js").write_bytes(ASSET)
    (root / "favicon.svg").write_text("<svg/>")
    (root / ".env").write_text("SECRET=1")
    (tmp_path / "outside.txt").write_text("outside")
    os.symlink(tmp_path / "outside.txt", root / "escape.txt")
    return root


class _Manager:
    def __len__(self) -> int:
        return 0


def _app(dist: Path | None) -> Any:
    ns = SimpleNamespace
    return create_http_app(
        _Manager(),  # type: ignore[arg-type]
        registry=ns(),  # type: ignore[arg-type]
        resources=ns(),  # type: ignore[arg-type]
        clock=lambda: 0.0,
        auth=static_auth(),
        web_dist=dist,
    )


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ctl.test"
    )


async def test_routes_fall_back_to_index_and_assets_are_immutable(dist: Path) -> None:
    async with _client(_app(dist)) as client:
        for path in ("/", "/jobs", "/jobs/job_123/logs", "/settings/tokens"):
            r = await client.get(path)
            assert r.status_code == 200 and r.content == INDEX, path
            assert r.headers["cache-control"] == "no-cache"
            assert r.headers["content-type"].startswith("text/html")
        asset = await client.get("/assets/app.3f9c2a.js")
        assert asset.content == ASSET
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "javascript" in asset.headers["content-type"]
        icon = await client.get("/favicon.svg")
        assert icon.status_code == 200 and icon.headers["cache-control"] == "no-cache"
        head = await client.head("/")
        assert head.status_code == 200 and head.content == b""


async def test_the_spa_never_shadows_the_api(dist: Path) -> None:
    async with _client(_app(dist)) as client:
        unknown = await client.get("/v1/no-such-route")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "not_found"
        assert (await client.get("/healthz")).json()["status"] == "ok"
        spec = await client.get("/v1/openapi.json")
        assert spec.status_code == 200 and "paths" in spec.json()
        nodes = await client.get("/v1/nodes")  # the API's auth still applies
        assert nodes.status_code == 401
        ok = await client.get("/v1/openapi.json", headers=bearer(ADMIN_TOKEN))
        assert ok.status_code == 200
        bare = await client.get("/v1")
        assert bare.status_code == 404 and bare.content != INDEX


@pytest.mark.security
async def test_files_outside_dist_and_hidden_files_are_never_served(
    dist: Path,
) -> None:
    async with _client(_app(dist)) as client:
        for path in ("/escape.txt", "/.env", "/assets/missing.js", "/x/../.env"):
            r = await client.get(path)
            assert r.status_code == 404, path
            assert b"outside" not in r.content and b"SECRET" not in r.content
    spa = SpaMiddleware(lambda *_: None, dist=dist)  # type: ignore[arg-type]
    for raw in (
        "/../outside.txt",
        "/assets/../../outside.txt",
        "//etc/passwd",
        "/escape.txt",
        "/.env",
        "/assets\\..\\..\\outside.txt",
        "/index.html\x00.js",
        "/" + "a" * 2000,
    ):
        assert spa._file(raw) is None, raw


async def test_other_methods_outside_v1_are_405(dist: Path) -> None:
    async with _client(_app(dist)) as client:
        r = await client.post("/jobs", content=b"x")
    assert r.status_code == 405


async def test_an_unbuilt_dashboard_is_a_plain_page_not_a_500(tmp_path: Path) -> None:
    async with _client(_app(tmp_path / "nothing-here")) as client:
        r = await client.get("/")
        assert r.status_code == 200 and r.text == NOT_BUILT_HTML
        assert "npm run build" in r.text and "https://" not in r.text
        assert (await client.get("/jobs")).status_code == 200
        assert (await client.get("/assets/app.js")).status_code == 404
        assert (await client.get("/healthz")).status_code == 200


@pytest.mark.security
async def test_security_headers_on_spa_and_api_responses(dist: Path) -> None:
    async with _client(_app(dist)) as client:
        for path in ("/", "/assets/app.3f9c2a.js", "/healthz", "/v1/nodes", "/nope.js"):
            r = await client.get(path)
            assert r.headers["content-security-policy"] == CSP, path
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["referrer-policy"] == "no-referrer"
            assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in CSP and "frame-ancestors 'none'" in CSP
    assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP


async def test_web_dist_none_turns_the_spa_off(tmp_path: Path) -> None:
    async with _client(_app(None)) as client:
        r = await client.get("/")
    assert r.status_code == 404
