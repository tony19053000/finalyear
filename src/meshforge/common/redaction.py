"""Secret redaction for log records (ARCHITECTURE §25).

Used by :mod:`meshforge.common.logging` (which re-exports the public names). Two
layers:

1. *Key-based* (:func:`is_sensitive_key`, :func:`redact`): fields whose key looks
   sensitive and any ``SecretStr``/``SecretBytes`` value become ``"[REDACTED]"``,
   at any nesting depth.
2. *Value scrubbing* (:func:`scrub_text`) of free text: URL credentials,
   ``Bearer``/``Basic`` tokens, ``key=value`` / ``key: value`` pairs whose key is
   sensitive, and PEM private-key blocks (``-----BEGIN ... PRIVATE KEY-----`` up to
   the matching ``END`` line, or to the end of the text if unclosed), and V0.7
   join codes (``mf1-`` followed by at least 16 code characters).

``scrub_text`` runs on every string of every log record, so it must be
**linear-time** on adversarial input (a super-linear scrubber is a
denial-of-service on the whole process). Every regex here has a bounded or
lookbehind-anchored start so each input position is examined O(1) times, and the
value of a sensitive key is consumed by a manual scanner that never re-scans text.

Value rules for a *sensitive* key (when in doubt, over-redact):

* starts with ``'`` or ``"`` → redacted through the matching closing quote
  (backslash escapes honoured); unclosed → redacted to the end of the text;
* starts with ``{`` or ``[`` → redacted through the matching bracket (nesting and
  quoted strings honoured), scanning at most ``_BRACKET_SCAN_LIMIT`` characters;
  unbalanced → redacted to the end of the line *or* the end of the scanned window,
  whichever is farther (the window part keeps the scan linear);
* anything else → redacted up to the next whitespace character. Punctuation
  (``) ] } ; & , '`` …) does **not** end the value — it may be part of the secret.

Size cap: text longer than :data:`MAX_SCRUB_CHARS` is scrubbed for its first
``MAX_SCRUB_CHARS`` characters and the remainder is replaced by
``"[TRUNCATED <n> bytes]"`` (``n`` = UTF-8 size of the dropped part). Logging blobs
that large is itself a bug; the cap bounds scrubbing cost per string.

Known gaps (documented, not silent): keys longer than 64 characters (plus leading
dashes) are not recognised; a key must not be glued to a preceding word character
(``xpassword=…`` is not a key — and ``xpassword`` would not be sensitive anyway).
"""

from __future__ import annotations

import dataclasses
import functools
import itertools
import re
from collections.abc import Iterator, Mapping
from typing import Any, Final

from pydantic import BaseModel, SecretBytes, SecretStr

__all__ = [
    "MAX_SCRUB_CHARS",
    "REDACTED",
    "TRUNCATED",
    "is_sensitive_key",
    "redact",
    "scrub_text",
]

REDACTED: Final = "[REDACTED]"
TRUNCATED: Final = "[TRUNCATED]"
MAX_SCRUB_CHARS: Final = 64 * 1024
"""Strings longer than this are scrubbed up to here and the rest is dropped."""

_SENSITIVE_PARTS: Final = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "pwd",
        "secret",
        "secrets",
        "token",
        "apikey",
        "authorization",
        "auth",
        "cookie",
        "credential",
        "credentials",
        "privatekey",
        "dsn",
        "otp",
        "jwt",
    }
)
# Two-word combinations that are sensitive although neither word is on its own.
_SENSITIVE_PAIRS: Final = frozenset(
    {
        ("api", "key"),
        ("private", "key"),
        ("access", "key"),
        ("signing", "key"),
        ("session", "key"),
        ("set", "cookie"),
        ("database", "url"),
        ("database", "uri"),
        ("db", "url"),
        ("db", "uri"),
        ("connection", "string"),
        ("conn", "str"),
        ("conn", "string"),
        ("join", "code"),
        ("pairing", "code"),
        ("pin", "code"),
        ("pin", "number"),
    }
)
# ``refresh_<anything>`` (refresh_token, refresh_secret…) is sensitive.
_SENSITIVE_PREFIX_WORDS: Final = frozenset({"refresh"})
# Sensitive only as the *last* word part: ``pin``/``user_pin`` yes, but
# ``pin_memory`` (a PyTorch DataLoader flag we log as telemetry) no.
_SENSITIVE_LAST_PARTS: Final = frozenset({"pin"})

_CAMEL_RE: Final = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPLIT_RE: Final = re.compile(r"[_\-.\s]+")

