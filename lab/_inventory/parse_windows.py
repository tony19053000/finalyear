"""Pure parsers for Windows sources.

One PowerShell call (:data:`CIM_SCRIPT`) collects every CIM/NetAdapter class as a
single JSON object ``{key: {"ok": bool, "data"|"error": ...}}``; PowerShell starts
slowly, so one process instead of seven. Only non-identifying properties are
selected (no serial numbers, user names, registered owner, SSIDs).
"""

from __future__ import annotations

import base64
import json
import re
from typing import Final

from _inventory.model import JSON
from _inventory.parse_linux import normalise_vendor
from _inventory.sanitize import is_shareable_ipv4

#: key -> PowerShell expression. Each runs in its own try/catch.
CIM_QUERIES: Final[dict[str, str]] = {
    "processor": "Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,"
    "NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,CurrentClockSpeed",
    "computer_system": "Get-CimInstance Win32_ComputerSystem | Select-Object "
    "Manufacturer,Model,TotalPhysicalMemory,NumberOfProcessors,PCSystemType",
    "operating_system": "Get-CimInstance Win32_OperatingSystem | Select-Object "
    "Caption,Version,BuildNumber,OSArchitecture,TotalVisibleMemorySize,FreePhysicalMemory",
    "video_controllers": "Get-CimInstance Win32_VideoController | Select-Object "
    "Name,AdapterCompatibility,AdapterRAM,DriverVersion,Status",
    "net_adapters": "Get-NetAdapter | Select-Object Name,InterfaceDescription,"
    "InterfaceIndex,MacAddress,Status,LinkSpeed,ReceiveLinkSpeed,TransmitLinkSpeed,"
    "MediaType,PhysicalMediaType,NdisPhysicalMedium,Virtual,HardwareInterface",
    "net_ip": "Get-NetIPAddress -AddressFamily IPv4 | Select-Object "
    "InterfaceIndex,InterfaceAlias,IPAddress,PrefixLength",
    "battery": "Get-CimInstance Win32_Battery | Select-Object "
    "BatteryStatus,EstimatedChargeRemaining",
    "secure_boot": "Confirm-SecureBootUEFI",
}


def build_cim_script() -> str:
    """The PowerShell script run by the Windows probes (outputs one JSON object)."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$ProgressPreference = 'SilentlyContinue'",
        # Windows PowerShell 5.1 serialises arrays as {"value": [...], "Count": n}
        # because of this ETS type data; PowerShell 7 has none (hence try/catch).
        "try { Remove-TypeData System.Array -ErrorAction Stop } catch { }",
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "$r = [ordered]@{}",
    ]
    for key, expr in CIM_QUERIES.items():
        lines.append(
            f"try {{ $r['{key}'] = @{{ ok = $true; data = @({expr}) }} }} "
            f"catch {{ $r['{key}'] = @{{ ok = $false; error = $_.Exception.Message }} }}"
        )
    lines.append("$r | ConvertTo-Json -Depth 5 -Compress")
    return "\n".join(lines)


CIM_SCRIPT: Final = build_cim_script()


def encoded_command(script: str) -> str:
    """Base64 UTF-16LE for ``powershell -EncodedCommand`` (immune to Windows quoting)."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def parse_cim_bundle(text: str) -> dict[str, dict[str, JSON]]:
    """Output of :data:`CIM_SCRIPT` -> ``{key: {"ok": bool, "data": list | "error": str}}``."""
    start = text.find("{")
    if start < 0:
        raise ValueError(f"no JSON in PowerShell output: {text.strip()[:300]}")
    raw = json.loads(text[start:])
    if not isinstance(raw, dict):
        raise TypeError("PowerShell output is not a JSON object")
    out: dict[str, dict[str, JSON]] = {}
    for key, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("ok"):
            data = entry.get("data")
            if data is None:
                items: list[JSON] = []
            elif isinstance(data, list):
                items = data
            elif isinstance(data, dict) and isinstance(data.get("value"), list):
                items = data["value"]  # PS 5.1 array wrapper, if type data survived
            else:  # ConvertTo-Json unwraps one-element arrays
                items = [data]
            out[str(key)] = {"ok": True, "data": items}
        else:
            out[str(key)] = {
                "ok": False,
                "error": str(entry.get("error") or "unknown error"),
            }
    return out


