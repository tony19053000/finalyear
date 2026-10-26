"""Serving the dashboard SPA from the controller (V0.8 G1, D169, ADR-0017).

:class:`SpaMiddleware` sits in front of the API (an ASGI middleware, so it
never changes how ``/v1`` routes, authenticates or answers):

* ``/v1`` and everything under it, and ``/healthz``, go to the API untouched -
  the SPA can never shadow an API path, also not an unknown one (that stays the
  API's own 404 envelope);
* any other ``GET``/``HEAD`` is the dashboard: a file of ``dist/`` when one
  matches exactly (resolved inside ``dist/`` - ``..``, absolute paths, a
  symlink out of it and hidden files are refused), else ``index.html`` (client
  routing) - but a missing *file* (the last segment has a dot: ``/x.js``) is
  the API's 404 envelope, never HTML served as script;
* other methods outside ``/v1`` -> 405;
* no ``dist/index.html`` (the frontend was not built) -> a small static page
  saying so, status 200 - never a 500 (a dotted path is still a 404).

Caching: files under ``/assets/`` carry a content hash in their name (Vite) ->
``public, max-age=31536000, immutable``; ``index.html`` and the "not built"
page -> ``no-cache`` (always revalidated, so a new build is picked up at once);
other files -> ``no-cache``.

:class:`SecurityHeadersMiddleware` adds to **every** response (API too): a
strict ``Content-Security-Policy`` (``default-src 'self'``, no inline script,
``frame-ancestors 'none'``), ``X-Content-Type-Options: nosniff``,
``Referrer-Policy: no-referrer``, ``X-Frame-Options: DENY``. Set only where the
response has not set its own.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Final

from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meshforge.common.errors import FailureType
from meshforge.common.web_security import CSP, SECURITY_HEADERS
from meshforge.controller.api.errors import error_response
from meshforge.controller.web import DIST_DIR

__all__ = [
    "CSP",
    "DEFAULT_DIST",
    "NOT_BUILT_HTML",
    "SECURITY_HEADERS",
    "SecurityHeadersMiddleware",
    "SpaMiddleware",
]

DEFAULT_DIST: Final = DIST_DIR
IMMUTABLE: Final = "public, max-age=31536000, immutable"
NO_CACHE: Final = "no-cache"
NOT_BUILT_HTML: Final = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>MeshForge</title></head>
<body><h1>MeshForge dashboard not built</h1>
<p>The controller and its API (<code>/v1</code>) are running, but this
installation has no built dashboard. Build it once:
<code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code>, then reload.
The <code>compute</code> CLI works without it.</p></body></html>
"""
_API_PREFIX: Final = "/v1"
_API_EXACT: Final = frozenset({"/v1", "/healthz"})
_MAX_PATH_CHARS: Final = 1024


def _not_found() -> Response:
    """A missing file: the API's own 404 envelope, never HTML."""
    return error_response(FailureType.NOT_FOUND, "no such file", 404)


def _is_api(path: str) -> bool:
    return path in _API_EXACT or path.startswith(_API_PREFIX + "/")


class SpaMiddleware:
    """See module docstring."""

    def __init__(self, app: ASGIApp, *, dist: Path = DEFAULT_DIST) -> None:
        self.app = app
        self.dist = dist

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _is_api(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return
        response = self._respond(str(scope.get("method", "GET")), str(scope["path"]))
        await response(scope, receive, send)

    def _respond(self, method: str, path: str) -> Response:
        if method not in ("GET", "HEAD"):
            return PlainTextResponse(
                "method not allowed", status_code=405, headers={"allow": "GET, HEAD"}
            )
        last = path.rsplit("/", 1)[-1]
        index = self.dist / "index.html"
        if not index.is_file():
            if "." in last:
                return _not_found()
            return Response(
                NOT_BUILT_HTML,
                media_type="text/html; charset=utf-8",
                headers={"cache-control": NO_CACHE},
            )
        found = self._file(path)
        if found is not None:
            cache = IMMUTABLE if path.startswith("/assets/") else NO_CACHE
            media = mimetypes.guess_type(found.name)[0] or "application/octet-stream"
            return FileResponse(
                found, media_type=media, headers={"cache-control": cache}
            )
        if "." in last:
            return _not_found()
        return FileResponse(
            index,
            media_type="text/html; charset=utf-8",
            headers={"cache-control": NO_CACHE},
        )

    def _file(self, path: str) -> Path | None:
        """The file of ``dist/`` that ``path`` names exactly, or None."""
        if len(path) > _MAX_PATH_CHARS or "\x00" in path or "\\" in path:
            return None
        parts = [p for p in path.split("/") if p]
        if not parts or any(p in (".", "..") or p.startswith(".") for p in parts):
            return None
        root = self.dist.resolve()
        try:
            candidate = (root / os.path.join(*parts)).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return None
        return candidate


class SecurityHeadersMiddleware:
    """See module docstring (headers on every HTTP response)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in SECURITY_HEADERS:
                    key = name.encode("latin-1")
                    if key not in present:
                        headers.append((key, value.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, with_headers)