# A whole string that is just an HTTP credential ("Bearer abc", "Basic dXNl…").
_AUTH_ONLY_RE: Final = re.compile(r"\s*(?:bearer|basic)\s+\S+\s*", re.IGNORECASE)
# "Bearer <token>" / "Basic <b64>" anywhere in a string.
_AUTH_ANY_RE: Final = re.compile(
    r"\b(?P<scheme>bearer|basic)(?P<sp>\s+)(?!\[REDACTED\])[A-Za-z0-9\-._~+/]+=*",
    re.IGNORECASE,
)
# Credentials embedded in a URL: scheme://user:password@host. The scheme can only
# start where no scheme character precedes it and every class is bounded, so each
# position is tried O(1) times (no quadratic rescans of long "a.a.a.…" runs).
_URL_CREDS_RE: Final = re.compile(
    r"(?P<pre>(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^\s/@:]{0,256}:)"
    r"(?P<pw>[^\s/@]{1,256})@"
)
# key=value / key: value / "key": value. Only the key and separator are matched; the
# value is scanned by _value_end. The lookbehind means a key can only start at the
# beginning of a word run (linear-time); leading dashes allow CLI flags (--token=x).
_KV_KEY_RE: Final = re.compile(
    r"""(?<![\w.\-])(?P<key>-*[A-Za-z_][\w.\-]{0,63})(?P<sep>["']?\s*[=:]\s*)"""
)
_NON_WS_RE: Final = re.compile(r"\S*")
# PEM private-key blocks (ADR-0013 §7). Only the BEGIN marker is matched by regex;
# the END marker is found with ``str.find`` so the scan is linear.
_PEM_BEGIN_RE: Final = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]{0,40}PRIVATE KEY)-----"
)
_PEM_END_PREFIX: Final = "-----END "
# V0.7 join codes (``common/join_code.py``): ``mf1-`` + base32 groups. A code is a
# single-use credential, so its shape is scrubbed wherever it appears (checklist
# 20). Lookbehind-anchored start + bounded class: linear.
_JOIN_CODE_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_])(?P<pre>[Mm][Ff][0-9]-)[A-Za-z0-9][A-Za-z0-9\-]{15,600}"
)
_BRACKET_SCAN_LIMIT: Final = 4096
_CLOSERS: Final = {"{": "}", "[": "]"}

_MAX_DEPTH: Final = 8
_SCALARS: Final = (int, float, bool, type(None))


def is_sensitive_key(key: object) -> bool:
    """True if a log/dict key names something that must never be logged."""
    if not isinstance(key, str) or not key:
        return False
    return _is_sensitive_str(key)


@functools.lru_cache(maxsize=4096)
def _is_sensitive_str(key: str) -> bool:
    # Cached: the same handful of keys is checked on every log record.
    parts = [p for p in _SPLIT_RE.split(_CAMEL_RE.sub("_", key).lower()) if p]
    if not parts:
        return False
    if parts == ["key"] or parts[-1] in _SENSITIVE_LAST_PARTS:
        return True
    if any(p in _SENSITIVE_PARTS for p in parts):
        return True
    if any(p in _SENSITIVE_PREFIX_WORDS for p in parts[:-1]):
        return True
    return any(pair in _SENSITIVE_PAIRS for pair in itertools.pairwise(parts))


def _quoted_end(text: str, start: int) -> int | None:
    """Index just past the quote closing ``text[start]``, or None if unclosed."""
    quote = text[start]
    i = start + 1
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1
    return None


def _bracket_end(text: str, start: int) -> int | None:
    """Index just past the bracket matching ``text[start]`` (``{``/``[``), or None.

    Counts nesting of ``{}``/``[]`` and skips quoted strings; scans at most
    ``_BRACKET_SCAN_LIMIT`` characters.
    """
    stack = [_CLOSERS[text[start]]]
    i = start + 1
    limit = min(len(text), start + _BRACKET_SCAN_LIMIT)
    while i < limit:
        ch = text[i]
        if ch in "\"'":
            end = _quoted_end(text, i)
            if end is None or end > limit:
                return None
            i = end
            continue
        if ch == "\\":
            i += 2
            continue
        if ch in _CLOSERS:
            stack.append(_CLOSERS[ch])
        elif ch in "}]":
            if ch != stack[-1]:
                return None  # mismatched -> treat as unbalanced (over-redact)
            stack.pop()
            if not stack:
                return i + 1
        i += 1
    return None


def _redacted_value(text: str, start: int) -> tuple[str, int]:
    """Replacement for the sensitive value at ``text[start:]`` and where it ends."""
    if start >= len(text):
        return "", start
    first = text[start]
    if first in "\"'":
        end = _quoted_end(text, start)
        if end is None:
            return f"{first}{REDACTED}", len(text)
        return f"{first}{REDACTED}{first}", end
    if first in _CLOSERS:
        end = _bracket_end(text, start)
        if end is None:
            eol = text.find("\n", start)
            line_end = len(text) if eol == -1 else eol
            window_end = min(len(text), start + _BRACKET_SCAN_LIMIT)
            return REDACTED, max(line_end, window_end)
        return REDACTED, end
    match = _NON_WS_RE.match(text, start)
    if match is None:  # pragma: no cover - \S* always matches
        raise AssertionError("unreachable: \\S* always matches")
    return REDACTED, match.end()