def _int(value: JSON) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_processors(items: list[JSON]) -> dict[str, JSON]:
    """Win32_Processor rows (one per socket) -> cpu section value."""
    if not items:
        raise ValueError("Win32_Processor returned no rows")
    first = items[0]
    cores = [_int(i.get("NumberOfCores")) for i in items]
    threads = [_int(i.get("NumberOfLogicalProcessors")) for i in items]
    return {
        "model": str(first.get("Name") or "").strip() or None,
        "vendor": first.get("Manufacturer"),
        "sockets": len(items),
        "physical_cores": sum(c for c in cores if c) or None,
        "logical_threads": sum(t for t in threads if t) or None,
        # Win32_Processor.MaxClockSpeed is the rated (base) clock; turbo is not exposed.
        "base_mhz": _int(first.get("MaxClockSpeed")),
        "max_mhz": None,
        "current_mhz": _int(first.get("CurrentClockSpeed")),
    }


def parse_os(items: list[JSON]) -> dict[str, JSON]:
    """Win32_OperatingSystem -> name/version/build."""
    if not items:
        raise ValueError("Win32_OperatingSystem returned no rows")
    row = items[0]
    return {
        "name": row.get("Caption"),
        "version": row.get("Version"),
        "build": str(row.get("BuildNumber"))
        if row.get("BuildNumber") is not None
        else None,
        "architecture": row.get("OSArchitecture"),
    }


def parse_memory(os_items: list[JSON], cs_items: list[JSON]) -> dict[str, JSON]:
    """Total/available RAM from Win32_ComputerSystem (bytes) / Win32_OperatingSystem (KiB)."""
    total = _int(cs_items[0].get("TotalPhysicalMemory")) if cs_items else None
    avail_kib = _int(os_items[0].get("FreePhysicalMemory")) if os_items else None
    if total is None and os_items:
        kib = _int(os_items[0].get("TotalVisibleMemorySize"))
        total = kib * 1024 if kib else None
    if total is None:
        raise ValueError("no memory size in CIM data")
    return {
        "total_bytes": total,
        "available_bytes": avail_kib * 1024 if avail_kib else None,
    }


_UINT32_MAX: Final = 2**32 - 1


def parse_video_controllers(items: list[JSON]) -> list[dict[str, JSON]]:
    """Win32_VideoController -> GPUs. AdapterRAM is a uint32 (caps near 4 GiB)."""
    gpus: list[dict[str, JSON]] = []
    for row in items:
        ram = _int(row.get("AdapterRAM"))
        vram_mib = ram // (1024 * 1024) if ram and ram > 0 else None
        capped = ram is not None and ram >= _UINT32_MAX - 1024 * 1024
        gpus.append(
            {
                "name": row.get("Name"),
                "vendor": normalise_vendor(
                    row.get("AdapterCompatibility") or row.get("Name")
                ),
                "vram_mib": vram_mib,
                "vram_note": "AdapterRAM caps at 4 GiB; real VRAM may be larger"
                if capped
                else None,
                "driver_version": row.get("DriverVersion"),
                "status": row.get("Status"),
            }
        )
    return gpus


#: NdisPhysicalMedium values (ntddndis.h): 9 = Native 802.11, 14 = 802.3, 0 = unspecified.
_NDIS_KIND: Final = {9: "wifi", 14: "ethernet", 10: "bluetooth", 8: "wwan"}
_LINK_SPEED_RE: Final = re.compile(r"([0-9.]+)\s*([KMG])bps", re.IGNORECASE)


def parse_link_speed_mbit(link_speed: JSON, receive_bps: JSON) -> float | None:
    """``ReceiveLinkSpeed`` (bit/s) or ``LinkSpeed`` text like ``866.7 Mbps`` -> Mbit/s."""
    bps = _int(receive_bps)
    if bps and bps > 0:
        return round(bps / 1_000_000, 1)
    match = _LINK_SPEED_RE.search(str(link_speed or ""))
    if not match:
        return None
    factor = {"k": 0.001, "m": 1.0, "g": 1000.0}[match.group(2).lower()]
    return round(float(match.group(1)) * factor, 1)


