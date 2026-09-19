"""Where am I running: native Windows, native Linux, or WSL?

S2 exists to prove ADR-0009 (the agent runs **natively** on Windows). A result
produced inside WSL2 would answer a different question - WSL2's NAT, not the
Windows firewall - so the tool refuses to run there unless the operator passes
``--allow-wsl``. Detection reuses ``_inventory.parse_linux.detect_wsl`` (the
same rules the inventory records) and stays a pure function of its inputs so
the unit tests run on any OS.

Also collected for the report: OS/build, Python, the local address and
interface used towards the peer, and - on Windows, without admin rights - the
network profile of each connection (``Get-NetConnectionProfile``); Private is
required by docs/LAB.md §6.
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import socket
import sys
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Any, Final

from _inventory import parse_linux as pl
from _inventory import parse_windows as pw
from _inventory import runner

type JSON = Any

#: Bound to a name so mypy does not treat the other branch as unreachable.
IS_WINDOWS: Final = sys.platform == "win32"

#: PowerShell, no admin rights needed. ``@(...)`` keeps a single profile a list.
PROFILE_SCRIPT: Final = (
    "$ErrorActionPreference='Stop'; "
    "@(Get-NetConnectionProfile | Select-Object InterfaceAlias,InterfaceIndex,"
    "Name,NetworkCategory,IPv4Connectivity) | ConvertTo-Json -Depth 3 -Compress"
)
POWERSHELL_TIMEOUT_S: Final = 20.0
WSL_REFUSAL: Final = (
    "refusing to run inside WSL: S2 must prove that a *native* process is "
    "reachable (ADR-0009). Run this from native Windows PowerShell (or native "
    "Linux). Pass --allow-wsl only to record a deliberately-WSL data point."
)


def read_text(path: Path) -> str:
    """File content, or ``""`` when it cannot be read (never raises)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def wsl_status(
    *,
    proc_version: str = "",
    osrelease: str = "",
    env: Mapping[str, str] | None = None,
    platform_name: str = sys.platform,
) -> dict[str, JSON]:
    """Pure WSL verdict for the given evidence.

    ``sys.platform`` alone is not enough: WSL2 *is* Linux. Windows and macOS are
    native by definition (WSL runs a Linux kernel), so their evidence is empty.
    """
    environ = os.environ if env is None else env
    distro = environ.get("WSL_DISTRO_NAME") or None
    interop = environ.get("WSL_INTEROP") or None
    if not platform_name.startswith("linux"):
        return {
            "platform": platform_name,
            "is_wsl": False,
            "wsl_version": None,
            "distro": None,
            "native": True,
            "evidence": {"platform": platform_name},
        }
    verdict = pl.detect_wsl(proc_version, osrelease, distro)
    is_wsl = bool(verdict["is_wsl"]) or bool(interop)
    return {
        "platform": platform_name,
        "is_wsl": is_wsl,
        "wsl_version": verdict["wsl_version"],
        "distro": distro,
        "native": not is_wsl,
        "evidence": {
            "proc_version": proc_version.strip()[:200],
            "osrelease": osrelease.strip()[:200],
            "WSL_DISTRO_NAME": distro,
            "WSL_INTEROP": bool(interop),
        },
    }


def wsl_status_here(root: Path = Path("/")) -> dict[str, JSON]:
    """:func:`wsl_status` for this machine (``root`` is a test seam)."""
    if not sys.platform.startswith("linux"):
        return wsl_status()
    return wsl_status(
        proc_version=read_text(root / "proc" / "version"),
        osrelease=read_text(root / "proc" / "sys" / "kernel" / "osrelease"),
    )


def refuse_if_wsl(status: Mapping[str, JSON], *, allow_wsl: bool) -> str | None:
    """Refusal text when running inside WSL without ``--allow-wsl``, else None."""
    if not status.get("is_wsl") or allow_wsl:
        return None
    distro = status.get("distro") or "unknown distro"
    version = status.get("wsl_version")
    return f"{WSL_REFUSAL} (detected WSL{version or ''} '{distro}')"


def os_build() -> dict[str, JSON]:
    """OS identity with the Windows build number (``platform.version()``)."""
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "sys_platform": sys.platform,
    }


@cache
def local_facts() -> dict[str, JSON]:
    """OS/build plus the WSL verdict, as a peer sees it (cached per process)."""
    return {**os_build(), "wsl": wsl_status_here()}


def parse_connection_profiles(text: str) -> list[dict[str, JSON]]:
    """``Get-NetConnectionProfile | ConvertTo-Json`` -> one row per connection."""
    data = json.loads(text or "[]")
    rows = data if isinstance(data, list) else [data]
    out: list[dict[str, JSON]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "interface": row.get("InterfaceAlias"),
                "interface_index": row.get("InterfaceIndex"),
                "network": row.get("Name"),
                "category": row.get("NetworkCategory"),
                "ipv4_connectivity": row.get("IPv4Connectivity"),
            }
        )
    return out


def firewall_profile() -> dict[str, JSON]:
    """Windows connection profiles (Private/Public/DomainAuthenticated).

    On other systems this is not applicable; the run records that instead of
    pretending to know. Never raises.
    """
    if not IS_WINDOWS:
        return {"source": "n/a", "supported": False, "profiles": []}
    result = runner.run(
        "powershell",
        [
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            pw.encoded_command(PROFILE_SCRIPT),
        ],
        timeout=POWERSHELL_TIMEOUT_S,
    )
    if not result.ok:
        return {
            "source": "powershell",
            "supported": True,
            "profiles": [],
            "error": result.describe_failure(),
        }
    try:
        profiles = parse_connection_profiles(result.stdout)
    except (ValueError, TypeError) as exc:
        return {
            "source": "powershell",
            "supported": True,
            "profiles": [],
            "error": f"Get-NetConnectionProfile output unparseable: {exc}",
        }
    return {"source": "powershell", "supported": True, "profiles": profiles}


def non_private_profiles(firewall: Mapping[str, JSON]) -> list[str]:
    """Interfaces whose profile is not Private (docs/LAB.md §6 wants Private)."""
    rows = firewall.get("profiles") or []
    return [
        str(row.get("interface"))
        for row in rows
        if isinstance(row, dict)
        and str(row.get("category", "")).lower()
        not in {"private", "domainauthenticated"}
    ]


def _family(host: str) -> socket.AddressFamily:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def local_address_towards(host: str, port: int) -> str | None:
    """The local IP the OS would route to ``host`` (no packet is sent)."""
    try:
        with socket.socket(_family(host), socket.SOCK_DGRAM) as sock:
            sock.connect((host, port))
            return str(sock.getsockname()[0])
    except OSError:
        return None


def interface_of(address: str | None) -> str | None:
    """Name of the interface holding ``address``; None when unknown.

    Uses psutil when it is installed (agent extra); a plain ``uv sync`` has no
    psutil, and an unknown interface name is not worth a hard dependency.
    """
    if not address:
        return None
    try:
        psutil: Any = importlib.import_module("psutil")
    except ImportError:
        return None
    try:
        for name, addrs in psutil.net_if_addrs().items():
            if any(getattr(a, "address", None) == address for a in addrs):
                return str(name)
    except (OSError, RuntimeError):
        return None
    return None


def endpoint_facts(host: str, port: int) -> dict[str, JSON]:
    """Local address + interface used towards ``host:port``, and the peer itself."""
    local = local_address_towards(host, port)
    return {
        "remote_address": host,
        "remote_port": port,
        "local_address": local,
        "local_interface": interface_of(local),
    }
