"""V0.8 G1 (D170): the browser-session rules without a server - CSRF double
submit bound to the session, origin checks, cookie flags, token shapes and
log scrubbing of the new ``mfbs_`` credential (checklist 19, 20, 22)."""

from __future__ import annotations

import pytest
from starlette.requests import HTTPConnection
from starlette.responses import Response

from meshforge.common.api_token import (
    is_api_token,
    is_browser_session_token,
    new_api_token,
    new_browser_session_token,
)
from meshforge.common.redaction import scrub_text
from meshforge.controller.auth import browser

pytestmark = [pytest.mark.unit, pytest.mark.security]

TOKEN = new_browser_session_token().get_secret_value()


def _conn(
    headers: list[tuple[str, str]], *, scheme: str = "https", kind: str = "http"
) -> HTTPConnection:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers]
    scope = {
        "type": kind,
        "scheme": scheme,
        "method": "POST",
        "path": "/v1/x",
        "headers": raw,
        "server": ("127.0.0.1", 47110),
    }
    return HTTPConnection(scope)


HOST = ("host", "ctl.lan:47110")


def test_csrf_value_is_bound_to_the_session() -> None:
    other = new_browser_session_token().get_secret_value()
    assert browser.csrf_value(TOKEN) == browser.csrf_value(TOKEN)
    assert browser.csrf_value(TOKEN) != browser.csrf_value(other)
    assert TOKEN not in browser.csrf_value(TOKEN)


@pytest.mark.parametrize(
    ("header", "cookie", "ok"),
    [
        ("good", "good", True),
        (None, "good", False),
        ("good", None, False),
        ("good", "other", False),  # header != cookie
        ("tossed", "tossed", False),  # equal, but not this session's value
        ("", "", False),
        ("good" * 50, "good" * 50, False),  # over the length cap
    ],
)
def test_csrf_double_submit(header: str | None, cookie: str | None, ok: bool) -> None:
    good = browser.csrf_value(TOKEN)
    values = {"good": good, "other": browser.csrf_value("mfbs_" + "x" * 43)}
    headers = [HOST]
    if header is not None:
        headers.append((browser.CSRF_HEADER, values.get(header, header)))
    if cookie is not None:
        headers.append(("cookie", f"mf_csrf={values.get(cookie, cookie)}"))
    assert browser.csrf_ok(_conn(headers), TOKEN) is ok


def test_two_csrf_headers_are_refused() -> None:
    good = browser.csrf_value(TOKEN)
    conn = _conn(
        [
            HOST,
            (browser.CSRF_HEADER, good),
            (browser.CSRF_HEADER, good),
            ("cookie", f"mf_csrf={good}"),
        ]
    )
    assert not browser.csrf_ok(conn, TOKEN)


@pytest.mark.parametrize(
    ("headers", "require", "cross"),
    [
        ([HOST, ("origin", "https://ctl.lan:47110")], True, False),
        ([HOST, ("origin", "HTTPS://CTL.LAN:47110")], True, False),
        ([HOST], False, False),  # no Origin: allowed only when not required
        ([HOST], True, True),
        ([HOST, ("origin", "https://ctl.lan:8443")], False, True),  # same site!
        ([HOST, ("origin", "http://ctl.lan:47110")], False, True),  # scheme
        ([HOST, ("origin", "https://evil.example")], False, True),
        ([HOST, ("origin", "null")], False, True),
        (
            [HOST, ("origin", "https://ctl.lan:47110"), ("origin", "https://x")],
            False,
            True,
        ),
        ([("origin", "https://ctl.lan:47110")], False, True),  # no Host
        ([HOST, ("sec-fetch-site", "cross-site")], False, True),
        ([HOST, ("sec-fetch-site", "same-origin")], False, False),
    ],
)
def test_origin_rules(
    headers: list[tuple[str, str]], require: bool, cross: bool
) -> None:
    assert browser.cross_origin(_conn(headers), require=require) is cross


def test_websocket_origin_maps_wss_to_https() -> None:
    conn = _conn(
        [HOST, ("origin", "https://ctl.lan:47110")], scheme="wss", kind="websocket"
    )
    assert browser.own_origin(conn) == "https://ctl.lan:47110"
    assert not browser.cross_origin(conn, require=True)


def test_cookie_flags() -> None:
    response = Response()
    browser.set_session_cookies(response, TOKEN, max_age_s=600)
    raw = response.headers.getlist("set-cookie")
    session = next(c for c in raw if c.startswith("mf_session="))
    csrf = next(c for c in raw if c.startswith("mf_csrf="))
    for flag in ("HttpOnly", "Secure", "SameSite=strict", "Path=/v1", "Max-Age=600"):
        assert flag in session, (flag, session)
    assert "HttpOnly" not in csrf
    for flag in ("Secure", "SameSite=strict", "Path=/;", "Max-Age=600"):
        assert flag in csrf + ";", (flag, csrf)
    cleared = Response()
    browser.clear_session_cookies(cleared)
    for cookie in cleared.headers.getlist("set-cookie"):
        assert "Max-Age=0" in cookie


def test_token_kinds_have_disjoint_shapes() -> None:
    api = new_api_token().get_secret_value()
    assert is_browser_session_token(TOKEN) and not is_api_token(TOKEN)
    assert is_api_token(api) and not is_browser_session_token(api)
    assert not is_browser_session_token(TOKEN[:-1])
    assert not is_browser_session_token(None)


def test_a_session_token_is_scrubbed_from_log_text() -> None:
    """Mutation check: drop ``bs`` from the scrubber's prefix -> this fails."""
    text = f"cookie mf_session={TOKEN}; trailing words {TOKEN[:30]}"
    scrubbed = scrub_text(text)
    assert TOKEN[5:] not in scrubbed
    assert TOKEN[5:30] not in scrubbed