def classify_adapter(row: dict[str, JSON]) -> str:
    """wifi | ethernet | bluetooth | wwan | virtual | other for a Get-NetAdapter row."""
    if row.get("Virtual") is True or row.get("HardwareInterface") is False:
        return "virtual"
    kind = _NDIS_KIND.get(_int(row.get("NdisPhysicalMedium")) or -1)
    if kind:
        return kind
    text = f"{row.get('PhysicalMediaType', '')} {row.get('MediaType', '')}".lower()
    desc = str(row.get("InterfaceDescription") or "").lower()
    if "802.11" in text or "wireless" in desc or "wi-fi" in desc:
        return "wifi"
    if "802.3" in text or "ethernet" in desc:
        return "ethernet"
    return "other"


def parse_net_adapters(adapters: list[JSON], ips: list[JSON]) -> list[dict[str, JSON]]:
    """Get-NetAdapter + Get-NetIPAddress -> adapter list (shareable IPv4 only)."""
    by_index: dict[int, list[str]] = {}
    for row in ips:
        idx = _int(row.get("InterfaceIndex"))
        addr = str(row.get("IPAddress") or "")
        if idx is not None and is_shareable_ipv4(addr):
            by_index.setdefault(idx, []).append(f"{addr}/{row.get('PrefixLength')}")
    out: list[dict[str, JSON]] = []
    for row in adapters:
        idx = _int(row.get("InterfaceIndex"))
        mac = str(row.get("MacAddress") or "").replace("-", ":").lower() or None
        status = str(row.get("Status") or "")
        out.append(
            {
                "name": row.get("Name"),
                "description": row.get("InterfaceDescription"),
                "kind": classify_adapter(row),
                "mac": mac,
                "ipv4": sorted(by_index.get(idx, [])) if idx is not None else [],
                "link_speed_mbit": parse_link_speed_mbit(
                    row.get("LinkSpeed"), row.get("ReceiveLinkSpeed")
                )
                if status.lower() == "up"
                else None,
                "up": status.lower() == "up",
                "status": status or None,
            }
        )
    return sorted(out, key=lambda a: str(a["name"]))


def parse_battery(items: list[JSON]) -> dict[str, JSON]:
    """Win32_Battery -> power section value (BatteryStatus 2 = on AC)."""
    if not items:
        return {"on_ac": None, "has_battery": False, "batteries": []}
    batteries: list[dict[str, JSON]] = []
    on_ac: bool | None = None
    for row in items:
        status = _int(row.get("BatteryStatus"))
        # 1 = discharging; 2 = on AC; 3-9 = charging/charged states (all on AC).
        if status == 1:
            on_ac = False if on_ac is None else on_ac
        elif status is not None and status >= 2:
            on_ac = True
        batteries.append(
            {"percent": _int(row.get("EstimatedChargeRemaining")), "status": status}
        )
    return {"on_ac": on_ac, "has_battery": True, "batteries": batteries}


def parse_secure_boot_result(entry: dict[str, JSON] | None) -> dict[str, JSON]:
    """``Confirm-SecureBootUEFI`` result -> state (non-admin usually 'unknown')."""
    if entry is None:
        return {"state": "unknown", "detail": "not queried"}
    if entry.get("ok"):
        data = entry.get("data") or []
        value = data[0] if data else None
        if value is True:
            return {"state": "enabled", "detail": None}
        if value is False:
            return {"state": "disabled", "detail": None}
        return {"state": "unknown", "detail": f"unexpected value {value!r}"}
    error = str(entry.get("error") or "")
    if "not supported" in error.lower():
        return {"state": "unsupported", "detail": error}
    return {"state": "unknown", "detail": error or "unknown error"}


def parse_wsl_list(text: str) -> list[dict[str, JSON]]:
    """``wsl.exe -l -v`` (already decoded from UTF-16) -> distros with state/version."""
    distros: list[dict[str, JSON]] = []
    for line in text.splitlines():
        line = line.replace("\x00", "").strip()
        if not line or line.upper().startswith("NAME"):
            continue
        default = line.startswith("*")
        parts = line.lstrip("*").split()
        if len(parts) < 3:
            continue
        distros.append(
            {
                "name": " ".join(parts[:-2]),
                "state": parts[-2],
                "version": _int(parts[-1]),
                "default": default,
            }
        )
    return distros
