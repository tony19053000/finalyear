"""Pure parts of the Windows install steps (V0.17 G4, D385/D389/D395, Q30/Q31).

Everything the Inno Setup installer's helper (``winservice setup`` /
``teardown``, :mod:`meshforge.agent.winsetup`) hands to the SCM, ``icacls``,
``schtasks``, ``net`` and the registry is *decided* here, so it is unit-tested on
every OS; :mod:`meshforge.agent.winservice_ops` only applies it on Windows.

Program layout (D385)::

    C:\\Program Files\\MeshForge\\           ProgramLayout.root (read-only for users)
        <version>\\python\\python.exe        the bundle of one version (D384)
        <version>\\bin\\meshforge-agent.cmd  its launchers
        current.txt                         the version the service runs
        bin\\meshforge-agent.cmd, compute.cmd  PATH shims -> current version

The service's ``binPath`` names the versioned ``python.exe`` (no symlink pointer
on Windows); an upgrade rewrites it and ``current.txt``, a failed upgrade puts
both back. The PATH holds only ``<root>\\bin``, whose shims are rewritten to the
running version, so a new version never needs a PATH change.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Final

from meshforge.common.windows_service import SERVICE_ACCOUNT

__all__ = [
    "CURRENT_FILE_NAME",
    "DOCKER_USERS_GROUP",
    "LEGACY_TASK_NAME",
    "SHIM_COMMANDS",
    "SHIM_DIR_NAME",
    "ProgramLayout",
    "console_grant_argv",
    "console_revoke_argv",
    "docker_users_argv",
    "parse_command_line",
    "path_add",
    "path_remove",
    "program_layout",
    "schtasks_argv",
    "service_python",
    "shim_text",
]

CURRENT_FILE_NAME: Final = "current.txt"
SHIM_DIR_NAME: Final = "bin"
SHIM_COMMANDS: Final = ("meshforge-agent", "compute")
"""On the PATH for administrators and job submitters; the console launcher is a
Start-menu shortcut (``pythonw.exe``), not a command."""
LEGACY_TASK_NAME: Final = "MeshForgeAgent"
"""The development scheduled task (``deploy/windows``, roadmap 0.3.13). Same name
as the service - a *task*, not a service: messages always say which."""
DOCKER_USERS_GROUP: Final = "docker-users"
"""Created by Docker Desktop's installer (not localised)."""


# ----------------------------------------------------------------- program dir
@dataclass(frozen=True, slots=True)
class ProgramLayout:
    root: PureWindowsPath
    """``C:\\Program Files\\MeshForge``."""
    version_dir: str
    """The versioned folder's name under ``root`` (e.g. ``0.17.0-1``)."""

    @property
    def version_path(self) -> PureWindowsPath:
        return self.root / self.version_dir

    @property
    def python_exe(self) -> PureWindowsPath:
        return self.version_path / "python" / "python.exe"

    @property
    def pythonw_exe(self) -> PureWindowsPath:
        return self.version_path / "python" / "pythonw.exe"

    @property
    def current_file(self) -> PureWindowsPath:
        return self.root / CURRENT_FILE_NAME

    @property
    def shim_dir(self) -> PureWindowsPath:
        return self.root / SHIM_DIR_NAME


def program_layout(python_exe: str | PureWindowsPath) -> ProgramLayout:
    """``<root>\\<version>\\python\\python.exe`` -> its layout.

    Raises:
        ValueError: ``python_exe`` is not a bundle interpreter in that layout.
    """
    exe = PureWindowsPath(python_exe)
    if exe.name.lower() not in ("python.exe", "pythonw.exe") or len(exe.parents) < 3:
        raise ValueError(f"{exe} is not <program dir>\\<version>\\python\\python.exe")
    if exe.parent.name.lower() != "python":
        raise ValueError(f"{exe} is not <program dir>\\<version>\\python\\python.exe")
    version = exe.parents[1].name
    if not version or version in (".", ".."):
        raise ValueError(f"{exe} has no version folder")
    return ProgramLayout(exe.parents[2], version)


def shim_text(layout: ProgramLayout, command: str) -> str:
    """``<root>\\bin\\<command>.cmd``: runs the running version's own launcher.

    ``%~dp0`` is the shim's folder (``<root>\\bin\\``); the call is the last
    line, so cmd's exit code is the launcher's."""
    if command not in SHIM_COMMANDS:
        raise ValueError(f"no shim for {command!r}")
    return (
        "@echo off\r\n"
        f"rem {command} - MeshForge PATH shim, written by the installer for the "
        "version in current.txt (do not edit).\r\n"
        f'"%~dp0..\\{layout.version_dir}\\bin\\{command}.cmd" %*\r\n'
    )


