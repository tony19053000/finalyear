"""Join codes: what a human copies from the controller to a new laptop (V0.7 D135).

A code carries everything a laptop needs to join, and nothing that helps anyone
who does not hold it:

* the **enrolment listener's address** (``host:port``; V0.7 G2's interim TLS
  listener, D150 - the single session port from G3 on);
* a **128-bit single-use token** (``secrets``); the controller stores only its
  SHA-256 (:func:`token_hash`);
* a **128-bit CA pin**: the first 128 bits of SHA-256 over the fabric CA
  certificate's DER (:func:`meshforge.common.pki.short_fingerprint`). The agent
  checks the CA it is shown against it **before** it sends the token (D136).

Binary layout (big-endian), then base32 (RFC 4648 alphabet ``A-Z2-7``, no
padding), prefixed ``mf1-`` and grouped in fives with ``-`` for reading aloud::

    version   1 byte    = 1
    host      1 byte kind: 0x04 + 4 bytes IPv4 | 0x06 + 16 bytes IPv6 |
                           0x64 ('d') + 1 length byte + 1-253 ASCII host-name bytes
    port      2 bytes   1-65535
    token     16 bytes
    ca_pin    16 bytes
    checksum  4 bytes   = SHA-256(everything above)[:4]

An IPv4 code is 44 bytes = 71 base32 characters, ``mf1-`` + 15 groups. The
checksum makes a typo a ``bad_checksum`` ("this is not a valid code") on the
laptop, never a "wrong token" round trip to the controller; decoding is
case-insensitive and forgives the look-alikes ``0/O``, ``1/I``, ``8/B``.

:class:`JoinCode` keeps the token out of ``repr``; code strings must never be
logged (``common.redaction`` scrubs the ``mf1-...`` shape anyway, checklist 20).
Pure: no I/O.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import re
import secrets
import struct
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = [
    "CODE_VERSION",
    "MAX_HOST_NAME_BYTES",
    "PIN_BYTES",
    "TOKEN_BYTES",
    "JoinCode",
    "JoinCodeError",
    "JoinCodeReason",
    "decode",
    "encode",
    "new_token",
    "parse_ttl",
    "token_hash",
]

CODE_VERSION: Final = 1
TOKEN_BYTES: Final = 16
PIN_BYTES: Final = 16
CHECKSUM_BYTES: Final = 4
MAX_HOST_NAME_BYTES: Final = 253
MAX_CODE_CHARS: Final = 1024
"""Anything longer is refused before decoding (a code is < 500 characters)."""
_PREFIX: Final = "mf"
_GROUP: Final = 5
_KIND_V4: Final = 0x04
_KIND_V6: Final = 0x06
_KIND_NAME: Final = 0x64
_PIN_RE: Final = re.compile(r"[0-9a-f]{32}")
_NAME_LABEL_RE: Final = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)")
_LOOKALIKES: Final = str.maketrans({"0": "O", "1": "I", "8": "B"})
_B32_RE: Final = re.compile(r"[A-Z2-7]+")


class JoinCodeReason(StrEnum):
    """Why a code was refused (stable codes for messages and tests)."""

    MALFORMED = "malformed"
    TRUNCATED = "truncated"
    BAD_CHECKSUM = "bad_checksum"
    UNSUPPORTED_VERSION = "unsupported_version"
    BAD_ADDRESS = "bad_address"


class JoinCodeError(MeshForgeError):
    """A join code could not be read or built. Messages never echo the code."""

    default_failure_type = FailureType.INVALID_INPUT
    exit_code = ExitCode.USAGE_ERROR

    def __init__(self, reason: JoinCodeReason, message: str) -> None:
        super().__init__(message, details={"reason": reason.value})
        self.reason = reason


@dataclass(frozen=True, slots=True)
class JoinCode:
    """A decoded join code. ``token`` is excluded from ``repr``."""

    host: str
    """IP address (no brackets) or lower-case host name."""
    port: int
    token: bytes = field(repr=False)
    ca_pin: str
    """32 lower-case hex characters (128 bits)."""

    @property
    def address(self) -> str:
        """``host:port`` for a gRPC target (IPv6 in brackets)."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"

    @property
    def token_hash(self) -> str:
        return token_hash(self.token)


