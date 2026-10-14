"""The shape of a REST API token (V0.7 G4, D141) - shared by controller and CLI.

An API token is what ``POST /v1/auth/login`` returns and ``compute`` sends as
``Authorization: Bearer <token>``: :data:`API_TOKEN_PREFIX` + 43 URL-safe base64
characters of ``secrets.token_bytes(32)`` (256 bits). The prefix lets the
controller tell it from the legacy fabric token without a database round trip,
lets the log scrubber recognise it anywhere in free text
(:mod:`meshforge.common.redaction`), and makes a leaked token easy to grep for.

The controller stores only :func:`api_token_hash` (SHA-256, 64 lower-case hex) -
never the token. The CLI keeps it in a private file of its config dir
(``compute login``); neither side ever logs it or puts it in a URL.

**Browser sessions (V0.8 G1, D170)** are the same kind of credential with their
own prefix, :data:`BROWSER_SESSION_PREFIX` (``mfbs_``): they live only in the
dashboard's ``HttpOnly`` cookie, are accepted only from that cookie (never as a
``Bearer`` header) and an API token is never accepted from the cookie. Same
256 bits, same SHA-256-only storage, same scrubbing by shape.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Final

from pydantic import SecretStr

__all__ = [
    "API_TOKEN_CHARS",
    "API_TOKEN_PREFIX",
    "BROWSER_SESSION_PREFIX",
    "api_token_hash",
    "is_api_token",
    "is_browser_session_token",
    "new_api_token",
    "new_browser_session_token",
]

API_TOKEN_PREFIX: Final = "mfat_"  # noqa: S105 - a format prefix, not a secret
"""``m``esh``f``orge ``a``pi ``t``oken."""
_RANDOM_CHARS: Final = 43
API_TOKEN_CHARS: Final = len(API_TOKEN_PREFIX) + _RANDOM_CHARS
_SHAPE: Final = re.compile(r"mfat_[A-Za-z0-9_-]{43}")
BROWSER_SESSION_PREFIX: Final = "mfbs_"
"""``m``esh``f``orge ``b``rowser ``s``ession."""
_SESSION_SHAPE: Final = re.compile(r"mfbs_[A-Za-z0-9_-]{43}")


def new_api_token() -> SecretStr:
    """A fresh 256-bit API token (from :mod:`secrets`)."""
    return SecretStr(API_TOKEN_PREFIX + secrets.token_urlsafe(32))


def is_api_token(text: str | None) -> bool:
    """True when ``text`` has an API token's exact shape (says nothing of validity)."""
    return text is not None and _SHAPE.fullmatch(text) is not None


def new_browser_session_token() -> SecretStr:
    """A fresh 256-bit browser session token (from :mod:`secrets`)."""
    return SecretStr(BROWSER_SESSION_PREFIX + secrets.token_urlsafe(32))


def is_browser_session_token(text: str | None) -> bool:
    """True when ``text`` has a browser session token's exact shape."""
    return text is not None and _SESSION_SHAPE.fullmatch(text) is not None


def api_token_hash(token: str) -> str:
    """SHA-256 hex of the token: what the ``api_token`` row stores and looks up."""
    return hashlib.sha256(token.encode("ascii", "surrogateescape")).hexdigest()