# ------------------------------------------------------------ command lines
def parse_command_line(cmdline: str) -> list[str]:
    """Split a Windows command line like ``CommandLineToArgvW`` / the MS C runtime
    (the inverse of :func:`subprocess.list2cmdline`): ``2n`` backslashes before a
    quote are ``n`` backslashes and the quote toggles; ``2n+1`` are ``n`` and a
    literal quote; other backslashes are literal; ``""`` inside quotes is a
    literal quote. The first argument (the program) follows the simpler rule
    the system uses for it: quotes delimit, backslashes are literal."""
    args: list[str] = []
    i, n = 0, len(cmdline)
    while i < n and cmdline[i] in " \t":
        i += 1
    if i >= n:
        return args
    # argv[0]
    if cmdline[i] == '"':
        end = cmdline.find('"', i + 1)
        end = n if end < 0 else end
        args.append(cmdline[i + 1 : end])
        i = end + 1
    else:
        start = i
        while i < n and cmdline[i] not in " \t":
            i += 1
        args.append(cmdline[start:i])
    while True:
        while i < n and cmdline[i] in " \t":
            i += 1
        if i >= n:
            return args
        current: list[str] = []
        in_quotes = False
        while i < n and (in_quotes or cmdline[i] not in " \t"):
            ch = cmdline[i]
            if ch == "\\":
                slashes = 0
                while i < n and cmdline[i] == "\\":
                    slashes += 1
                    i += 1
                if i < n and cmdline[i] == '"':
                    current.append("\\" * (slashes // 2))
                    if slashes % 2:
                        current.append('"')
                        i += 1
                    continue
                current.append("\\" * slashes)
                continue
            if ch == '"':
                if in_quotes and i + 1 < n and cmdline[i + 1] == '"':
                    current.append('"')
                    i += 2
                    continue
                in_quotes = not in_quotes
                i += 1
                continue
            current.append(ch)
            i += 1
        args.append("".join(current))


def service_python(bin_path: str) -> str | None:
    """The interpreter a ``winservice host`` service runs (its ``binPath``'s
    program), or ``None`` when ``bin_path`` is not one of ours."""
    argv = parse_command_line(bin_path)
    if len(argv) < 5 or argv[1:5] != ["-I", "-m", "meshforge.agent.winservice", "host"]:
        return None
    return argv[0]


# -------------------------------------------------------------------- PATH
def _norm(entry: str) -> str:
    return entry.strip().strip('"').rstrip("\\/").lower()


def path_add(value: str, entry: str) -> str:
    """``value`` (a ``;``-separated PATH) with ``entry`` appended, unless an
    equal entry (case-, quote- and trailing-slash-insensitive) is there."""
    parts = [p for p in value.split(";") if p.strip()]
    if any(_norm(p) == _norm(entry) for p in parts):
        return ";".join(parts)
    return ";".join([*parts, entry])


def path_remove(value: str, entry: str) -> str:
    """``value`` without every entry equal to ``entry`` (same comparison as
    :func:`path_add`); everything else kept in order, empty items dropped."""
    return ";".join(
        p for p in value.split(";") if p.strip() and _norm(p) != _norm(entry)
    )


# --------------------------------------------------------- tool command lines
def schtasks_argv(action: str, name: str = LEGACY_TASK_NAME) -> list[str]:
    """``schtasks`` for the legacy development task: ``query`` (CSV, verbose,
    so the status column can be read), ``end``, ``disable``, ``delete``."""
    base = ["schtasks"]
    if action == "query":
        return [*base, "/Query", "/TN", name, "/FO", "CSV", "/V", "/NH"]
    if action == "end":
        return [*base, "/End", "/TN", name]
    if action == "disable":
        return [*base, "/Change", "/TN", name, "/DISABLE"]
    if action == "delete":
        return [*base, "/Delete", "/TN", name, "/F"]
    raise ValueError(f"unknown schtasks action {action!r}")


def docker_users_argv(*, add: bool) -> list[str]:
    """Q31: the service account in (or out of) ``docker-users``."""
    return [
        "net",
        "localgroup",
        DOCKER_USERS_GROUP,
        SERVICE_ACCOUNT,
        "/add" if add else "/delete",
    ]


def console_grant_argv(console_dir: PureWindowsPath, sids: Sequence[str]) -> list[str]:
    """Q30: read access to the console token folder for ``sids`` (inherited by
    the token file the service writes there). ``/grant`` adds without replacing
    other users' grants; the folder keeps its inheritance removed."""
    if not sids:
        raise ValueError("no SID to grant")
    argv = ["icacls", str(console_dir)]
    for sid in dict.fromkeys(sids):
        argv += ["/grant", f"*{sid}:(OI)(CI)RX"]
    return argv


def console_revoke_argv(console_dir: PureWindowsPath, sids: Sequence[str]) -> list[str]:
    if not sids:
        raise ValueError("no SID to revoke")
    argv = ["icacls", str(console_dir), "/remove:g"]
    return [*argv, *(f"*{sid}" for sid in dict.fromkeys(sids))]
