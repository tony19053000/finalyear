"""No invisible error dialogs on Windows (V1.0 G10 BUG-4, D451).

A Windows process that hits a critical error (a drive or DLL that cannot be
opened, a GP fault) may show a modal system dialog and wait for a click. The
agent runs as a service - there is no desktop and nobody to click - so such a
worker would wait for ever with every thread blocked, holding its chunk (lab L4:
a chunk worker idle 17 min, all threads ``Wait``). The error mode is inherited
by every child (``CreateProcess`` without ``CREATE_DEFAULT_ERROR_MODE``), so
setting it once in the agent covers every workload, venv launcher and env build:

* ``SEM_FAILCRITICALERRORS`` - the call fails instead of asking;
* ``SEM_NOGPFAULTERRORBOX`` - no "stopped working" box: the process just ends;
* ``SEM_NOOPENFILEERRORBOX`` - no "cannot find file" box.

The bits already set are kept. Anywhere else, and on any error, it is a no-op:
it must never stop the agent from starting.
"""

from __future__ import annotations

import sys
from typing import Any, Final

from meshforge.common.logging import get_logger

__all__ = ["QUIET_FLAGS", "quiet_error_mode"]

log = get_logger(__name__)

SEM_FAILCRITICALERRORS: Final = 0x0001
SEM_NOGPFAULTERRORBOX: Final = 0x0002
SEM_NOOPENFILEERRORBOX: Final = 0x8000
QUIET_FLAGS: Final = (
    SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
)


def _kernel32() -> Any:  # pragma: no cover - Windows only
    import ctypes

    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]


def quiet_error_mode(
    *, platform: str = sys.platform, kernel32: Any | None = None
) -> int | None:
    """Add :data:`QUIET_FLAGS` to this process's (inheritable) error mode.
    Returns the new mode, or None (not Windows, or it could not be set)."""
    if platform != "win32":
        return None
    try:
        k32 = kernel32 if kernel32 is not None else _kernel32()
        mode = int(k32.GetErrorMode()) | QUIET_FLAGS
        k32.SetErrorMode(mode)
    except (OSError, AttributeError, ValueError) as exc:
        log.warning("agent.error_mode_not_set", error=type(exc).__name__)
        return None
    log.info("agent.error_mode_set", mode=hex(mode))
    return mode