def new_token() -> bytes:
    """A fresh 128-bit token from :mod:`secrets`."""
    return secrets.token_bytes(TOKEN_BYTES)


def token_hash(token: bytes) -> str:
    """Lower-case hex SHA-256 of a token: what the controller stores."""
    return hashlib.sha256(token).hexdigest()


# ------------------------------------------------------------------------- encode


def _host_bytes(host: str) -> bytes:
    text = host.strip().removeprefix("[").removesuffix("]")
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        name = text.lower().rstrip(".")
        raw = name.encode("ascii", "ignore")
        if (
            not name
            or len(raw) != len(name)
            or len(raw) > MAX_HOST_NAME_BYTES
            or not all(_NAME_LABEL_RE.fullmatch(label) for label in name.split("."))
        ):
            raise JoinCodeError(
                JoinCodeReason.BAD_ADDRESS,
                f"{host!r} is not an IP address or host name",
            ) from None
        return bytes([_KIND_NAME, len(raw)]) + raw
    if ip.is_unspecified:
        raise JoinCodeError(
            JoinCodeReason.BAD_ADDRESS,
            f"{host!r} is a wildcard address; a code must name a reachable host",
        )
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            raise JoinCodeError(
                JoinCodeReason.BAD_ADDRESS,
                f"{host!r} has an IPv6 zone id, which another laptop cannot use",
            )
        return bytes([_KIND_V6]) + ip.packed
    return bytes([_KIND_V4]) + ip.packed


def encode(code: JoinCode) -> str:
    """The human form ``mf1-xxxxx-xxxxx-...`` of ``code``."""
    if not 1 <= code.port <= 65535:
        raise JoinCodeError(JoinCodeReason.BAD_ADDRESS, "port must be 1-65535")
    if len(code.token) != TOKEN_BYTES:
        raise JoinCodeError(JoinCodeReason.MALFORMED, "token must be 16 bytes")
    if not _PIN_RE.fullmatch(code.ca_pin):
        raise JoinCodeError(JoinCodeReason.MALFORMED, "ca_pin must be 32 hex")
    body = (
        bytes([CODE_VERSION])
        + _host_bytes(code.host)
        + struct.pack(">H", code.port)
        + code.token
        + bytes.fromhex(code.ca_pin)
    )
    payload = body + hashlib.sha256(body).digest()[:CHECKSUM_BYTES]
    text = base64.b32encode(payload).decode("ascii").rstrip("=").lower()
    groups = [text[i : i + _GROUP] for i in range(0, len(text), _GROUP)]
    return f"{_PREFIX}{CODE_VERSION}-" + "-".join(groups)


# ------------------------------------------------------------------------- decode


def _letters(text: str) -> str:
    if len(text) > MAX_CODE_CHARS:
        raise JoinCodeError(JoinCodeReason.MALFORMED, "this is not a join code")
    cleaned = re.sub(r"[\s\-]", "", text).upper()
    if not cleaned.startswith(_PREFIX.upper()) or len(cleaned) < 3:
        raise JoinCodeError(JoinCodeReason.MALFORMED, "a join code starts with 'mf1-'")
    version = cleaned[2]
    if version != str(CODE_VERSION):
        raise JoinCodeError(
            JoinCodeReason.UNSUPPORTED_VERSION,
            f"this code is format {version!r}; this MeshForge reads format "
            f"{CODE_VERSION} (upgrade the agent, or ask for a new code)",
        )
    letters = cleaned[3:].translate(_LOOKALIKES)
    if not _B32_RE.fullmatch(letters):
        raise JoinCodeError(
            JoinCodeReason.MALFORMED, "a join code holds only letters and digits 2-7"
        )
    return letters


