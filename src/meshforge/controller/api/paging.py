"""Keyset page cursors for ``/v1`` lists (V0.8 G1, D174).

A cursor names the last row of a page by its sort key ``(created_at, id)``;
the next page starts strictly after it. It is **opaque** to clients (base64url
of ``v1|<ISO 8601 created_at>|<id>``) but not secret: it carries nothing a
caller could not read from the page itself, and every list still applies the
caller's own visibility rule - a cursor never widens what a user may see.
Anything that does not decode exactly is 400, never "start from the top".
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
from collections.abc import Callable
from typing import Final

from meshforge.common.errors import InvalidIdError
from meshforge.controller.api.errors import ApiError

__all__ = ["MAX_CURSOR_CHARS", "decode_cursor", "encode_cursor"]

MAX_CURSOR_CHARS: Final = 160
_VERSION: Final = "v1"


def encode_cursor(created_at: dt.datetime, row_id: str) -> str:
    raw = f"{_VERSION}|{created_at.astimezone(dt.UTC).isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).rstrip(b"=").decode("ascii")


def decode_cursor(
    cursor: str, *, validate_id: Callable[[str], object]
) -> tuple[dt.datetime, str]:
    """``(created_at, id)`` of ``cursor``, or 400 ``invalid_input``."""
    bad = ApiError.invalid("cursor is not a cursor from this list's next_cursor")
    if not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise bad
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
        version, at_text, row_id = raw.split("|")
        at = dt.datetime.fromisoformat(at_text)
        validate_id(row_id)
    except (
        ValueError,
        UnicodeError,
        binascii.Error,
        InvalidIdError,
    ):
        raise bad from None
    if version != _VERSION or at.tzinfo is None:
        raise bad
    return at, row_id
