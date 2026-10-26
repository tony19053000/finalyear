"""What the installers leave behind about upgrades, and how the programs read it
(V0.17 G5, D393/D403, Q35/Q38/Q39).

Every installed program folder (D385) has an admin-only ``.upgrade`` folder next
to its ``current`` pointer::

    /opt/meshforge/.upgrade/                       agent .deb
    /opt/meshforge/controller/.upgrade/            controller .deb
    C:\\Program Files\\MeshForge\\upgrade\\          Windows agent

It is written only by the package scripts / the elevated installer (root /
Administrators own the program folders; the service account and job code can
not write there) and holds:

* ``state`` - the upgrade in progress (``from``, ``to``, ``phase``, ``boot``,
  ``previous``), ``key=value`` lines; present = an upgrade has not finished;
* ``rolled-back.json`` - the last upgrade failed and the previous version runs
  again (cleared by the next good upgrade);
* ``interrupted.json`` - a start found a half-finished upgrade and what it did
  about it (reverted to the previous version / let the new one finish);
* ``fail-upgrade-to`` - the **forced-failure trigger** for tests and the lab
  (Q38): one line, a version (or ``*``); the upgrade to that version fails its
  health check, so the rollback path runs for real. An admin creates it by hand;
  it is never a configuration option and the package removes it after use.

Q38 asked for "a file under the admin-only data dir". On Linux the data dir
(``/var/lib/meshforge``) belongs to the service account - which also runs the
jobs - so a root-run maintainer script reading or writing there could be
redirected by a planted symlink. The program folder is root-only by
construction, so the trigger and the upgrade records live there (deviation
recorded in the G5 progress note and ADR-0022).

This module only **reads** (doctor, the local console, the controller's
start-up health check). The writers are ``packaging/linux/common/upgrade.sh``
and :mod:`meshforge.agent.winsetup`.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

type NoticeKind = Literal["rolled_back", "interrupted", "in_progress"]

__all__ = [
    "FORCED_FAILURE_NAME",
    "INTERRUPTED_NAME",
    "PREVIOUS_DIR_NAME",
    "ROLLED_BACK_NAME",
    "STATE_NAME",
    "UPGRADE_DIR_NAME",
    "UpgradeNotice",
    "forced_failure",
    "program_dir",
    "upgrade_dir",
    "upgrade_notice",
]

UPGRADE_DIR_NAME: Final = ".upgrade"
"""POSIX; Windows uses ``upgrade`` (no hidden-dot convention there)."""
WINDOWS_UPGRADE_DIR_NAME: Final = "upgrade"
PREVIOUS_DIR_NAME: Final = ".previous"
STATE_NAME: Final = "state"
ROLLED_BACK_NAME: Final = "rolled-back.json"
INTERRUPTED_NAME: Final = "interrupted.json"
FORCED_FAILURE_NAME: Final = "fail-upgrade-to"
_MAX_READ: Final = 64 * 1024


def program_dir(bundle_root: Path) -> Path:
    """The folder holding ``current`` and ``.upgrade`` for a bundle installed at
    ``bundle_root`` (``<dir>/<ver>`` or, after a rollback, ``<dir>/.previous/<ver>``)."""
    parent = bundle_root.parent
    if parent.name == PREVIOUS_DIR_NAME:
        return parent.parent
    return parent


def upgrade_dir(program: Path, *, windows: bool | None = None) -> Path:
    win = sys.platform == "win32" if windows is None else windows
    return program / (WINDOWS_UPGRADE_DIR_NAME if win else UPGRADE_DIR_NAME)


def _read_trusted(path: Path, require_root: bool = True) -> str | None:
    """``path``'s text if it is a regular file the admin wrote: not a symlink
    and, on POSIX, owned by root (the folder is root-only anyway; this is a
    second lock). ``None`` otherwise."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    if require_root and sys.platform != "win32" and st.st_uid != 0:
        return None
    try:
        with path.open("rb") as handle:
            return handle.read(_MAX_READ).decode("utf-8", "replace")
    except OSError:
        return None


def forced_failure(
    program: Path,
    version: str,
    *,
    windows: bool | None = None,
    require_root: bool = True,
) -> str | None:
    """Q38: the reason to fail this upgrade's health check on purpose, or
    ``None``. Matches when the trigger's first line is ``version`` or ``*``."""
    path = upgrade_dir(program, windows=windows) / FORCED_FAILURE_NAME
    text = _read_trusted(path, require_root)
    if text is None:
        return None
    wanted = (text.strip().splitlines() or [""])[0].strip()
    if wanted not in (version, "*"):
        return None
    return (
        f"forced upgrade failure: {path} names {wanted!r} (a test/lab trigger; "
        "delete the file to upgrade normally)"
    )


@dataclass(frozen=True, slots=True)
class UpgradeNotice:
    """What ``doctor`` and the console say about the last upgrade."""

    kind: NoticeKind
    message: str
    details: dict[str, str]


def _json(path: Path, require_root: bool) -> dict[str, str] | None:
    text = _read_trusted(path, require_root)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): str(v) for k, v in data.items()}


def _state(path: Path, require_root: bool) -> dict[str, str] | None:
    text = _read_trusted(path, require_root)
    if text is None:
        return None
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def upgrade_notice(
    program: Path, *, windows: bool | None = None, require_root: bool = True
) -> UpgradeNotice | None:
    """The one upgrade fact worth telling the owner, most important first."""
    folder = upgrade_dir(program, windows=windows)
    rolled = _json(folder / ROLLED_BACK_NAME, require_root)
    if rolled is not None:
        to = rolled.get("to", "?")
        frm = rolled.get("from", "?")
        why = rolled.get("reason", "no reason recorded")
        return UpgradeNotice(
            "rolled_back",
            f"running the previous version {frm}: the upgrade to {to} failed "
            f"({why}); the new version is installed but not used - install it "
            "again after fixing the cause (see troubleshooting: rollback)",
            rolled,
        )
    interrupted = _json(folder / INTERRUPTED_NAME, require_root)
    if interrupted is not None:
        action = interrupted.get("action", "")
        to = interrupted.get("to", "?")
        frm = interrupted.get("from", "?")
        if action == "reverted":
            text = (
                f"an upgrade to {to} was interrupted (power loss or a killed "
                f"installer); the previous version {frm} runs - finish it with "
                "`sudo dpkg --configure -a` (Linux) or run the installer again"
            )
        else:
            text = (
                f"an upgrade to {to} was interrupted after it was switched on; "
                f"version {to} runs but its final check did not happen - finish "
                "it with `sudo dpkg --configure -a` (Linux) or run the installer again"
            )
        return UpgradeNotice("interrupted", text, interrupted)
    state = _state(folder / STATE_NAME, require_root)
    if state is not None:
        return UpgradeNotice(
            "in_progress",
            f"an upgrade from {state.get('from', '?')} to {state.get('to', '?')} "
            f"is in progress (phase {state.get('phase', '?')})",
            state,
        )
    return None