def _scrub_kv(text: str) -> str:
    """Mask the value of every ``key=value`` / ``key: value`` whose key is sensitive."""
    if "=" not in text and ":" not in text:
        return text
    out: list[str] = []
    emitted = 0  # end of the text already copied to ``out``
    pos = 0
    while (match := _KV_KEY_RE.search(text, pos)) is not None:
        pos = match.end()
        if not is_sensitive_key(match["key"].lstrip("-")):
            continue
        replacement, end = _redacted_value(text, pos)
        if end == pos:
            continue  # empty value ("password=" at end of text)
        out.append(text[emitted:pos])
        out.append(replacement)
        emitted = pos = end
    out.append(text[emitted:])
    return "".join(out)


def _scrub_pem(text: str) -> str:
    """Replace every PEM private-key block with ``-----BEGIN <label>----- [REDACTED]``."""
    if "PRIVATE KEY-----" not in text:
        return text
    out: list[str] = []
    emitted = 0
    pos = 0
    while (match := _PEM_BEGIN_RE.search(text, pos)) is not None:
        end_marker = f"{_PEM_END_PREFIX}{match['label']}-----"
        end = text.find(end_marker, match.end())
        stop = len(text) if end == -1 else end + len(end_marker)
        out.append(text[emitted : match.end()])
        out.append(f" {REDACTED} ")
        emitted = pos = stop
    out.append(text[emitted:])
    return "".join(out)


def scrub_text(text: str) -> str:
    """Mask known secret *shapes* inside free text (see module docstring).

    URL credentials, ``Bearer``/``Basic`` tokens and sensitive ``key=value`` pairs
    are replaced; anything else is returned unchanged. Linear-time; text beyond
    :data:`MAX_SCRUB_CHARS` is dropped and replaced by a ``[TRUNCATED n bytes]``
    marker.
    """
    suffix = ""
    if len(text) > MAX_SCRUB_CHARS:
        dropped = text[MAX_SCRUB_CHARS:].encode("utf-8", "surrogatepass")
        suffix = f"[TRUNCATED {len(dropped)} bytes]"
        text = text[:MAX_SCRUB_CHARS]
    if _AUTH_ONLY_RE.fullmatch(text):
        return REDACTED + suffix
    text = _scrub_pem(text)
    text = _JOIN_CODE_RE.sub(lambda m: f"{m['pre']}{REDACTED}", text)
    text = _URL_CREDS_RE.sub(lambda m: f"{m['pre']}{REDACTED}@", text)
    text = _AUTH_ANY_RE.sub(lambda m: f"{m['scheme']}{m['sp']}{REDACTED}", text)
    return _scrub_kv(text) + suffix


def _redact_fields(pairs: Iterator[tuple[Any, Any]], depth: int) -> dict[Any, Any]:
    return {
        k: REDACTED if is_sensitive_key(k) else redact(v, depth + 1) for k, v in pairs
    }


def redact(value: Any, _depth: int = 0) -> Any:
    """Return ``value`` with secrets masked.

    Recurses into mappings, lists/tuples/sets, namedtuples (by field name),
    pydantic models and dataclasses (both become dicts). Strings are passed through
    :func:`scrub_text`. Containers nested deeper than ``_MAX_DEPTH`` become
    :data:`TRUNCATED`.

    Shape changes: a namedtuple whose constructor cannot be re-called with its
    fields becomes a ``dict`` keyed by field name; a set whose redacted items are
    unhashable (e.g. a model turned into a dict) becomes a ``list`` (order is then
    arbitrary, as for any set).
    """
    if isinstance(value, SecretStr | SecretBytes):
        return REDACTED
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, _SCALARS):
        return value
    if _depth >= _MAX_DEPTH:
        return TRUNCATED
    if isinstance(value, Mapping):
        return _redact_fields(iter(value.items()), _depth)
    if isinstance(value, BaseModel):
        try:
            dumped = value.model_dump()
        except Exception:  # noqa: BLE001 - never let logging crash on a bad model
            return f"<{type(value).__name__}: not renderable>"
        return _redact_fields(iter(dumped.items()), _depth)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _redact_fields(
            ((f.name, getattr(value, f.name, None)) for f in dataclasses.fields(value)),
            _depth,
        )
    if isinstance(value, tuple) and hasattr(value, "_fields"):  # namedtuple
        fields = _redact_fields(zip(value._fields, value, strict=False), _depth)
        try:
            return type(value)(*fields.values())
        except Exception:  # noqa: BLE001 - custom __new__; keep the field names
            return fields
    if isinstance(value, list | tuple):
        items = [redact(v, _depth + 1) for v in value]
        return items if isinstance(value, list) else tuple(items)
    if isinstance(value, set | frozenset):
        items = [redact(v, _depth + 1) for v in value]
        try:
            return type(value)(items) if type(value) in (set, frozenset) else set(items)
        except TypeError:  # redacted items may be unhashable -> list (documented)
            return items
    return value
