"""Who may use the local console (D391): the token file, launch codes, sessions.

* **Token** - 32 random bytes (URL-safe), new at every start of the agent, written
  to ``<token_dir>/token``. On the Linux system service the folder is
  ``/run/meshforge-console`` (``2750 meshforge:meshforge-admin``, made by
  systemd-tmpfiles), so the file is ``0640`` and readable by the service user and
  the ``meshforge-admin`` group only; in a folder without the set-group-id bit
  (a developer's ``<data_dir>/console``) it is ``0600``. Being able to read it
  *is* the permission: there is no password.
* **Launch code** - the launcher trades the token (``Authorization: Bearer``) for
  a single-use code valid ``launch_code_ttl_s`` and opens the browser on
  ``#c=<code>``; only that short-lived code ever appears in a process's command
  line (``xdg-open``'s argv is visible to other local users), never the token.
* **Session** - the page posts the code once (never the token) and
  gets an ``HttpOnly; SameSite=Strict`` cookie naming a random server-side
  session (memory only: a restart signs everyone out, like the token).
* **Host / Origin** - every request must say ``Host: 127.0.0.1:<port>`` (a DNS
  rebinding page would send its own name) and every cookie-authenticated write
  ``Origin: http://127.0.0.1:<port>`` (CSRF). Pure functions, tested alone.

Comparisons are constant-time (``hmac.compare_digest``); nothing here logs a
token, a code or a session id.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import secrets
import stat
import sys
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.private_files import protect_private

__all__ = [
    "COOKIE_NAME",
    "MAX_FAILURES_PER_MINUTE",
    "TOKEN_FILE_NAME",
    "ConsoleAuth",
    "expected_host",
    "expected_origin",
    "host_ok",
    "origin_ok",
    "write_token_file",
]

TOKEN_FILE_NAME: Final = "token"  # noqa: S105 - a file name
COOKIE_NAME: Final = "mf_console"
MAX_SESSIONS: Final = 16
MAX_LAUNCH_CODES: Final = 8
MAX_FAILURES_PER_MINUTE: Final = 30
"""Wrong tokens/codes per minute after which a FAILED try answers 429 instead
of 401 (a 256-bit token cannot be guessed; this only keeps a local loop quiet).
The credential is always checked first, so the right one is never refused
(G3-D1: a stranger's wrong tries must not lock the owner out)."""
_TOKEN_BYTES: Final = 32


def expected_host(port: int) -> str:
    return f"127.0.0.1:{port}"


def expected_origin(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def host_ok(host: str | None, port: int) -> bool:
    """``Host`` is exactly ``127.0.0.1:<port>`` (case/space-insensitive)."""
    return host is not None and host.strip().lower() == expected_host(port)


def origin_ok(origin: str | None, port: int) -> bool:
    return origin is not None and origin.strip().lower() == expected_origin(port)


def write_token_file(directory: Path, token: str) -> Path:
    """Write ``token`` to ``directory/token`` (module docstring for the mode).

    A missing folder is created ``0700`` (developer mode); an existing one is
    never loosened. Raises :class:`OSError` when it cannot be written."""
    if not directory.is_dir():
        directory.mkdir(parents=True, mode=0o700)
    path = directory / TOKEN_FILE_NAME
    shared = os.name == "posix" and bool(directory.stat().st_mode & stat.S_ISGID)
    mode = 0o640 if shared else 0o600
    atomic_write_bytes(path, (token + "\n").encode("ascii"), mode=mode)
    if os.name == "posix":
        os.chmod(path, mode)  # the service's UMask=0077 would have taken g+r away
    elif sys.platform == "win32":  # pragma: no cover - G4 sets the folder's ACL
        protect_private(path)
    return path


@dataclass(slots=True)
class _Failures:
    window_start: float = 0.0
    count: int = 0


class ConsoleAuth:
    """The console's credentials, in memory (one event loop; no locking needed)."""

    def __init__(
        self,
        *,
        port: int,
        session_ttl_s: float,
        launch_code_ttl_s: float,
        token: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.port = port
        self.token = token or secrets.token_urlsafe(_TOKEN_BYTES)
        self._session_ttl_s = session_ttl_s
        self._launch_ttl_s = launch_code_ttl_s
        self._clock = clock
        self._sessions: OrderedDict[str, float] = OrderedDict()
        self._codes: OrderedDict[str, float] = OrderedDict()
        self._failures = _Failures()
        self.token_path: Path | None = None

    # ------------------------------------------------------------- token file
    def publish(self, directory: Path) -> Path:
        """Write the token file (raises :class:`OSError`)."""
        self.token_path = write_token_file(directory, self.token)
        return self.token_path

    def unpublish(self) -> None:
        """Remove the token file (best effort, at shutdown)."""
        if self.token_path is not None:
            with contextlib.suppress(OSError):
                self.token_path.unlink()

    # ------------------------------------------------------------ throttling
    def throttled(self) -> bool:
        now = self._clock()
        if now - self._failures.window_start >= 60.0:
            self._failures = _Failures(now, 0)
        return self._failures.count >= MAX_FAILURES_PER_MINUTE

    def _failed(self) -> None:
        self._failures.count += 1

    # ----------------------------------------------------------------- checks
    def token_matches(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        ok = hmac.compare_digest(candidate.encode(), self.token.encode())
        if not ok:
            self._failed()
        return ok

    def new_launch_code(self) -> str:
        self._expire()
        code = secrets.token_urlsafe(24)
        self._codes[code] = self._clock() + self._launch_ttl_s
        while len(self._codes) > MAX_LAUNCH_CODES:
            self._codes.popitem(last=False)
        return code

    def redeem_launch_code(self, candidate: str | None) -> bool:
        """True once per valid, unexpired code (it is spent either way)."""
        self._expire()
        if not candidate:
            return False
        for code in list(self._codes):
            if hmac.compare_digest(code.encode(), candidate.encode()):
                del self._codes[code]
                return True
        self._failed()
        return False

    def new_session(self) -> str:
        self._expire()
        session = secrets.token_urlsafe(32)
        self._sessions[session] = self._clock() + self._session_ttl_s
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.popitem(last=False)  # the oldest browser signs out
        return session

    def session_ok(self, candidate: str | None) -> bool:
        if not candidate:
            return False
        self._expire()
        return any(
            hmac.compare_digest(s.encode(), candidate.encode()) for s in self._sessions
        )

    def end_session(self, candidate: str | None) -> None:
        if not candidate:
            return
        for session in list(self._sessions):
            if hmac.compare_digest(session.encode(), candidate.encode()):
                del self._sessions[session]

    def _expire(self) -> None:
        now = self._clock()
        for table in (self._sessions, self._codes):
            for key in [k for k, until in table.items() if until <= now]:
                del table[key]
