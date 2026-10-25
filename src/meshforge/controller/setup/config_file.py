"""The packaged controller's first settings file (V0.17 G2).

``meshforge-controller setup init-config`` (run by the package's postinst, as
root, once) writes ``/etc/meshforge/controller.toml`` when it does not exist:
the three listeners on this machine's LAN address (the source address of the
default route), managed database mode. The LAN address matters twice: laptops
reach the controller there, and it goes into the controller's TLS certificate
when the fabric is created - so it is fixed before the setup page runs.

No LAN address (no network yet, or only a public address, which the controller
refuses to bind without ``allow_any_bind``): loopback, and the setup page warns
that other laptops cannot reach it until the file is edited. An existing file is
never overwritten.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from typing import Final

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import MeshForgeError
from meshforge.common.netaddr import parse_bind
from meshforge.controller.settings import (
    DEFAULT_ENROLL_PORT,
    DEFAULT_GRPC_PORT,
    DEFAULT_HTTP_PORT,
)

__all__ = ["LOOPBACK", "default_config_text", "detect_lan_ipv4", "write_default_config"]

LOOPBACK: Final = "127.0.0.1"
_PROBE: Final = ("192.0.2.1", 9)
"""TEST-NET-1: connecting a UDP socket picks the route's source address and
sends nothing."""


def detect_lan_ipv4() -> str | None:
    """The source address of the default route, if it is a LAN address the
    controller may bind (private, not loopback/link-local); else None."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(_PROBE)
            address = str(sock.getsockname()[0])
    except OSError:
        return None
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return None
    try:
        parse_bind(f"{address}:{DEFAULT_GRPC_PORT}", allow_any=False)
    except MeshForgeError:
        return None  # a public address: never bound by default
    return address


def default_config_text(address: str) -> str:
    lan = address != LOOPBACK
    note = (
        "# Laptops reach this controller at the address below; it is also written\n"
        "# into the controller's TLS certificate when the fabric is created. If it\n"
        "# changes, edit the three lines and restart the controller (before the\n"
        "# fabric exists; afterwards also add the new address to [controller.tls]\n"
        "# extra_sans so the certificate is renewed with it).\n"
        if lan
        else "# No LAN address was found when the package was installed, so the\n"
        "# controller listens on this computer only. Put this computer's LAN address\n"
        "# in the three lines below, then: sudo systemctl restart meshforge-controller\n"
    )
    return (
        "# MeshForge controller settings - written once by the meshforge-controller\n"
        "# package (V0.17). Edit, then: sudo systemctl restart meshforge-controller\n"
        f"{note}\n"
        "[controller]\n"
        f'grpc_bind = "{address}:{DEFAULT_GRPC_PORT}"\n'
        f'http_bind = "{address}:{DEFAULT_HTTP_PORT}"\n'
        "\n"
        "[controller.enroll]\n"
        f'bind = "{address}:{DEFAULT_ENROLL_PORT}"\n'
        "\n"
        "[controller.db]\n"
        "# The controller's own PostgreSQL 16 (data in /var/lib/meshforge/db).\n"
        'mode = "managed"\n'
    )


def write_default_config(path: Path, address: str | None = None) -> tuple[bool, str]:
    """Write the file unless it exists: ``(written, address used)``. Mode 0644
    (no secrets in it; the service user must read it)."""
    chosen = address or detect_lan_ipv4() or LOOPBACK
    if path.exists():
        return False, chosen
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    try:
        atomic_write_bytes(
            path,
            default_config_text(chosen).encode("utf-8"),
            mode=0o644,
            exclusive=True,
        )
    except FileExistsError:
        return False, chosen
    if os.name == "posix":
        os.chmod(path, 0o644)
    return True, chosen
