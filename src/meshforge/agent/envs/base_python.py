"""Which base interpreter a venv runs on, and when that base is stale (V1.0 G11, D458).

A uv venv does not contain Python: ``pyvenv.cfg`` names the base interpreter's
folder (``home``) and the venv's ``python`` is a symlink (POSIX) or a launcher
(Windows) that starts the interpreter found there. Lab L4 (Windows, agent
0.17.1-1 -> 0.17.2-1): the Windows installer puts each version side by side
(``C:\\Program Files\\MeshForge\\<ver>-<rev>\\``) and keeps the old folder, and
the env hash named only ``cpython-3.12.x`` - the same in both versions - so the
cached venvs kept running ``0.17.1-1\\python\\python.exe`` after the upgrade.

Three rules close it:

1. **The env hash names the base interpreter** (:func:`interpreter_identity`):
   implementation, its resolved real path, the full ``sys.version`` (build
   included) and its platform tag, as a sha256 digest (no path in the hash
   input). Another interpreter - an upgraded bundle, a rebuilt system Python -
   is another venv. The interpreter is probed once per agent process
   (:meth:`EnvBuilder.find_interpreter`), so a cache hit costs no extra process.
2. **A venv whose base moved is never used** (:func:`resolved_home`): its
   marker records the resolved ``home`` at build time; a ``home`` that is gone,
   or now resolves elsewhere (a ``current`` pointer re-pointed), makes the venv
   ``damaged`` - evicted and rebuilt, never run on a missing or other interpreter.
3. **GC removes venvs of an old or vanished base** (:func:`stale_base`), quota or
   not: once every venv on an old bundle is gone, nothing points into the old
   version folder any more and it can be deleted safely.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Final, Literal

from meshforge.common.buildinfo import bundle_root

__all__ = [
    "BASE_HOME_KEY",
    "StaleBase",
    "interpreter_identity",
    "pyvenv_home",
    "resolved_home",
    "same_path",
    "stale_base",
]

BASE_HOME_KEY: Final = "base_home"
"""Marker key: the venv's ``home``, resolved, when it was built."""
_MARKER: Final = ".meshforge-env.json"
_MAX_MARKER_BYTES: Final = 64 * 1024
StaleBase = Literal["base_missing", "base_old"]


def pyvenv_home(venv: Path) -> Path | None:
    """``home`` from ``venv/pyvenv.cfg`` (the base interpreter's folder), or None
    (no file, unreadable, or no ``home`` key)."""
    try:
        text = (venv / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "home" and value.strip():
            return Path(value.strip())
    return None


def _canonical(path: str | os.PathLike[str]) -> str:
    """Resolved and case-folded where the OS folds case (Windows)."""
    return os.path.normcase(os.path.realpath(path))


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """``a`` and ``b`` name the same place once both are resolved."""
    return _canonical(a) == _canonical(b)


def resolved_home(venv: Path) -> str | None:
    """``home`` of ``venv`` resolved through symlinks/junctions (what the venv
    really runs), or None when there is no ``home``. Blocking (``realpath``)."""
    home = pyvenv_home(venv)
    return None if home is None else _canonical(home)


def interpreter_identity(
    implementation: str, executable: str, sys_version: str, platform: str
) -> str:
    """sha256 hex of the base interpreter's identity (rule 1): implementation,
    resolved real path of ``executable``, full ``sys.version`` and platform tag.
    Node-local by design: the controller keeps env hashes per node."""
    document = [implementation, _canonical(executable), sys_version, platform]
    canonical = json.dumps(document, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _recorded_home(venv: Path) -> str | None:
    try:
        with (venv / _MARKER).open("rb") as fh:
            data = json.loads(fh.read(_MAX_MARKER_BYTES))
    except (OSError, ValueError, RecursionError):
        return None
    value = data.get(BASE_HOME_KEY) if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def stale_base(venv: Path, current_root: Path | None) -> StaleBase | None:
    """Why the venv's base interpreter is stale (rule 3), or None.

    ``base_missing``: ``pyvenv.cfg`` names a ``home`` that does not exist, or
    the ``home`` recorded at build time is gone. ``base_old``: that base lies in
    a MeshForge bundle that is not ``current_root`` (this agent's own bundle,
    resolved; None = a dev agent, where only ``base_missing`` applies). A folder
    without a ``home`` is not judged (None). Blocking: two small reads and a
    few ``stat`` calls."""
    home = pyvenv_home(venv)
    if home is None:
        return None
    if not home.is_dir():
        return "base_missing"
    recorded = _recorded_home(venv)
    base = recorded if recorded is not None else _canonical(home)
    if recorded is not None and not Path(recorded).is_dir():
        return "base_missing"
    if current_root is None:
        return None
    root = bundle_root(Path(base) / "python")
    if root is not None and not same_path(root, current_root):
        return "base_old"
    return None
