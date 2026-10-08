"""Bind-address rules shared by every MeshForge listener (controller gRPC/REST, later
the agent's blob server).

The system is remote code execution by design (CLAUDE.md §9), so a listener binds
one LAN interface unless the operator explicitly asks for more:

* an **IP literal** is required (no hostnames - they resolve differently per OS);
* the **wildcard** (``0.0.0.0`` / ``::``) is refused unless ``allow_any``;
* a **public** (non-private, non-loopback, non-link-local) address is refused unless
  ``allow_any``; multicast is never acceptable.

Same semantics as the V0.2 spike harness (``lab/spikes/_harness/protocol.py``),
reimplemented here because ``src/`` never imports ``lab/``.
"""

from __future__ import annotations

import ipaddress
from typing import Final

from meshforge.common.errors import ConfigError

__all__ = [
    "BindAddress",
    "check_bind_address",
    "is_lan_address",
    "is_loopback_host",
    "parse_bind",
]

_PORT_MAX: Final = 65535


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


def is_loopback_host(text: str) -> bool:
    """A loopback IP literal (``127.0.0.0/8``, ``::1``, IPv4-mapped) or the name
    ``localhost`` - the only hosts ``[security] mode = "insecure-dev"`` accepts."""
    host = text.strip().removeprefix("[").removesuffix("]").split("%", 1)[0]
    if host.lower() == "localhost":
        return True
    try:
        return _ip(host).is_loopback
    except ValueError:
        return False


def check_bind_address(text: str, *, allow_any: bool) -> str:
    """Validate a bind host; returns the normalised IP string.

    Raises :class:`~meshforge.common.errors.ConfigError` (exit ``CONFIG_ERROR``)
    with an operator-readable remedy.
    """
    try:
        addr = _ip(text.strip())
    except ValueError:
        raise ConfigError(
            f"bind address needs an IP literal, got {text!r}",
            details={"bind": text},
        ) from None
    if addr.is_unspecified and not allow_any:
        raise ConfigError(
            f"refusing to bind the wildcard address {text}: give this machine's LAN IP "
            "(e.g. 192.168.1.20), or set allow_any_bind if you really mean every "
            "interface",
            details={"bind": text},
        )
    if not addr.is_unspecified and not is_lan_address(str(addr)) and not allow_any:
        raise ConfigError(
            f"refusing to bind non-private address {text} (trusted LAN only); set "
            "allow_any_bind to override",
            details={"bind": text},
        )
    return str(addr)


class BindAddress(tuple[str, int]):
    """``(host, port)`` parsed from ``"host:port"`` / ``"[v6]:port"``."""

    __slots__ = ()

    @property
    def host(self) -> str:
        return self[0]

    @property
    def port(self) -> int:
        return self[1]

    def grpc_target(self) -> str:
        """``host:port`` in the form ``grpc.aio.server.add_insecure_port`` accepts."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def parse_bind(text: str, *, allow_any: bool = False) -> BindAddress:
    """Parse and validate ``"host:port"``; port ``0`` means "any free port".

    IPv6 must be bracketed (``[::1]:47100``). Raises :class:`ConfigError`.
    """
    raw = text.strip()
    if raw.startswith("["):
        host_part, sep, port_part = raw[1:].partition("]")
        if not sep or not port_part.startswith(":"):
            raise ConfigError(
                f"bind address must look like [ipv6]:port, got {text!r}",
                details={"bind": text},
            )
        port_text = port_part[1:]
    else:
        host_part, sep, port_text = raw.rpartition(":")
        if not sep or ":" in host_part:
            raise ConfigError(
                f"bind address must look like ip:port (bracket IPv6), got {text!r}",
                details={"bind": text},
            )
    try:
        port = int(port_text)
    except ValueError:
        raise ConfigError(
            f"bind address has a non-numeric port: {text!r}", details={"bind": text}
        ) from None
    if not 0 <= port <= _PORT_MAX:
        raise ConfigError(
            f"bind port must be 0..{_PORT_MAX}, got {port}", details={"bind": text}
        )
    host = check_bind_address(host_part, allow_any=allow_any)
    return BindAddress((host, port))
