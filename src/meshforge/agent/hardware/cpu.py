"""CPU model / core counts and total RAM (roadmap 0.3.3).

Source order per platform, best first, each one guarded:

* **any**: ``psutil`` for the physical-core count and total RAM, ``os.cpu_count()``
  for logical threads (it is never wrong and never blocks).
* **Linux**: ``/proc/cpuinfo`` ``model name`` for the marketing string
  (``platform.processor()`` returns only ``"x86_64"`` there).
* **Windows**: the registry's ``ProcessorNameString`` (the marketing string that
  ``Win32_Processor.Name`` reports - WMI reads it from the same key), read in
  :mod:`.cpu_windows` with ``winreg``: no child process. Until V1.0 G8 F6 this was
  a PowerShell CIM query; a stalled PowerShell (Defender/AMSI, cold start) plus
  its kill took ``hardware show`` past its 6 s deadline on the Windows CI
  runner. Fallback: ``platform.processor()`` (the CPU identifier, always set).
* **anything else** (macOS, BSD): ``platform.processor()``.

Nothing here raises: a caller gets whatever was learned, with empty strings and
zeros for what was not. The parsers are pure functions so the Linux and Windows
paths are unit-tested on every platform.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.agent.hardware.optional import psutil_module
from meshforge.agent.hardware.probe import Budget, ProbeOutcome, guarded

__all__ = [
    "CPU_PROBE_CAP_S",
    "CpuFacts",
    "parse_cpuinfo_model",
    "parse_meminfo_total_bytes",
    "parse_win32_processor",
    "probe_cpu",
]

CPU_PROBE_CAP_S: Final = 2.0
"""Cap for the whole CPU probe (every source is a file or registry read now)."""

_PROC_CPUINFO: Final = Path("/proc/cpuinfo")
_PROC_MEMINFO: Final = Path("/proc/meminfo")


@dataclass(frozen=True, slots=True)
class CpuFacts:
    """What Hello needs about the CPU, plus where the model string came from."""

    model: str
    physical_cores: int
    logical_threads: int
    ram_bytes: int
    source: str


def parse_cpuinfo_model(text: str) -> str:
    """First ``model name`` (x86) / ``Model`` (ARM) line of ``/proc/cpuinfo``; else ``""``."""
    fallback = ""
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        name = key.strip().lower()
        if name == "model name":
            return " ".join(value.split())
        if name in {"hardware", "model"} and not fallback:
            fallback = " ".join(value.split())
    return fallback


def parse_meminfo_total_bytes(text: str) -> int:
    """``MemTotal`` of ``/proc/meminfo`` in bytes; 0 when absent or unparseable."""
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() == "MemTotal":
            parts = value.split()
            if parts and parts[0].isdigit():
                # MemTotal is reported in kibibytes unless the unit says otherwise.
                factor = 1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1
                return int(parts[0]) * factor
    return 0


def parse_win32_processor(text: str) -> str:
    """The processor name from a ``Win32_Processor.Name`` / registry
    ``ProcessorNameString`` value: first non-empty line, whitespace collapsed
    (Intel pads the registry string with leading spaces)."""
    for line in text.splitlines():
        stripped = " ".join(line.split())
        if stripped:
            return stripped
    return ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _ram_bytes() -> int:
    psutil = psutil_module()
    if psutil is not None:
        try:
            return int(psutil.virtual_memory().total)
        except (OSError, RuntimeError, ValueError):
            pass
    # Not `return 0` in an else: mypy narrows sys.platform and would call the
    # non-Linux branch unreachable on this machine.
    total = 0
    if sys.platform.startswith("linux"):
        total = parse_meminfo_total_bytes(_read(_PROC_MEMINFO))
    return total


def _physical_cores(logical: int) -> int:
    psutil = psutil_module()
    if psutil is None:
        return logical
    try:
        physical = psutil.cpu_count(logical=False)
    except (OSError, RuntimeError, ValueError):
        return logical
    return int(physical) if physical else logical


def _registry_processor_name() -> str:
    """``ProcessorNameString`` of CPU 0, or ``""`` (not Windows / not readable)."""
    name = ""
    if sys.platform == "win32":
        from meshforge.agent.hardware.cpu_windows import processor_name_string

        name = processor_name_string()
    return name


def _windows_model() -> tuple[str, str]:
    """``(model, source)`` on Windows: the registry name, else
    ``platform.processor()`` (the ``PROCESSOR_IDENTIFIER`` string, always set)."""
    model = parse_win32_processor(_registry_processor_name())
    if model:
        return model, "windows-registry"
    return platform.processor(), "platform"


def _collect(_timeout_s: float) -> CpuFacts:
    # The budget slice is unused: no source here can block (F6 removed the
    # PowerShell query), but ``guarded`` passes one to every probe.
    logical = os.cpu_count() or 0
    model, source = "", "platform"
    if sys.platform.startswith("linux"):
        model = parse_cpuinfo_model(_read(_PROC_CPUINFO))
        source = "proc" if model else "platform"
    elif sys.platform == "win32":
        model, source = _windows_model()
    if not model:
        model = platform.processor() or platform.machine() or ""
        source = "platform"
    return CpuFacts(
        model=model,
        physical_cores=_physical_cores(logical),
        logical_threads=logical,
        ram_bytes=_ram_bytes(),
        source=source,
    )


def probe_cpu(
    budget: Budget, *, cap_s: float = CPU_PROBE_CAP_S
) -> ProbeOutcome[CpuFacts]:
    """CPU facts for this machine; always ``OK`` unless the collection itself broke."""
    return guarded("cpu", budget, cap_s, _collect)
