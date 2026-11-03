# ruff: noqa: E402 - the platform assert must precede the winreg import
"""The Windows CPU name from the registry (V1.0 G8 F6). Windows only.

``HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0`` value
``ProcessorNameString`` is written by the kernel at boot; ``Win32_Processor.Name``
(the CIM query this replaced) reports the same string. A registry read takes
microseconds and starts no process, so Defender/AMSI scanning a PowerShell
start-up can no longer stall hardware discovery.
"""

from __future__ import annotations

import sys

assert sys.platform == "win32"  # noqa: S101 - tells mypy this module is Windows-only

import winreg
from typing import Final

__all__ = ["processor_name_string"]

_CPU0_KEY: Final = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
_MAX_CHARS: Final = 256


def processor_name_string() -> str:
    """CPU 0's ``ProcessorNameString``, or ``""`` when it cannot be read."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _CPU0_KEY) as key:
            value, kind = winreg.QueryValueEx(key, "ProcessorNameString")
    except OSError:
        return ""
    if kind != winreg.REG_SZ or not isinstance(value, str):
        return ""
    return value[:_MAX_CHARS]
