# ruff: noqa: E402 - the platform assert must precede the pywin32 imports
"""Windows half of ``meshforge-console-open`` (V0.17 G4, D391, Q30, Q31). Windows only.

The Start-menu shortcuts run ``pythonw.exe -I -m meshforge.agent.console.launcher``
(no console window), so there is no terminal: messages are message boxes, and
the two actions that need an administrator ask Windows for it (UAC) and run one
fixed command of :mod:`meshforge.agent.winservice` elevated:

* ``--docker-access`` -> ``winservice docker-access`` (Q31: the service account
  joins ``docker-users`` and restarts; never automatic);
* "request access" (Q30), offered when this user may not read the console
  token -> ``winservice console-access add --sid <this user's SID>``. An
  administrator approves in the UAC prompt; over-the-shoulder elevation runs as
  the admin, so the SID is passed explicitly (it is the *requesting* user's).

The elevated process is the bundle's own ``python.exe`` (hidden window); its
exit code is the answer. Nothing here sees the token.
"""

from __future__ import annotations

import sys

assert sys.platform == "win32"  # noqa: S101 - tells mypy this module is Windows-only

import ctypes
import subprocess
from pathlib import Path
from typing import Final

import pywintypes
import win32api
import win32con
import win32event
import win32process
import win32security
from win32com.shell import shell, shellcon

__all__ = [
    "ElevationCancelledError",
    "ask_yes_no",
    "current_user_sid",
    "message_box",
    "run_elevated",
    "service_python",
]

_TITLE: Final = "MeshForge"
_MB_OK: Final = 0x0
_MB_YESNO: Final = 0x4
_MB_ICONINFORMATION: Final = 0x40
_MB_ICONQUESTION: Final = 0x20
_MB_ICONWARNING: Final = 0x30
_IDYES: Final = 6
_ERROR_CANCELLED: Final = 1223


class ElevationCancelledError(Exception):
    """The user dismissed the UAC prompt (or no administrator approved)."""


def message_box(text: str, *, warning: bool = False) -> None:
    icon = _MB_ICONWARNING if warning else _MB_ICONINFORMATION
    ctypes.windll.user32.MessageBoxW(None, text, _TITLE, _MB_OK | icon)


def ask_yes_no(text: str) -> bool:
    answer = ctypes.windll.user32.MessageBoxW(
        None, text, _TITLE, _MB_YESNO | _MB_ICONQUESTION
    )
    return int(answer) == _IDYES


def service_python() -> str:
    """The console ``python.exe`` next to this ``pythonw.exe`` (the bundle's)."""
    exe = Path(sys.executable)
    candidate = exe.with_name("python.exe")
    return str(candidate if candidate.is_file() else exe)


def current_user_sid() -> str:
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    try:
        user, _attrs = win32security.GetTokenInformation(token, win32security.TokenUser)
    finally:
        win32api.CloseHandle(token)
    return str(win32security.ConvertSidToStringSid(user))


def run_elevated(args: list[str], *, timeout_s: float = 180.0) -> int:
    """``python.exe -I -m meshforge.agent.winservice <args>`` through UAC; its
    exit code. :class:`ElevationCancelledError` when the prompt is dismissed."""
    params = subprocess.list2cmdline(["-I", "-m", "meshforge.agent.winservice", *args])
    try:
        info = shell.ShellExecuteEx(
            fMask=shellcon.SEE_MASK_NOCLOSEPROCESS | shellcon.SEE_MASK_NO_CONSOLE,
            lpVerb="runas",
            lpFile=service_python(),
            lpParameters=params,
            nShow=win32con.SW_HIDE,
        )
    except pywintypes.error as exc:
        if exc.winerror == _ERROR_CANCELLED:
            raise ElevationCancelledError from None
        raise
    handle = info["hProcess"]
    try:
        waited = win32event.WaitForSingleObject(handle, int(timeout_s * 1000))
        if waited != win32event.WAIT_OBJECT_0:
            return 1
        return int(win32process.GetExitCodeProcess(handle))
    finally:
        win32api.CloseHandle(handle)
