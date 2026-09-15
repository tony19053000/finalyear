"""Pure parsers for Linux sources (lscpu, /proc, /sys, ip -j, lspci -mm, mokutil)."""

from __future__ import annotations

import json
import re
import shlex
from typing import Final

from _inventory.model import JSON
from _inventory.sanitize import is_shareable_ipv4

_KV_RE: Final = re.compile(r"^\s*([^:]+?)\s*:\s*(.*?)\s*$")


def _kv(text: str) -> dict[str, str]:
    """``Key: value`` lines -> dict, first occurrence wins."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        match = _KV_RE.match(line)
        if match and match.group(1) not in out:
            out[match.group(1)] = match.group(2)
    return out


def _int(text: str | None) -> int | None:
    try:
        return int(str(text).strip())
    except ValueError:
        return None


def _float(text: str | None) -> float | None:
    try:
        return float(str(text).strip())
    except ValueError:
        return None


def parse_lscpu(text: str) -> dict[str, JSON]:
    """``LC_ALL=C lscpu`` -> model, physical cores, threads, max/min MHz."""
    kv = _kv(text)
    if "Model name" not in kv and "CPU(s)" not in kv:
        raise ValueError("unrecognised lscpu output")
    sockets = _int(kv.get("Socket(s)")) or 1
    cores_per_socket = _int(kv.get("Core(s) per socket"))
    return {
        "model": kv.get("Model name"),
        "vendor": kv.get("Vendor ID"),
        "architecture": kv.get("Architecture"),
        "sockets": sockets,
        "physical_cores": cores_per_socket * sockets if cores_per_socket else None,
        "logical_threads": _int(kv.get("CPU(s)")),
        "max_mhz": _float(kv.get("CPU max MHz")),
        "min_mhz": _float(kv.get("CPU min MHz")),
    }


def parse_cpuinfo(text: str) -> dict[str, JSON]:
    """/proc/cpuinfo fallback: model, physical cores (unique core ids), threads."""
    model: str | None = None
    threads = 0
    cores: set[tuple[str, str]] = set()
    phys = core = ""
    for line in [*text.splitlines(), ""]:
        match = _KV_RE.match(line)
        if not line.strip():
            if phys or core:
                cores.add((phys, core))
            phys = core = ""
            continue
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if key == "processor":
            threads += 1
        elif key == "model name" and model is None:
            model = value
        elif key == "physical id":
            phys = value
        elif key == "core id":
            core = value
    if threads == 0:
        raise ValueError("no processors in /proc/cpuinfo")
    return {
        "model": model,
        "physical_cores": len(cores) or None,
        "logical_threads": threads,
    }


def parse_khz_file(text: str) -> float | None:
    """A cpufreq ``*_frequency`` file (kHz) -> MHz."""
    khz = _int(text)
    return khz / 1000 if khz and khz > 0 else None


def parse_meminfo(text: str) -> dict[str, JSON]:
    """/proc/meminfo -> total/available bytes."""
    kv = _kv(text)
    total = kv.get("MemTotal", "").split()
    if not total:
        raise ValueError("MemTotal missing in /proc/meminfo")
    avail = kv.get("MemAvailable", "").split()
    return {
        "total_bytes": int(total[0]) * 1024,
        "available_bytes": int(avail[0]) * 1024 if avail else None,
    }


def parse_os_release(text: str) -> dict[str, str]:
    """/etc/os-release (shell-quoted KEY=value) -> dict."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        try:
            parts = shlex.split(value)
        except ValueError:
            parts = [value.strip("\"'")]
        out[key.strip()] = parts[0] if parts else ""
    return out


def detect_wsl(
    proc_version: str, osrelease: str, env_distro: str | None
) -> dict[str, JSON]:
    """WSL detection from /proc/version, /proc/sys/kernel/osrelease, $WSL_DISTRO_NAME."""
    text = f"{proc_version} {osrelease}".lower()
    is_wsl = "microsoft" in text or "wsl" in text or bool(env_distro)
    version: int | None = None
    if is_wsl:
        version = 2 if ("wsl2" in text or "microsoft-standard" in text) else 1
    return {"is_wsl": is_wsl, "wsl_version": version, "distro": env_distro or None}


