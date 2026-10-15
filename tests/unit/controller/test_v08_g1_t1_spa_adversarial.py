"""V0.8 G1 tester r1: the SPA middleware against hostile paths (D169).

Derived from the brief: /v1 and /healthz are never shadowed, nothing outside
dist/ is ever served, security + cache headers are right, a missing build is a
page and not a 500.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.responses import JSONResponse

from meshforge.controller.web.spa import (
    CSP,
    SecurityHeadersMiddleware,
    SpaMiddleware,
)

pytestmark = [pytest.mark.unit, pytest.mark.security, pytest.mark.asyncio]

SECRET = "TOP-SECRET-OUTSIDE-DIST"


def _app(dist: Path) -> FastAPI:
    app = FastAPI()

    @app.get("/v1/ping")
    async def ping() -> dict[str, str]:  # pragma: no cover - trivial
        return {"api": "yes"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # pragma: no cover - trivial
        return {"status": "ok"}

    @app.get("/v1/boom")
    async def boom() -> JSONResponse:
        raise RuntimeError("boom")

    app.add_middleware(SpaMiddleware, dist=dist)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "secret.txt").write_text(SECRET)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>INDEX</html>")
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)")
    (dist / "assets" / "app-abc123.css").write_text("a{}")
    (dist / "assets" / "font.woff2").write_bytes(b"\x00woff")
    (dist / ".env").write_text(SECRET)
    (dist / "assets" / ".hidden").write_text(SECRET)
    return dist


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


async def _raw(app: FastAPI, path: str, method: str = "GET") -> httpx.Response:
    """Send ``path`` byte for byte (httpx would normalise ``..``)."""
    async with _client(app) as client:
        request = client.build_request(method, "http://testserver/")
        request.url = httpx.URL("http://testserver" + path)
        return await client.send(request)


HOSTILE = [
    "/../secret.txt",
    "/assets/../../secret.txt",
    "/assets/%2e%2e/%2e%2e/secret.txt",
    "/%2e%2e/secret.txt",
    "/..%2fsecret.txt",
    "/assets/..%2f..%2fsecret.txt",
    "/assets/..\\..\\secret.txt",
    "/\\..\\secret.txt",
    "//etc/passwd",
    "/etc/passwd",
    "/assets//..//..//secret.txt",
    "/.env",
    "/assets/.hidden",
    "/assets/%00.js",
    "/index.html%00.txt",
    "/" + "a" * 5000,
    "/" + "../" * 200 + "secret.txt",
    "/assets/app-abc123.js/../../../secret.txt",
]


@pytest.mark.parametrize("path", HOSTILE)
async def test_no_path_ever_serves_a_file_outside_dist_or_a_hidden_one(
    tree: Path, path: str
) -> None:
    resp = await _raw(_app(tree), path)
    assert resp.status_code in (200, 400, 404), (path, resp.status_code)
    assert SECRET not in resp.text, path
    assert resp.status_code != 500


async def test_a_symlink_inside_dist_pointing_out_is_refused(tree: Path) -> None:
    link = tree / "assets" / "escape.js"
    try:
        os.symlink(tree.parent / "secret.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    resp = await _raw(_app(tree), "/assets/escape.js")
    assert SECRET not in resp.text
    assert resp.status_code == 404


async def test_a_symlinked_directory_out_of_dist_is_refused(tree: Path) -> None:
    outside = tree.parent / "outside"
    outside.mkdir()
    (outside / "x.js").write_text(SECRET)
    try:
        os.symlink(outside, tree / "assets" / "dir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    resp = await _raw(_app(tree), "/assets/dir/x.js")
    assert SECRET not in resp.text


@pytest.mark.parametrize("path", ["/v1/ping", "/healthz"])
async def test_api_paths_are_never_shadowed(tree: Path, path: str) -> None:
    resp = await _raw(_app(tree), path)
    assert resp.status_code == 200 and "INDEX" not in resp.text


@pytest.mark.parametrize(
    "path", ["/v1/nope", "/v1/", "/v1", "/v1/ping/extra", "/v1/x.js"]
)
async def test_unknown_api_paths_are_the_apis_404_never_html(
    tree: Path, path: str
) -> None:
    resp = await _raw(_app(tree), path)
    assert "INDEX" not in resp.text, path
    assert "text/html" not in resp.headers.get("content-type", ""), path


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_non_get_methods_outside_v1_are_405_with_no_file_content(
    tree: Path, method: str
) -> None:
    for path in ("/", "/assets/app-abc123.js", "/jobs/abc"):
        resp = await _raw(_app(tree), path, method)
        assert resp.status_code == 405, (method, path)
        assert "INDEX" not in resp.text


async def test_head_returns_headers_and_no_body(tree: Path) -> None:
    resp = await _raw(_app(tree), "/", "HEAD")
    assert resp.status_code == 200 and resp.content == b""
    assert resp.headers["cache-control"] == "no-cache"


async def test_spa_routes_get_index_and_missing_files_get_a_json_404(
    tree: Path,
) -> None:
    app = _app(tree)
    for path in ("/", "/jobs/job_1", "/nodes", "/settings/tokens", "/docs", "/redoc"):
        resp = await _raw(app, path)
        assert resp.status_code == 200 and "INDEX" in resp.text, path
        assert resp.headers["cache-control"] == "no-cache"
    for path in ("/missing.js", "/assets/gone.css", "/favicon.ico", "/a/b.map"):
        resp = await _raw(app, path)
        assert resp.status_code == 404 and "INDEX" not in resp.text, path
        assert "json" in resp.headers["content-type"]


async def test_cache_headers_hashed_assets_immutable_index_never(tree: Path) -> None:
    app = _app(tree)
    asset = await _raw(app, "/assets/app-abc123.js")
    assert asset.status_code == 200
    assert "immutable" in asset.headers["cache-control"]
    assert "max-age=31536000" in asset.headers["cache-control"]
    for path in ("/", "/index.html", "/nodes"):
        resp = await _raw(app, path)
        cache = resp.headers["cache-control"]
        assert "immutable" not in cache and "max-age=3" not in cache, path


async def test_content_types_are_right_for_the_bundle_files(tree: Path) -> None:
    app = _app(tree)
    js = await _raw(app, "/assets/app-abc123.js")
    css = await _raw(app, "/assets/app-abc123.css")
    assert "javascript" in js.headers["content-type"]
    assert "text/css" in css.headers["content-type"]
    assert js.headers.get("x-content-type-options") == "nosniff"


async def test_security_headers_are_on_every_kind_of_response(tree: Path) -> None:
    app = _app(tree)
    cases = [
        ("GET", "/"),
        ("GET", "/assets/app-abc123.js"),
        ("GET", "/missing.js"),
        ("POST", "/"),
        ("GET", "/v1/ping"),
        ("GET", "/v1/nope"),
        ("GET", "/healthz"),
    ]
    for method, path in cases:
        resp = await _raw(app, path, method)
        assert resp.headers.get("content-security-policy") == CSP, (method, path)
        assert resp.headers.get("x-content-type-options") == "nosniff", path
        assert resp.headers.get("referrer-policy") == "no-referrer", path
        assert resp.headers.get("x-frame-options") == "DENY", path


async def test_the_csp_has_no_unsafe_directives_and_forbids_framing() -> None:
    assert "unsafe-inline" not in CSP and "unsafe-eval" not in CSP
    assert "default-src 'self'" in CSP and "frame-ancestors 'none'" in CSP
    assert "object-src 'none'" in CSP and "base-uri 'self'" in CSP
    assert "http:" not in CSP and "*" not in CSP and "https:" not in CSP


async def test_not_built_is_a_page_not_a_500_and_not_cached(tmp_path: Path) -> None:
    app = _app(tmp_path / "does-not-exist")
    for path in ("/", "/jobs/1", "/nodes"):
        resp = await _raw(app, path)
        assert resp.status_code == 200 and "not built" in resp.text
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers.get("content-security-policy") == CSP
    assert (await _raw(app, "/x.js")).status_code == 404
    assert (await _raw(app, "/v1/ping")).status_code == 200
    # dist exists but is empty (a failed build): same page.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "not built" in (await _raw(_app(empty), "/")).text


async def test_a_directory_named_like_a_route_does_not_500(tree: Path) -> None:
    (tree / "nodes").mkdir()
    for path in ("/nodes", "/nodes/", "/assets", "/assets/"):
        resp = await _raw(_app(tree), path)
        assert resp.status_code == 200 and "INDEX" in resp.text, path


@pytest.mark.skipif(sys.platform == "win32", reason="posix paths")
async def test_unicode_and_odd_segments_never_500(tree: Path) -> None:
    (tree / "assets" / "ü-π.js").write_text("x")
    app = _app(tree)
    for path in ("/assets/%C3%BC-%CF%80.js", "/%F0%9F%92%A5", "/assets/%ff.js", "/ "):
        resp = await _raw(app, path)
        assert resp.status_code < 500, path


async def test_t1_an_unhandled_500_still_carries_the_security_headers() -> None:
    from types import SimpleNamespace

    from mf_users import ADMIN_TOKEN, bearer, static_auth

    from meshforge.controller.http import create_http_app

    ns = SimpleNamespace

    class _Manager:
        def __len__(self) -> int:
            return 0

    app = create_http_app(
        _Manager(),  # type: ignore[arg-type]
        registry=ns(),  # type: ignore[arg-type]
        resources=ns(),  # type: ignore[arg-type]
        clock=lambda: 0.0,
        auth=static_auth(),
        web_dist=None,
    )

    @app.get("/v1/_t1_boom")
    async def boom() -> None:
        raise RuntimeError("boom")

    async with _client(app) as client:
        # Fix round 1: authenticated, so the request reaches the route (without
        # a token the guard answered 401 and the 500 path was never taken).
        resp = await client.get("/v1/_t1_boom", headers=bearer(ADMIN_TOKEN))
    assert resp.status_code == 500
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert resp.headers.get("content-security-policy") == CSP
