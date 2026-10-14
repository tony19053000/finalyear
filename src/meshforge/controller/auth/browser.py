"""The dashboard's browser session: cookies, CSRF and origin rules (V0.8 G1, D170).

A browser never holds an API token. ``POST /v1/session`` (name + password, the
login throttle of D159) answers with two cookies:

* :data:`SESSION_COOKIE` - the ``mfbs_`` session token: ``HttpOnly; Secure;
  SameSite=Strict; Path=/v1``. Script cannot read it (XSS cannot steal it), a
  cross-site page cannot make the browser send it, it goes only to the API.
* :data:`CSRF_COOKIE` - the CSRF value: ``Secure; SameSite=Strict; Path=/``,
  **readable** by the dashboard's script, which copies it into the
  :data:`CSRF_HEADER` header of every mutating request (double submit).

The CSRF value is not random state of its own: it is ``HMAC-SHA256(session
token, "meshforge-csrf-v1")`` (base64url), so it is bound to the session -
a value planted by a sibling site (cookie tossing) or kept from an older
session never matches - and nothing extra is stored. Knowing it reveals
nothing about the session token (HMAC is one-way).

Rules for a request authenticated **by the cookie** (:func:`check_cookie_request`;
a request with an ``Authorization`` header never uses the cookie):

* a mutating method (anything but ``GET``/``HEAD``/``OPTIONS``) needs the header
  equal to the cookie **and** to the HMAC of the presented session, compared in
  constant time, and an ``Origin`` - if the browser sent one - equal to the
  controller's own origin; else 403 ``csrf``;
* a WebSocket handshake needs an ``Origin`` equal to the controller's own
  origin (browsers always send one): ``SameSite`` stops cross-*site* pages, but
  another port of the same host is the same site, so cross-site WebSocket
  hijacking is closed by the origin, not the cookie.

``POST /v1/session`` itself (no session yet) refuses a cross-origin ``Origin``
or ``Sec-Fetch-Site: cross-site`` and anything but ``application/json`` - an
HTML form cannot send JSON, so a hostile page cannot log the browser into the
attacker's account (login CSRF). A fresh token is minted on every login and a
cookie the browser brought is never adopted (no session fixation).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Final

from starlette.requests import HTTPConnection
from starlette.responses import Response

__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SAFE_METHODS",
    "SESSION_COOKIE",
    "clear_session_cookies",
    "cross_origin",
    "csrf_ok",
    "csrf_value",
    "own_origin",
    "set_session_cookies",
]

SESSION_COOKIE: Final = "mf_session"
CSRF_COOKIE: Final = "mf_csrf"
CSRF_HEADER: Final = "x-meshforge-csrf"
SESSION_COOKIE_PATH: Final = "/v1"
CSRF_COOKIE_PATH: Final = "/"
SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_CSRF_CONTEXT: Final = b"meshforge-csrf-v1"
_MAX_CSRF_CHARS: Final = 128


def csrf_value(session_token: str) -> str:
    """The CSRF value bound to ``session_token`` (module docstring)."""
    mac = hmac.new(
        session_token.encode("ascii", "surrogateescape"), _CSRF_CONTEXT, hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def own_origin(conn: HTTPConnection) -> str | None:
    """``scheme://host[:port]`` this request was addressed to (``Host``)."""
    hosts = conn.headers.getlist("host")
    if len(hosts) != 1 or not hosts[0]:
        return None
    scheme = {"ws": "http", "wss": "https"}.get(conn.url.scheme, conn.url.scheme)
    return f"{scheme}://{hosts[0].lower()}"


def cross_origin(conn: HTTPConnection, *, require: bool) -> bool:
    """True when the request's ``Origin`` is not the controller's own (or is
    missing while ``require``), or ``Sec-Fetch-Site`` says ``cross-site``."""
    if conn.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return True
    origins = conn.headers.getlist("origin")
    if not origins:
        return require
    if len(origins) != 1:
        return True
    mine = own_origin(conn)
    return mine is None or not hmac.compare_digest(
        origins[0].lower().encode("utf-8", "surrogateescape"),
        mine.encode("utf-8", "surrogateescape"),
    )


def csrf_ok(conn: HTTPConnection, session_token: str) -> bool:
    """Double submit: header == cookie == HMAC(session) (constant time)."""
    headers = conn.headers.getlist(CSRF_HEADER)
    cookie = conn.cookies.get(CSRF_COOKIE)
    if len(headers) != 1 or not cookie:
        return False
    sent = headers[0]
    if len(sent) > _MAX_CSRF_CHARS or len(cookie) > _MAX_CSRF_CHARS:
        return False
    expected = csrf_value(session_token).encode("ascii")
    sent_b = sent.encode("utf-8", "surrogateescape")
    cookie_b = cookie.encode("utf-8", "surrogateescape")
    # Both compares always run (no early exit on the first).
    first = hmac.compare_digest(sent_b, cookie_b)
    second = hmac.compare_digest(sent_b, expected)
    return first and second


def set_session_cookies(response: Response, token: str, *, max_age_s: int) -> None:
    """The session cookie and its CSRF companion (module docstring)."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age_s,
        path=SESSION_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_value(token),
        max_age=max_age_s,
        path=CSRF_COOKIE_PATH,
        secure=True,
        httponly=False,
        samesite="strict",
    )


def clear_session_cookies(response: Response) -> None:
    """Expire both cookies in the browser (logout, or a dead session)."""
    response.delete_cookie(
        SESSION_COOKIE,
        path=SESSION_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )
    response.delete_cookie(
        CSRF_COOKIE, path=CSRF_COOKIE_PATH, secure=True, samesite="strict"
    )
