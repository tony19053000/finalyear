"""A tiny stdlib client for the controller's read-only REST surface (``/v1``).

Only ``urllib``: the lab kit must run after a plain ``uv sync`` on native Windows
(lab/README.md). Every failure is one of two typed exceptions, so the CLI can print
one clear line and exit instead of showing a traceback:

* :class:`ControllerUnreachable` (``failure_type`` ``controller_unreachable``) -
  refused, reset, timed out, DNS failure: the controller is not answering.
* :class:`ApiError` - it answered, but not with what we asked for: an HTTP error
  (``failure_type`` = the envelope's ``code``, e.g. ``not_found``) or a body that
  is not the JSON object we expect (``bad_response``).
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Final

DEFAULT_URL: Final = "http://127.0.0.1:47110"
URL_ENV: Final = "MESHFORGE_CLI__CONTROLLER_URL"
"""The same variable ``compute`` reads (docs/OPERATIONS.md §1)."""
DEFAULT_TIMEOUT_S: Final = 2.0
UNREACHABLE: Final = "controller_unreachable"
BAD_RESPONSE: Final = "bad_response"
SESSIONS_LIMIT: Final = 500
"""The API maximum for ``/v1/sessions`` (ARCHITECTURE §20)."""

type JsonObject = dict[str, Any]


class FabricApiError(Exception):
    """Base of every failure talking to the controller."""

    def __init__(self, failure_type: str, message: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type


class ControllerUnreachable(FabricApiError):  # noqa: N818 (reads as a state)
    """Nothing answered at the controller URL (refused, reset, timeout, DNS)."""

    def __init__(self, message: str) -> None:
        super().__init__(UNREACHABLE, message)


class ApiError(FabricApiError):
    """The controller answered with an error or with an unexpected body."""

    def __init__(self, failure_type: str, message: str, *, status: int | None) -> None:
        super().__init__(failure_type, message)
        self.status = status


def default_url() -> str:
    return os.environ.get(URL_ENV) or DEFAULT_URL


def normalise_url(url: str) -> str:
    """``http(s)://host:port`` without a trailing slash; ``ValueError`` otherwise."""
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"not an http(s) URL with a host: {url!r}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"the controller URL must not carry a query: {url!r}")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
    )


class FabricApi:
    """GET-only client; one short-lived connection per request (no pooling).

    A fresh connection per poll is deliberate for the lab: it is what tells us, at
    100 ms resolution, that the controller went away or came back.
    """

    def __init__(self, base_url: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.base_url = normalise_url(base_url)
        self.timeout_s = timeout_s

    def get(self, path: str, **query: str | int) -> JsonObject:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 (scheme checked)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ControllerUnreachable(
                f"cannot reach the controller at {self.base_url} ({_reason(exc)})"
            ) from None
        return _object(body, path, status=200)

    # ------------------------------------------------------------ endpoints
    def healthz(self) -> JsonObject:
        return self.get("/healthz")

    def nodes(self) -> list[JsonObject]:
        return _list(self.get("/v1/nodes"), "nodes")

    def node(self, node_id: str) -> JsonObject:
        return self.get("/v1/nodes/" + urllib.parse.quote(node_id, safe=""))

    def sessions(
        self, node_id: str | None = None, *, limit: int = SESSIONS_LIMIT
    ) -> list[JsonObject]:
        query: dict[str, str | int] = {"limit": limit}
        if node_id is not None:
            query["node_id"] = node_id
        return _list(self.get("/v1/sessions", **query), "sessions")

    def live_samples(self, node_id: str, *, live_limit: int = 30) -> list[JsonObject]:
        doc = self.get(
            "/v1/nodes/" + urllib.parse.quote(node_id, safe="") + "/resources",
            limit=1,
            live_limit=live_limit,
        )
        return _list(doc, "live")


def _reason(exc: BaseException) -> str:
    inner = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(inner, TimeoutError | socket.timeout):
        return "timed out"
    if isinstance(inner, ConnectionRefusedError):
        return "connection refused"
    if isinstance(inner, ConnectionResetError):
        return "connection reset"
    if isinstance(inner, socket.gaierror):
        return "host name not found"
    text = str(inner).strip()
    return text or type(inner).__name__


def _http_error(exc: urllib.error.HTTPError) -> ApiError:
    code, message = f"http_{exc.code}", exc.reason or ""
    try:
        doc = json.loads(exc.read() or b"null")
    except (OSError, ValueError):
        doc = None
    error = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or code)
        message = str(error.get("message") or message)
    return ApiError(
        code, f"controller answered HTTP {exc.code}: {message}", status=exc.code
    )


def _object(body: bytes, path: str, *, status: int) -> JsonObject:
    try:
        doc = json.loads(body)
    except ValueError:
        raise ApiError(
            BAD_RESPONSE, f"{path}: the body is not JSON", status=status
        ) from None
    if not isinstance(doc, dict):
        raise ApiError(BAD_RESPONSE, f"{path}: expected a JSON object", status=status)
    return doc


def _list(doc: JsonObject, key: str) -> list[JsonObject]:
    items = doc.get(key)
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ApiError(
            BAD_RESPONSE, f"expected a list of objects under {key!r}", status=200
        )
    return items