def parse_ip_json(text: str) -> dict[str, list[dict[str, JSON]]]:
    """``ip -j -4 addr`` -> {ifname: [{"address", "prefixlen"}]} (shareable IPs only)."""
    data = json.loads(text or "[]")
    if not isinstance(data, list):
        raise TypeError("ip -j output is not a list")
    out: dict[str, list[dict[str, JSON]]] = {}
    for iface in data:
        if not isinstance(iface, dict) or "ifname" not in iface:
            continue
        addrs = out.setdefault(str(iface["ifname"]), [])
        for info in iface.get("addr_info") or []:
            if info.get("family") != "inet":
                continue
            local = str(info.get("local", ""))
            if is_shareable_ipv4(local):
                addrs.append({"address": local, "prefixlen": info.get("prefixlen")})
    return out


def classify_sys_net(
    name: str, *, has_device: bool, has_wireless: bool, type_code: str | None
) -> str:
    """Kind of a /sys/class/net interface: wifi | ethernet | loopback | virtual | other."""
    if name == "lo" or (type_code or "").strip() == "772":
        return "loopback"
    if has_wireless:
        return "wifi"
    if not has_device:
        return "virtual"
    if (type_code or "").strip() == "1":
        return "ethernet"
    return "other"


def parse_sys_speed(text: str | None) -> int | None:
    """/sys/class/net/<if>/speed -> Mbit/s; None when unknown/down (-1, EINVAL)."""
    value = _int(text)
    return value if value is not None and value > 0 else None


_IW_BITRATE_RE: Final = re.compile(r"tx bitrate:\s*([0-9.]+)\s*MBit/s", re.IGNORECASE)


def parse_iw_link(text: str) -> float | None:
    """``iw dev <if> link`` -> Wi-Fi TX bitrate in Mbit/s (the SSID is ignored)."""
    match = _IW_BITRATE_RE.search(text)
    return float(match.group(1)) if match else None


def parse_lspci_mm(text: str) -> list[dict[str, JSON]]:
    """``lspci -mm`` -> display controllers [{"name", "vendor", "pci_slot"}]."""
    gpus: list[dict[str, JSON]] = []
    for line in text.splitlines():
        try:
            fields = shlex.split(line)
        except ValueError:
            continue
        if len(fields) < 4:
            continue
        slot, klass, vendor, device = fields[0], fields[1], fields[2], fields[3]
        lowered = klass.lower()
        if not ("vga" in lowered or "3d" in lowered or "display" in lowered):
            continue
        gpus.append(
            {
                "name": device,
                "vendor": normalise_vendor(vendor),
                "pci_slot": slot,
            }
        )
    return gpus


def normalise_vendor(text: str | None) -> str | None:
    """Map vendor strings (``NVIDIA Corporation``, ``Advanced Micro Devices``) to a short name."""
    if not text:
        return None
    lowered = text.lower()
    for key, name in (
        ("nvidia", "NVIDIA"),
        ("advanced micro devices", "AMD"),
        ("amd", "AMD"),
        ("ati ", "AMD"),
        ("intel", "Intel"),
        ("microsoft", "Microsoft"),
        ("qualcomm", "Qualcomm"),
    ):
        if key in lowered:
            return name
    return text.strip()


def parse_mokutil_sb_state(text: str) -> str:
    """``mokutil --sb-state`` -> enabled | disabled | unsupported | unknown."""
    lowered = text.lower()
    if "secureboot enabled" in lowered:
        return "enabled"
    if "secureboot disabled" in lowered:
        return "disabled"
    if "not supported" in lowered or "doesn't support" in lowered:
        return "unsupported"
    return "unknown"


def parse_secureboot_efivar(data: bytes) -> str:
    """Raw ``SecureBoot-<guid>`` efivar (4 attribute bytes + 1 value byte)."""
    if len(data) < 5:
        return "unknown"
    return "enabled" if data[4] == 1 else "disabled"


def parse_power_supplies(entries: dict[str, dict[str, str]]) -> dict[str, JSON]:
    """/sys/class/power_supply/* (``{name: {"type","online","status","capacity"}}``)."""
    on_ac: bool | None = None
    batteries: list[dict[str, JSON]] = []
    for name in sorted(entries):
        info = entries[name]
        kind = info.get("type", "").strip().lower()
        if kind in {"mains", "usb", "usb_c", "usb_pd"} and "online" in info:
            online = info["online"].strip() == "1"
            on_ac = online or bool(on_ac)
        elif kind == "battery":
            batteries.append(
                {
                    "percent": _int(info.get("capacity")),
                    "status": info.get("status", "").strip() or None,
                }
            )
    if on_ac is None and batteries:
        statuses = {str(b["status"]).lower() for b in batteries}
        if "discharging" in statuses:
            on_ac = False
        elif statuses & {"charging", "full", "not charging"}:
            on_ac = True
    return {"on_ac": on_ac, "has_battery": bool(batteries), "batteries": batteries}