def _parse_host(body: bytes, at: int) -> tuple[str, int]:
    kind = body[at]
    if kind == _KIND_V4:
        return str(ipaddress.IPv4Address(body[at + 1 : at + 5])), at + 5
    if kind == _KIND_V6:
        return str(ipaddress.IPv6Address(body[at + 1 : at + 17])), at + 17
    if kind == _KIND_NAME:
        size = body[at + 1]
        name = body[at + 2 : at + 2 + size].decode("ascii", "replace")
        return name, at + 2 + size
    raise JoinCodeError(JoinCodeReason.MALFORMED, "unknown address kind in the code")


def decode(text: str) -> JoinCode:
    """Read a code typed or pasted by a human. Every refusal is a
    :class:`JoinCodeError` whose message never contains the code."""
    letters = _letters(text)
    try:
        payload = base64.b32decode(letters + "=" * (-len(letters) % 8))
    except (binascii.Error, ValueError):
        raise JoinCodeError(
            JoinCodeReason.TRUNCATED,
            "the code has a wrong length: a character is missing or extra",
        ) from None
    minimum = 1 + 3 + 2 + TOKEN_BYTES + PIN_BYTES + CHECKSUM_BYTES  # 1-char name
    if len(payload) < minimum:
        raise JoinCodeError(
            JoinCodeReason.TRUNCATED, "the code is too short: copy all of it"
        )
    body, checksum = payload[:-CHECKSUM_BYTES], payload[-CHECKSUM_BYTES:]
    expected = hashlib.sha256(body).digest()[:CHECKSUM_BYTES]
    if not hmac.compare_digest(checksum, expected):
        raise JoinCodeError(
            JoinCodeReason.BAD_CHECKSUM,
            "this is not a valid code (a character is wrong); check it or ask for "
            "a new one",
        )
    if body[0] != CODE_VERSION:
        raise JoinCodeError(
            JoinCodeReason.UNSUPPORTED_VERSION, "the code's format byte is unknown"
        )
    try:
        host, at = _parse_host(body, 1)
    except (IndexError, ValueError):
        raise JoinCodeError(
            JoinCodeReason.MALFORMED, "the code's address field is damaged"
        ) from None
    expected_len = at + 2 + TOKEN_BYTES + PIN_BYTES
    if len(body) != expected_len:
        raise JoinCodeError(JoinCodeReason.MALFORMED, "the code's fields do not add up")
    (port,) = struct.unpack(">H", body[at : at + 2])
    token = body[at + 2 : at + 2 + TOKEN_BYTES]
    pin = body[at + 2 + TOKEN_BYTES :].hex()
    code = JoinCode(host=host, port=port, token=token, ca_pin=pin)
    try:
        _host_bytes(code.host)  # same rules as encode (a checksummed bad host)
    except JoinCodeError:
        raise JoinCodeError(
            JoinCodeReason.BAD_ADDRESS, "the code names an unusable address"
        ) from None
    if port == 0:
        raise JoinCodeError(JoinCodeReason.BAD_ADDRESS, "the code names port 0")
    return code


_TTL_RE: Final = re.compile(r"\s*(\d{1,6})\s*([smh]?)\s*", re.IGNORECASE)
_TTL_UNITS: Final = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_ttl(text: str) -> int:
    """``"15m"`` / ``"2h"`` / ``"90s"`` / ``"900"`` -> seconds (bounds are the
    caller's: an invite lives 1 min - 24 h). Raises :class:`ValueError`."""
    match = _TTL_RE.fullmatch(text)
    if match is None:
        raise ValueError(f"{text!r} is not a duration like 15m, 2h, 90s or 900")
    return int(match[1]) * _TTL_UNITS[match[2].lower()]
