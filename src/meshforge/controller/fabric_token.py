"""The controller's fabric token: create, load, rotate, verify (D44, ADR-0014).

``fabric.token`` lives in ``<data_dir>/controller/`` (ARCHITECTURE §23; override:
``[controller] token_file``). Rules - the same as the node identity (ADR-0013):

* **Create once, atomically.** The first ``meshforge-controller`` start (or
  ``token show``) writes a fresh 256-bit token with :func:`atomic_write_bytes`
  (``0600`` from creation, exclusive), under ``fabric.token.lock`` so two
  processes racing on an empty directory end with one token. The folder is
  ``0700``; Windows gets an owner-only ACL (:mod:`meshforge.common.private_files`).
* **Load strictly.** A file readable by other users (POSIX), a malformed token or
  an oversized file is a :class:`~meshforge.common.fabric_token.FabricTokenError`
  (``config_error``): the controller refuses to start rather than serve with a
  token it cannot trust.
* **Rotate** replaces the file atomically. A running controller picks the new
  token up within :data:`DEFAULT_RELOAD_INTERVAL_S` (the verifier re-reads the
  file when its size/mtime/inode changed) - no restart, and the old token stops
  working at once after that.
* **Verify** in constant time (:func:`hmac.compare_digest`). If the file becomes
  unreadable while running, verification **fails closed** (every protected call
  is refused) and ``controller.token.unavailable`` is logged.

The token is never logged; only its SHA-256 *fingerprint* prefix is, so two log
lines can be compared ("did it rotate?") without revealing it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.fabric_token import (
    FabricTokenError,
    generate_token,
    read_token_file,
)
from meshforge.common.filelock import locked
from meshforge.common.logging import get_logger
from meshforge.common.private_files import PrivateFileError, protect_private

__all__ = [
    "DEFAULT_RELOAD_INTERVAL_S",
    "FabricTokenVerifier",
    "fingerprint",
    "load_or_create_token",
    "rotate_token",
]

log = get_logger(__name__)

DEFAULT_RELOAD_INTERVAL_S: Final = 1.0
_LOCK_TIMEOUT_S: Final = 10.0
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600


def fingerprint(token: SecretStr) -> str:
    """First 12 hex characters of SHA-256(token): loggable, not reversible."""
    return hashlib.sha256(token.get_secret_value().encode()).hexdigest()[:12]


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _ensure_dir(directory: Path) -> None:
    try:
        existed = directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        if not existed:
            protect_private(directory)
    except OSError as exc:
        raise FabricTokenError(
            f"cannot create the token folder {directory}: {exc.strerror or exc}",
            details={"path": str(directory)},
        ) from None
    except PrivateFileError as exc:
        raise FabricTokenError(exc.message, details=exc.details) from None


def _write(path: Path, token: SecretStr, *, exclusive: bool) -> None:
    data = (token.get_secret_value() + "\n").encode("ascii")
    try:
        atomic_write_bytes(path, data, mode=_FILE_MODE, exclusive=exclusive)
        protect_private(path)
    except OSError as exc:
        raise FabricTokenError(
            f"cannot write token file {path}: {exc.strerror or exc}",
            details={"path": str(path)},
        ) from None
    except PrivateFileError as exc:
        raise FabricTokenError(exc.message, details=exc.details) from None


def load_or_create_token(path: Path) -> tuple[SecretStr, bool]:
    """Return ``(token, created)``; create the file when it does not exist."""
    _ensure_dir(path.parent)
    try:
        with locked(_lock_path(path), timeout_s=_LOCK_TIMEOUT_S, mode=_FILE_MODE):
            if not path.exists():
                token = generate_token()
                _write(path, token, exclusive=True)
                log.info("controller.token.created", fingerprint=fingerprint(token))
                return token, True
            token = read_token_file(path, require_private=True)
    except TimeoutError:
        raise FabricTokenError(
            "the token file is locked by another process; retry",
            details={"path": str(path)},
        ) from None
    return token, False


def rotate_token(path: Path) -> SecretStr:
    """Replace the token with a fresh one (atomic); return it."""
    _ensure_dir(path.parent)
    try:
        with locked(_lock_path(path), timeout_s=_LOCK_TIMEOUT_S, mode=_FILE_MODE):
            token = generate_token()
            _write(path, token, exclusive=False)
    except TimeoutError:
        raise FabricTokenError(
            "the token file is locked by another process; retry",
            details={"path": str(path)},
        ) from None
    log.info("controller.token.rotated", fingerprint=fingerprint(token))
    return token


@dataclass(frozen=True, slots=True)
class _Loaded:
    token: SecretStr | None
    signature: tuple[int, int, int, int] | None


class FabricTokenVerifier:
    """Checks ``Authorization: Bearer`` values against the token file.

    Thread-safe (FastAPI may run a dependency in a worker thread). ``clock`` is
    monotonic (``loop.time`` or ``time.monotonic``); the file is re-stat'ed at most
    once per ``reload_interval_s``.
    """

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float],
        reload_interval_s: float = DEFAULT_RELOAD_INTERVAL_S,
        initial: SecretStr | None = None,
    ) -> None:
        self._path = path
        self._clock = clock
        self._interval = reload_interval_s
        self._lock = threading.Lock()
        self._loaded = (
            _Loaded(initial, self._signature()) if initial else _Loaded(None, None)
        )
        self._checked_at: float | None = clock() if initial else None

    def _signature(self) -> tuple[int, int, int, int] | None:
        """What must change for the file to be re-read (mode: a chmod 644 counts)."""
        try:
            st = os.stat(self._path)
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode)

    def _current(self) -> SecretStr | None:
        now = self._clock()
        with self._lock:
            if self._checked_at is not None and now - self._checked_at < self._interval:
                return self._loaded.token
            self._checked_at = now
            signature = self._signature()
            if signature is not None and signature == self._loaded.signature:
                return self._loaded.token
            try:
                token = read_token_file(self._path, require_private=True)
            except FabricTokenError as exc:
                if self._loaded.token is not None or self._loaded.signature is None:
                    log.error(  # noqa: TRY400 - the reason is typed; no traceback
                        "controller.token.unavailable",
                        failure_type=exc.failure_type.value,
                        reason=type(exc).__name__,
                    )
                self._loaded = _Loaded(None, signature)
                return None
            if self._loaded.token is not None and not hmac.compare_digest(
                token.get_secret_value(), self._loaded.token.get_secret_value()
            ):
                log.info("controller.token.reloaded", fingerprint=fingerprint(token))
            self._loaded = _Loaded(token, signature)
            return token

    def verify(self, presented: str | None) -> bool:
        """True only for the current token. ``None``/empty is always False."""
        if not presented:
            return False
        current = self._current()
        if current is None:
            return False  # fail closed
        return hmac.compare_digest(
            presented.encode("utf-8", "surrogateescape"),
            current.get_secret_value().encode("ascii"),
        )
