"""Facts about the D384 runtime bundle this process runs from, if any (V0.17 G1).

``scripts/build_bundle.py`` writes ``BUILDINFO.json`` at the bundle root::

    <root>/BUILDINFO.json          {"version", "git_sha", "role", "python": {...}, ...}
    <root>/python/bin/python3.12   POSIX interpreter (= sys.executable)
    <root>/python/python.exe       Windows interpreter
    <root>/bin/meshforge-agent     launchers (Windows: .cmd)

A developer checkout (``uv run``) has no ``BUILDINFO.json`` near its interpreter,
so every function here returns ``None`` there and callers keep their dev behaviour.

The version itself has one source of truth, the installed package metadata
(``meshforge.__version__``); ``BUILDINFO.json`` only adds the git commit and the
bundle's own facts to ``--version``.

``stable_root`` serves SP3 finding F2: the Linux package installs the bundle at
``/opt/meshforge/<ver>/`` and points ``/opt/meshforge/current`` at it. Anything
that remembers the interpreter's path across an upgrade (a job venv's
``pyvenv.cfg``) must remember the ``current`` path, which survives the upgrade,
not the versioned one, which the upgrade deletes.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Final

from meshforge import __version__

__all__ = [
    "BUILDINFO_NAME",
    "CURRENT_LINK_NAME",
    "BuildInfo",
    "bundle_command",
    "bundle_root",
    "current_build_info",
    "read_build_info",
    "stable_root",
    "version_line",
]

BUILDINFO_NAME: Final = "BUILDINFO.json"
CURRENT_LINK_NAME: Final = "current"
"""``/opt/meshforge/current`` -> ``<ver>`` (D385): the pointer an upgrade swaps."""
_MAX_DEPTH: Final = 3
"""``python/bin/python3.12`` is two folders below the root; one spare."""


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """The parts of ``BUILDINFO.json`` the running code uses."""

    root: Path
    version: str
    git_sha: str
    role: str
    python_version: str
    raw: dict[str, object] = field(repr=False, compare=False)

    @property
    def short_sha(self) -> str:
        return self.git_sha[:12]


def bundle_root(executable: str | os.PathLike[str] | None = None) -> Path | None:
    """The bundle root holding ``executable`` (default ``sys.executable``), or
    ``None`` outside a bundle. Not resolved through symlinks: the caller's own
    path is kept (a venv's ``bin/python`` symlink to the bundle is *not* the
    bundle - its ``sys.executable`` is the venv's)."""
    exe = Path(executable if executable is not None else sys.executable)
    if not exe.is_absolute():
        return None
    for parent in list(exe.parents)[:_MAX_DEPTH]:
        if (parent / BUILDINFO_NAME).is_file():
            return parent
    return None


def read_build_info(root: Path) -> BuildInfo | None:
    """``root/BUILDINFO.json`` parsed; ``None`` if missing or unreadable (a
    damaged file must not break ``--version``)."""
    try:
        raw = json.loads((root / BUILDINFO_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    python = raw.get("python")
    return BuildInfo(
        root=root,
        version=str(raw.get("version", "")),
        git_sha=str(raw.get("git_sha", "")),
        role=str(raw.get("role", "")),
        python_version=str(
            python.get("version", "") if isinstance(python, dict) else ""
        ),
        raw=raw,
    )


@cache
def current_build_info() -> BuildInfo | None:
    """:func:`read_build_info` of this process's bundle (cached)."""
    root = bundle_root()
    return read_build_info(root) if root is not None else None


def version_line(prog: str, info: BuildInfo | None = None) -> str:
    """``--version`` text: ``<prog> <version>``, plus ``(git <sha>, python
    <ver>, bundle)`` when running from a bundle."""
    info = current_build_info() if info is None else info
    line = f"{prog} {__version__}"
    if info is None:
        return line
    extras = [f"git {info.short_sha or 'unknown'}"]
    if info.python_version:
        extras.append(f"python {info.python_version}")
    extras.append("bundle")
    return f"{line} ({', '.join(extras)})"


def bundle_command(name: str, root: Path | None = None) -> Path | None:
    """The bundle's own launcher ``bin/<name>`` (``.cmd`` on Windows), if this
    process runs from a bundle that has it (SP3 F5: ``compute`` finds *its*
    agent before anything on ``PATH``)."""
    root = bundle_root() if root is None else root
    if root is None:
        return None
    suffix = ".cmd" if sys.platform == "win32" else ""
    candidate = root / "bin" / (name + suffix)
    return candidate if candidate.is_file() else None


def stable_root(root: Path) -> Path:
    """``<parent>/current`` when that pointer resolves to ``root`` (the Linux
    package layout), else ``root`` unchanged (a bundle unpacked anywhere else,
    or Windows where the installer rewrites the service path instead, D385)."""
    link = root.parent / CURRENT_LINK_NAME
    try:
        if link.resolve(strict=True) == root.resolve(strict=True):
            return link
    except OSError:
        pass
    return root
