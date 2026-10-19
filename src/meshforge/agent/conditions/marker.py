"""The persisted "applied" marker of benchmark conditions (V0.9 G4, D217/D241).

A condition changes the machine outside the attempt (load processes, a
``tc`` qdisc). If the agent dies between apply and revert, nothing in memory
remembers it - so **before** a change is made it is written to
``<data dir>/conditions/active.json`` (atomic replace), and removed only after
the revert succeeded. The next agent start reads the file and undoes whatever
it lists (:meth:`~.runner.ConditionRunner.sweep`):

* a load process is killed only if a process with that pid **and that
  creation time** still exists and is one of our burners (its command line
  holds :data:`BURNER_MODULE`) - a pid reused by another program is never
  touched;
* a netem qdisc is deleted from the recorded interface only when the entry
  says ``netem_installed`` (written right before ``tc replace``) and
  ``tc qdisc show`` lists a netem root there **with the expected parameters
  the entry stores** (``netem_expect``, G5 item 0.3) - never a foreign netem
  the pre-check refused, never one with other parameters, never a non-netem
  root qdisc.

The file is small (one entry per running attempt with a condition), owned by
the agent, never holds a secret. An unreadable / corrupt file is moved aside
and logged - a sweep can then miss a survivor, but a burner still exits by
itself at its deadline and when the agent is gone (:mod:`.burn`).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from meshforge.common.logging import get_logger

__all__ = ["BURNER_MODULE", "MARKER_NAME", "ConditionMarker"]

log = get_logger(__name__)

MARKER_NAME: Final = "active.json"
BURNER_MODULE: Final = "meshforge.agent.conditions.burn"
_MAX_BYTES: Final = 1024 * 1024


class ConditionMarker:
    """``attempt_id -> entry`` persisted as JSON (see module docstring).
    Thread-safe; every write replaces the whole file atomically."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / MARKER_NAME
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        if len(data) > _MAX_BYTES:
            raise ValueError("the condition marker is too large")
        body = json.loads(data.decode("utf-8"))
        if not isinstance(body, dict) or not all(
            isinstance(k, str) and isinstance(v, dict) for k, v in body.items()
        ):
            raise ValueError("the condition marker is not an object of objects")
        return body

    def _write(self, body: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not body:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            return
        tmp = self.path.with_name(f".{MARKER_NAME}.{secrets.token_hex(4)}.tmp")
        tmp.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def load(self) -> dict[str, dict[str, Any]]:
        """Every recorded entry; a corrupt file is moved aside (logged) and
        read as empty."""
        with self._lock:
            try:
                return self._read()
            except (OSError, ValueError, UnicodeDecodeError) as exc:
                aside = self.path.with_name(
                    f"{MARKER_NAME}.corrupt-{secrets.token_hex(4)}"
                )
                with contextlib.suppress(OSError):
                    os.replace(self.path, aside)
                log.warning(
                    "agent.conditions.marker_unreadable",
                    cause=type(exc).__name__,
                    moved_to=aside.name,
                )
                return {}

    def put(self, attempt_id: str, entry: Mapping[str, Any]) -> None:
        """Record (or replace) ``attempt_id``'s entry - before the change."""
        with self._lock:
            try:
                body = self._read()
            except (OSError, ValueError, UnicodeDecodeError):
                body = {}
            body[attempt_id] = dict(entry)
            self._write(body)

    def remove(self, attempt_id: str) -> None:
        """Forget ``attempt_id`` - after its revert succeeded."""
        with self._lock:
            try:
                body = self._read()
            except (OSError, ValueError, UnicodeDecodeError):
                body = {}
            if body.pop(attempt_id, None) is not None or not body:
                self._write(body)

    def clear(self) -> None:
        with self._lock:
            self._write({})
