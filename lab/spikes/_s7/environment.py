"""Environment honesty: what the machine's power/thermal state was, before *and*
after the run.

A GFLOPS number is only comparable if the conditions are known, so every S7
result carries two snapshots (``before`` / ``after``) of:

* **power** - AC or battery (from the inventory probe, so the two tools agree),
* **CPU governor / power plan** - ``scaling_governor`` per CPU on Linux, the
  active power scheme on Windows,
* **thermals** - Linux thermal zones, and the GPU temperature / SM clock /
  active throttle reasons from ``nvidia-smi`` when it is present.

Scores measured on battery are *not* comparable with scores measured on AC
(laptops cut clocks hard), so the run warns loudly and sets ``on_battery`` in
the JSON instead of silently producing a pretty number.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final

from _inventory import probes, runner
from _inventory.model import JSON

IS_LINUX: Final = sys.platform.startswith("linux")
IS_WINDOWS: Final = sys.platform == "win32"
NVIDIA_QUERY: Final = (
    "temperature.gpu,clocks.current.sm,clocks.current.memory,"
    "clocks_throttle_reasons.active,power.draw"
)
_NVIDIA_FIELDS: Final = (
    "temperature_c",
    "sm_clock_mhz",
    "memory_clock_mhz",
    "throttle_reasons_hex",
    "power_draw_w",
)
_PROBE_TIMEOUT_S: Final = 10.0
_MAX_CPUS_REPORTED: Final = 128


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def on_battery(power_probe: dict[str, JSON] | None) -> bool | None:
    """``True`` when the inventory power probe says the machine is on battery.

    ``None`` when it cannot be known (probe failed, no battery information).
    """
    if not power_probe or not power_probe.get("ok"):
        return None
    value = power_probe.get("value") or {}
    on_ac = value.get("on_ac") if isinstance(value, dict) else None
    if on_ac is None:
        return None
    return not bool(on_ac)


def cpu_governors(root: Path = Path("/sys/devices/system/cpu")) -> dict[str, JSON]:
    """Linux ``scaling_governor`` / ``energy_performance_preference`` per CPU."""
    if not IS_LINUX or not root.is_dir():
        return {"source": "unavailable", "governors": None}
    governors: dict[str, str] = {}
    prefs: set[str] = set()
    try:
        cpus = sorted(root.glob("cpu[0-9]*"))[:_MAX_CPUS_REPORTED]
    except OSError:
        return {"source": "sysfs", "governors": None, "error": "cannot list CPUs"}
    for cpu in cpus:
        governor = _read(cpu / "cpufreq" / "scaling_governor")
        if governor:
            governors[cpu.name] = governor
        pref = _read(cpu / "cpufreq" / "energy_performance_preference")
        if pref:
            prefs.add(pref)
    if not governors:
        return {"source": "sysfs", "governors": None, "error": "no cpufreq driver"}
    return {
        "source": "sysfs",
        "governors": governors,
        "distinct": sorted(set(governors.values())),
        "energy_performance_preference": sorted(prefs) or None,
    }


def windows_power_plan() -> dict[str, JSON]:
    """The active Windows power scheme (``powercfg /getactivescheme``)."""
    if not IS_WINDOWS:
        return {"source": "unavailable", "plan": None}
    result = runner.run("powercfg", ["/getactivescheme"], timeout=_PROBE_TIMEOUT_S)
    if not result.ok:
        return {"source": "powercfg", "plan": None, "error": result.describe_failure()}
    text = result.stdout.strip()
    _, _, name = text.partition("(")
    return {"source": "powercfg", "plan": name.rstrip(")\n ").strip() or text}


def linux_thermal_zones(root: Path = Path("/sys/class/thermal")) -> JSON:
    """``{zone: {"type": ..., "temp_c": ...}}`` from ``/sys/class/thermal``."""
    if not IS_LINUX or not root.is_dir():
        return None
    zones: dict[str, JSON] = {}
    try:
        entries = sorted(root.glob("thermal_zone*"))
    except OSError:
        return None
    for zone in entries:
        raw = _read(zone / "temp")
        try:
            temp = int(raw) / 1000.0 if raw is not None else None
        except ValueError:
            temp = None
        zones[zone.name] = {"type": _read(zone / "type"), "temp_c": temp}
    return zones or None


def parse_nvidia_smi_row(line: str) -> dict[str, JSON]:
    """One CSV row of :data:`NVIDIA_QUERY` -> typed fields (``None`` for N/A)."""
    parts = [p.strip() for p in line.split(",")]
    row: dict[str, JSON] = {}
    for name, value in zip(_NVIDIA_FIELDS, parts, strict=False):
        # nvidia-smi writes "N/A", "[N/A]", "[Not Supported]" for fields a card
        # or driver does not expose; they are missing values, not numbers.
        plain = value.strip("[] ").lower()
        if not plain or plain.startswith(("n/a", "not supported", "unknown")):
            row[name] = None
        elif name == "throttle_reasons_hex":
            row[name] = value
        else:
            try:
                row[name] = float(value)
            except ValueError:
                row[name] = None
    return row


def nvidia_state() -> JSON:
    """GPU temperature / clocks / throttle reasons, or ``None`` without nvidia-smi."""
    if runner.which("nvidia-smi") is None:
        return None
    result = runner.run(
        "nvidia-smi",
        [f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
        timeout=_PROBE_TIMEOUT_S,
    )
    if not result.ok:
        return {"error": result.describe_failure()}
    rows = [parse_nvidia_smi_row(line) for line in result.stdout.splitlines() if line]
    return rows or {"error": "nvidia-smi returned no rows"}


def snapshot(when: str, *, power: Any | None = None) -> dict[str, JSON]:
    """One environment snapshot. ``power`` is a test seam (an inventory probe)."""
    probe = power if power is not None else probes.probe_power()
    return {
        "when": when,
        "power": probe,
        "on_battery": on_battery(probe),
        "cpu": cpu_governors() if IS_LINUX else windows_power_plan(),
        "thermal_zones": linux_thermal_zones(),
        "nvidia": nvidia_state(),
    }


_CACHE_UNITS: Final = {"K": 2**10, "M": 2**20, "G": 2**30}


def parse_cache_size(text: str | None) -> int | None:
    """``"24 MiB"`` / ``"32M"`` / ``"12288K"`` -> bytes; ``None`` when unreadable."""
    if not text:
        return None
    value = text.strip().upper().replace("IB", "").replace("B", "").strip()
    if not value:
        return None
    unit = _CACHE_UNITS.get(value[-1], 1)
    number = value[:-1] if unit != 1 else value
    try:
        size = float(number.strip())
    except ValueError:
        return None
    return int(size * unit) if size > 0 else None


def last_level_cache_bytes(root: Path = Path("/sys/devices/system/cpu")) -> int | None:
    """Size of the last-level cache of CPU 0 (Linux sysfs), else ``None``.

    Windows exposes no reliable per-level cache size without admin rights, so
    the memory-bandwidth buffer falls back to
    :data:`spikes._s7.sizing.DEFAULT_MEMORY_BYTES` there.
    """
    if not IS_LINUX:
        return None
    cache_dir = root / "cpu0" / "cache"
    if not cache_dir.is_dir():
        return None
    best: tuple[int, int] | None = None
    try:
        indexes = sorted(cache_dir.glob("index*"))
    except OSError:
        return None
    for index in indexes:
        try:
            level = int(_read(index / "level") or "")
        except ValueError:
            continue
        size = parse_cache_size(_read(index / "size"))
        if size is None:
            continue
        if best is None or level > best[0]:
            best = (level, size)
    return best[1] if best else None
