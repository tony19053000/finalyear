"""The Windows installer's upgrade records (V0.17 G5, D393/D403, Q38/Q39).

The same records as the Linux packages (:mod:`meshforge.common.upgrade_state`),
in ``C:\\Program Files\\MeshForge\\upgrade\\`` - a folder only Administrators
and SYSTEM can write (the service's virtual account and its jobs cannot), and
which the uninstaller removes with the rest of ``{app}``:

* ``state`` - the setup run in progress: ``from`` (the interpreter the
  service ran before, if any), ``to`` (this version folder), ``phase``
  (``switching`` until the service is re-pointed, then ``checking``). Written
  before anything changes, removed when setup ends in a coherent state. A
  ``state`` file found at the start of setup means the last run was cut off
  (power loss, a killed installer): this run **finishes** it (the same steps,
  idempotent) and, if the new version is not healthy, **reverts** to the
  interpreter the record names - even when the service already points at the
  new one (that is exactly what an interrupted switch leaves);
* ``rolled-back.json`` - the last upgrade was rolled back (cleared by the
  next good one);
* ``fail-upgrade-to`` - the Q38 forced-failure trigger (one line: a version,
  a version folder like ``0.17.1-1``, or ``*``), removed after it was used.

Plain ``pathlib`` code: it runs (and is tested) on any OS; only the folder's
ACL is Windows-specific and comes from ``Program Files`` itself.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from meshforge.common.logging import get_logger
from meshforge.common.upgrade_state import (
    FORCED_FAILURE_NAME,
    ROLLED_BACK_NAME,
    STATE_NAME,
    WINDOWS_UPGRADE_DIR_NAME,
)

__all__ = [
    "FileRecords",
    "Journal",
    "NullRecords",
    "UpgradeRecords",
    "plain_version",
]

log = get_logger(__name__)

_MAX: Final = 64 * 1024


@dataclass(frozen=True, slots=True)
class Journal:
    """One setup run (see the module docstring)."""

    from_python: str | None
    to_version_dir: str
    phase: str


class UpgradeRecords(Protocol):
    def read_journal(self) -> Journal | None: ...
    def write_journal(self, journal: Journal) -> None: ...
    def clear_journal(self) -> None: ...
    def forced_failure(self, version_dir: str) -> str | None: ...
    def consume_forced_failure(self) -> None: ...
    def write_rolled_back(self, *, from_dir: str, to_dir: str, reason: str) -> None: ...
    def clear_rolled_back(self) -> None: ...


def plain_version(version_dir: str) -> str:
    """``0.17.1-2`` (version + installer revision) -> ``0.17.1``."""
    head, sep, tail = version_dir.rpartition("-")
    return head if sep and tail.isdigit() else version_dir


class NullRecords:
    """No records (tests that predate G5, a developer layout)."""

    def read_journal(self) -> Journal | None:
        return None

    def write_journal(self, journal: Journal) -> None:
        return None

    def clear_journal(self) -> None:
        return None

    def forced_failure(self, version_dir: str) -> str | None:
        return None

    def consume_forced_failure(self) -> None:
        return None

    def write_rolled_back(self, *, from_dir: str, to_dir: str, reason: str) -> None:
        return None

    def clear_rolled_back(self) -> None:
        return None


class FileRecords:
    """The records as files in ``<program root>\\upgrade``."""

    def __init__(self, program_root: Path) -> None:
        self.folder = program_root / WINDOWS_UPGRADE_DIR_NAME

    def _write(self, name: str, text: str) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.folder / f".{name}.{os.getpid()}"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.folder / name)

    def _read(self, name: str) -> str | None:
        path = self.folder / name
        try:
            if path.is_symlink() or not path.is_file():
                return None
            with path.open("rb") as handle:
                return handle.read(_MAX).decode("utf-8", "replace")
        except OSError:
            return None

    def read_journal(self) -> Journal | None:
        text = self._read(STATE_NAME)
        if text is None:
            return None
        facts: dict[str, str] = {}
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                facts[key.strip()] = value.strip()
        to = facts.get("to", "")
        if not to:
            return None
        # The interpreter is checked by winsetup (inside {app}, exists) before
        # anything is re-pointed to it.
        return Journal(facts.get("from") or None, to, facts.get("phase", ""))

    def write_journal(self, journal: Journal) -> None:
        self._write(
            STATE_NAME,
            f"from={journal.from_python or ''}\nto={journal.to_version_dir}\n"
            f"phase={journal.phase}\nat={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
        )

    def clear_journal(self) -> None:
        (self.folder / STATE_NAME).unlink(missing_ok=True)

    def forced_failure(self, version_dir: str) -> str | None:
        text = self._read(FORCED_FAILURE_NAME)
        if text is None:
            return None
        # the literal first line, the same rule as the Linux scripts (G5-T3)
        wanted = (text.splitlines() or [""])[0].strip(" \t\r")
        if wanted not in ("*", version_dir, plain_version(version_dir)):
            return None
        return (
            f"forced upgrade failure: {self.folder / FORCED_FAILURE_NAME} names "
            f"{wanted!r} (a test/lab trigger; it is removed after this run)"
        )

    def consume_forced_failure(self) -> None:
        (self.folder / FORCED_FAILURE_NAME).unlink(missing_ok=True)

    def write_rolled_back(self, *, from_dir: str, to_dir: str, reason: str) -> None:
        record = {
            "package": "meshforge-agent",
            "action": "rolled_back",
            "from": from_dir,
            "to": to_dir,
            "reason": reason[:500],
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._write(ROLLED_BACK_NAME, json.dumps(record) + "\n")

    def clear_rolled_back(self) -> None:
        (self.folder / ROLLED_BACK_NAME).unlink(missing_ok=True)
