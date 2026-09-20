"""Shared wire-level helpers for every spike: LAN-only address rules and the
length-prefixed JSON framing that S1 introduced and S2/S3 reuse.

Framing: one message is ``[4-byte big-endian length][UTF-8 JSON object]`` of at
most :data:`MAX_HEADER_BYTES`. Address rules: spike servers bind only to one
private LAN interface and talk only to LAN peers (docs/LAB.md §6). These are the
pieces V0.3/V0.4 lift into ``src/``; spikes import them from here, never from
another spike's package.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import struct
from typing import Any, Final

MAX_HEADER_BYTES: Final = 4096
_LEN: Final = struct.Struct("!I")

type Message = dict[str, Any]


class ProtocolError(ValueError):
    """The peer sent something that is not valid protocol (bad framing or header)."""


def recv_exact(sock: socket.socket, size: int) -> bytes:
    """Read exactly ``size`` bytes; EOF before that raises ``ConnectionResetError``."""
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionResetError("peer closed the connection")
        buf += chunk
    return bytes(buf)


def send_msg(sock: socket.socket, msg: Message) -> None:
    body = json.dumps(msg, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(body) > MAX_HEADER_BYTES:
        raise ProtocolError("message too large")
    sock.sendall(_LEN.pack(len(body)) + body)


#: Size of the big-endian length prefix in front of every message.
LENGTH_BYTES: Final = _LEN.size


def body_length(prefix: bytes) -> int:
    """Body size from a 4-byte length prefix; ``ProtocolError`` if out of range."""
    (size,) = _LEN.unpack(prefix)
    if not 0 < size <= MAX_HEADER_BYTES:
        raise ProtocolError(f"bad header length {size}")
    return int(size)


def decode_body(body: bytes) -> Message:
    """A message body (UTF-8 JSON object); ``ProtocolError`` otherwise."""
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"header is not JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ProtocolError("header is not a JSON object")
    return msg


def recv_msg(sock: socket.socket) -> Message:
    size = body_length(recv_exact(sock, LENGTH_BYTES))
    return decode_body(recv_exact(sock, size))


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    addr = ipaddress.ip_address(text)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def is_lan_address(text: str) -> bool:
    """Private (RFC 1918 / ULA), loopback or link-local; never public/multicast."""
    try:
        addr = _ip(text)
    except ValueError:
        return False
    if addr.is_multicast or addr.is_unspecified:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


def is_allowed_peer(text: str) -> bool:
    """Clients the server talks to: LAN addresses only (docs/LAB.md §6)."""
    return is_lan_address(text)


def check_bind_address(text: str, *, allow_any: bool) -> str:
    """Validate a ``--bind`` value; returns the normalised address.

    An IP literal is required. The wildcard (``0.0.0.0`` / ``::``) and public
    addresses are refused unless ``allow_any`` - a spike server listens only on
    the one lab interface being measured.
    """
    try:
        addr = _ip(text.strip())
    except ValueError:
        raise ValueError(f"--bind needs an IP address, got {text!r}") from None
    if addr.is_unspecified and not allow_any:
        raise ValueError(
            f"refusing to bind the wildcard address {text}: pass this laptop's lab "
            "IP (e.g. 192.168.1.20), or --allow-any if you really mean every interface"
        )
    if not addr.is_unspecified and not is_lan_address(str(addr)) and not allow_any:
        raise ValueError(
            f"refusing to bind non-private address {text} (trusted LAN only, "
            "docs/LAB.md §6); pass --allow-any to override"
        )
    return str(addr)


def family_of(host: str) -> socket.AddressFamily:
    try:
        return socket.AF_INET6 if _ip(host).version == 6 else socket.AF_INET
    except ValueError:
        return socket.AF_INET
