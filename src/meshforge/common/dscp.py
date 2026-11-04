"""Bulk data is background traffic: DSCP CS1 (V1.0 G9-A, D430).

Job files (tree chunks, bundles, outputs) move over the HTTP blob planes - the
controller's REST/blob port and the agents' peer blob servers. On a shared
Wi-Fi a 5 GB transfer saturates the link and the small, latency-sensitive
traffic (gRPC heartbeats on the session port, DNS, a video call) queues behind
it. Marking the bulk sockets **CS1** (DSCP 8, ``IP_TOS = 0x20``, RFC 8622
"lower effort") lets Wi-Fi WMM put them in the background access category and
lets any QoS-aware router send everything else first. It is best effort: a
network that ignores DSCP behaves exactly as before.

* Listening sockets (:func:`mark_listener`) are marked **before** they accept;
  on Linux an accepted socket inherits the listener's ``IP_TOS`` (pinned by a
  real-socket test), so every response a server sends is CS1.
* Client sockets (:func:`bulk_socket_options`) get the option through httpx
  ``socket_options`` (set right after connect). Only ``IPPROTO_IP/IP_TOS`` is
  used there - it is accepted by IPv4 and IPv6 sockets alike on Linux (an IPv6
  connection uses the traffic class of a listener-side ``IPV6_TCLASS``, the
  client's v6 traffic class is left alone).
* **Never on the gRPC session port**: heartbeats are what CS1 protects.
* **Linux only.** Windows ignores ``IP_TOS`` from applications (DSCP needs a
  Group Policy QoS rule) and macOS is not a target: elsewhere nothing is set and
  nothing is logged.
* A ``setsockopt`` that fails logs **one** warning per place
  (``common.dscp.unsupported``) and the socket is used unmarked. The "warned
  once" set is the only module state; it holds at most one entry per call site.
"""

from __future__ import annotations

import socket
import sys
from typing import Final, Literal

from meshforge.common.logging import get_logger

__all__ = [
    "CS1_TOS",
    "DscpBulk",
    "bulk_socket_options",
    "dscp_supported",
    "mark_listener",
]

log = get_logger(__name__)

type DscpBulk = Literal["cs1", "off"]
"""``[controller.http] / [agent.peer] / [agent.fetch] dscp_bulk``."""

CS1_TOS: Final = 0x20
"""DSCP CS1 (8) in the upper six bits of the IPv4 TOS / IPv6 traffic class."""

type SocketOption = tuple[int, int, int]

_warned: set[str] = set()


def dscp_supported() -> bool:
    """True where an application's ``IP_TOS`` reaches the wire (Linux)."""
    return sys.platform.startswith("linux")


def _warn_once(where: str, exc: OSError) -> None:
    if where in _warned:
        return
    _warned.add(where)
    log.warning(
        "common.dscp.unsupported",
        where=where,
        error=exc.strerror or type(exc).__name__,
        hint="bulk data is sent unmarked; set dscp_bulk = 'off' to silence this",
    )


def mark_listener(sock: socket.socket, mode: DscpBulk, *, where: str) -> bool:
    """Mark a listening TCP socket CS1 (``mode = "cs1"``, Linux); True = set.

    Call before the socket accepts. An IPv6 socket gets ``IPV6_TCLASS`` and,
    for v4-mapped peers of a dual-stack socket, ``IP_TOS`` too."""
    if mode != "cs1" or not dscp_supported():
        return False
    try:
        if sock.family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, CS1_TOS)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, CS1_TOS)
    except OSError as exc:
        _warn_once(where, exc)
        return False
    return True


def bulk_socket_options(mode: DscpBulk, *, where: str) -> tuple[SocketOption, ...]:
    """httpx/httpcore ``socket_options`` marking client sockets CS1, or ``()``.

    The option is tried once on throwaway IPv4 and IPv6 sockets first: an
    option httpcore cannot set would fail every connect, so anything the
    kernel refuses is left out (one warning) instead."""
    if mode != "cs1" or not dscp_supported():
        return ()
    option: SocketOption = (socket.IPPROTO_IP, socket.IP_TOS, CS1_TOS)
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            probe = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue  # no IPv6 on this host: no IPv6 connections either
        try:
            with probe:
                probe.setsockopt(*option)
        except OSError as exc:
            _warn_once(where, exc)
            return ()
    return (option,)
