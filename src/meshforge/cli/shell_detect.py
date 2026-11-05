"""Which shell ``compute --install-completion`` / ``--show-completion`` is for
(V1.0 G9-F, D439).

Typer asks shellingham, which on Windows returns the **first** ancestor process
whose executable is a known shell. The installed ``compute`` is a ``.cmd``
launcher (``scripts/bundle_lib.py``), and Windows runs a batch file through
``cmd.exe /c`` - so from PowerShell the process tree is::

    powershell.exe -> cmd.exe /c ""...\\bin\\compute.cmd" --install-completion" -> python.exe

and shellingham answers ``cmd`` (lab L2 C8.10-g: "Shell cmd is not supported.").

:func:`pick_windows_shell` walks the same ancestors but **skips a cmd.exe started
with ``/c`` (or its alias ``/r``)**: such a cmd runs one command and exits, so it is
a launcher (ours, or ``cmd /c ...`` typed by the user), never the shell the user
is typing into. An interactive cmd (no switch, or ``/k``) is the user's shell:
when ``compute.cmd`` is typed in cmd.exe the batch file runs inside that same
process, so the walk stops there and answers ``cmd`` - which
:mod:`meshforge.cli.completion` turns into a clear "use PowerShell" message.

If a cmd's command line cannot be read (access denied - not expected for the
user's own processes), the first such cmd counts as a launcher only when the next
shell above it is PowerShell: the shape the launcher makes.

The decision is pure (:func:`pick_windows_shell` over :class:`Proc` records, unit
tested with fake trees on every OS); only :func:`_windows_ancestors` touches the
Win32 API (Toolhelp snapshot + ``NtQueryInformationProcess`` command line). Other
operating systems keep shellingham's answer.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "SHELL_NAMES",
    "Proc",
    "detect_shell",
    "is_transient_cmd",
    "pick_windows_shell",
]

SHELL_NAMES: Final = frozenset(
    {
        "sh",
        "ash",
        "dash",
        "bash",
        "zsh",
        "fish",
        "csh",
        "tcsh",
        "ksh",
        "xonsh",
        "nu",
        "powershell",
        "pwsh",
        "cmd",
    }
)
"""Executable stems that are shells (shellingham's list); anything else in the
ancestor chain (python, a ``compute.exe`` trampoline, uv, conhost) is walked past."""

_POWERSHELLS: Final = frozenset({"powershell", "pwsh"})
MAX_DEPTH: Final = 10
"""Ancestors looked at, as shellingham does."""


@dataclass(frozen=True, slots=True)
class Proc:
    """One ancestor process: its executable stem (lower case, no ``.exe``) and
    its command line (``None`` = could not be read)."""

    name: str
    cmdline: str | None


def _strip_program(cmdline: str) -> str:
    """The command line after the program token (quoted or not)."""
    text = cmdline.lstrip()
    if text.startswith('"'):
        end = text.find('"', 1)
        return "" if end < 0 else text[end + 1 :]
    head, _, rest = text.partition(" ")
    # `cmd/c foo` (no space) is accepted by cmd.exe as well
    slash = head.find("/")
    return head[slash:] + " " + rest if slash > 0 else rest


def is_transient_cmd(cmdline: str) -> bool:
    """``True`` when this cmd.exe was started to run one command (``/c`` or
    ``/r``) rather than as an interactive shell (no switch, or ``/k``).

    Only the leading switches count: ``/c`` inside the command it runs (a path,
    an argument) does not."""
    for token in _strip_program(cmdline).split():
        if not token.startswith("/"):
            return False
        # `/d/c` is two switches to cmd.exe
        for switch in token[1:].split("/"):
            letter = switch[:1].lower()
            if letter in ("c", "r"):
                return True
            if letter == "k":
                return False
    return False


def pick_windows_shell(ancestors: Sequence[Proc]) -> str | None:
    """The user's shell from python's ancestors, nearest first (module
    docstring); ``None`` when no ancestor is a shell."""
    launcher_seen = False
    for index, proc in enumerate(ancestors[: MAX_DEPTH + 1]):
        if proc.name not in SHELL_NAMES:
            continue
        if proc.name != "cmd":
            return proc.name
        if proc.cmdline is not None:
            if is_transient_cmd(proc.cmdline):
                continue
            return "cmd"
        above = next(
            (p.name for p in ancestors[index + 1 :] if p.name in SHELL_NAMES), None
        )
        if not launcher_seen and above in _POWERSHELLS:
            launcher_seen = True
            continue
        return "cmd"
    return None


def detect_shell(
    *,
    platform: str = sys.platform,
    ancestors: Callable[[], Sequence[Proc]] | None = None,
    fallback: Callable[[], str | None] | None = None,
) -> str | None:
    """The shell to install / show completion for.

    Windows: :func:`pick_windows_shell` over the real process tree (``ancestors``
    is a test seam). Elsewhere: ``fallback`` (Typer's shellingham lookup)."""
    if platform == "win32":
        try:
            chain = (ancestors or _windows_ancestors)()
        except OSError:
            chain = ()
        return pick_windows_shell(chain)
    return fallback() if fallback is not None else None


# --------------------------------------------------------------- Win32 access
# mypy checks this block with `--platform win32` (CI types job); on other
# platforms it is skipped, and the stand-ins below are used.
if sys.platform == "win32":  # pragma: no cover - Windows only

    def _windows_ancestors() -> list[Proc]:
        """Python's ancestors, nearest first, from one Toolhelp snapshot; only
        cmd.exe command lines are read (that is the only one the decision needs)."""
        table = dict(_snapshot())
        chain: list[Proc] = []
        seen: set[int] = set()
        pid = table.get(os.getpid(), (0, ""))[0]
        while pid and pid not in seen and len(chain) <= MAX_DEPTH:
            seen.add(pid)
            entry = table.get(pid)
            if entry is None:
                break
            ppid, exe = entry
            stem = exe.rpartition(".")[0].lower() if "." in exe else exe.lower()
            chain.append(Proc(stem, _command_line(pid) if stem == "cmd" else None))
            pid = ppid
        return chain

    def _snapshot() -> Iterator[tuple[int, tuple[int, str]]]:
        """``pid -> (parent pid, exe file name)`` for every process."""
        import ctypes
        from ctypes import wintypes

        class ProcessEntry32W(ctypes.Structure):
            _fields_ = (
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        th32cs_snapprocess = 0x2
        snap = kernel32.CreateToolhelp32Snapshot(th32cs_snapprocess, 0)
        if snap is None or snap == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            entry = ProcessEntry32W()
            entry.dwSize = ctypes.sizeof(entry)
            more = kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while more:
                yield (
                    int(entry.th32ProcessID),
                    (int(entry.th32ParentProcessID), str(entry.szExeFile)),
                )
                more = kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snap)

    def _command_line(pid: int) -> str | None:
        """A process's command line (``NtQueryInformationProcess`` class 60,
        ``ProcessCommandLineInformation``, Windows 8.1+), or ``None``."""
        import ctypes
        from ctypes import wintypes

        class UnicodeString(ctypes.Structure):
            _fields_ = (
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.c_void_p),
            )

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.POINTER(wintypes.ULONG),
        ]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long

        process_query_limited_information = 0x1000
        process_command_line_information = 60
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.ULONG(0)
            for _attempt in range(3):
                buffer = ctypes.create_string_buffer(max(size.value, 1024))
                status = ntdll.NtQueryInformationProcess(
                    handle,
                    process_command_line_information,
                    buffer,
                    len(buffer),
                    ctypes.byref(size),
                )
                if status == 0:
                    text = UnicodeString.from_buffer(buffer)
                    if not text.Buffer or not text.Length:
                        return ""
                    return ctypes.wstring_at(text.Buffer, text.Length // 2)
                # STATUS_INFO_LENGTH_MISMATCH / BUFFER_TOO_SMALL / BUFFER_OVERFLOW
                if status & 0xFFFFFFFF not in (0xC0000004, 0xC0000023, 0x80000005):
                    return None
            return None
        finally:
            kernel32.CloseHandle(handle)

else:

    def _windows_ancestors() -> list[Proc]:
        """No Windows process tree here."""
        return []

    def _command_line(pid: int) -> str | None:
        """No Windows process here."""
        del pid
        return None
