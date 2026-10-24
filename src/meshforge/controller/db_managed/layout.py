"""Where the managed cluster keeps its files, and the path rules PostgreSQL imposes.

One root (``/var/lib/meshforge/db`` for an install, D390) holds everything::

    <root>/
      managed.lock     advisory lock: one controller per cluster (never deleted)
      password         generated password, 0600 (secret.py)
      data/            PGDATA, 0700 - created by initdb in data.initdb-tmp/ and
                       renamed, so a crash during initdb never leaves a half cluster
      run/             unix socket dir, 0700 (may be moved, e.g. /run/meshforge/db)
      log/             postgres server logs (logging collector) + startup.log
      backups/         pg_dump files (D393 pre-upgrade backups)

The socket dir is configurable because a unix socket path is limited to 107 bytes
(``sun_path``) and a deep data root (or a test tmp dir) can exceed it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.controller.db_managed.errors import ManagedDbError, ManagedDbReason

__all__ = [
    "MAX_SOCKET_PATH_BYTES",
    "ManagedLayout",
    "check_socket_dir",
    "socket_path",
]

MAX_SOCKET_PATH_BYTES: Final = 107
"""``sizeof(sockaddr_un.sun_path) - 1`` on Linux; PostgreSQL refuses longer paths."""


@dataclass(frozen=True, slots=True)
class ManagedLayout:
    """Paths of one managed cluster (see the module docstring)."""

    root: Path
    socket_dir_override: Path | None = None

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def initdb_tmp_dir(self) -> Path:
        return self.root / "data.initdb-tmp"

    @property
    def password_file(self) -> Path:
        return self.root / "password"

    @property
    def lock_file(self) -> Path:
        return self.root / "managed.lock"

    @property
    def socket_dir(self) -> Path:
        return self.socket_dir_override or self.root / "run"

    @property
    def log_dir(self) -> Path:
        return self.root / "log"

    @property
    def startup_log(self) -> Path:
        return self.log_dir / "startup.log"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    def prepare(self) -> None:
        """Create root, socket, log and backups dirs owner-only (0700).

        Existing dirs are tightened to 0700 on POSIX: PostgreSQL itself requires
        that of the data dir, and the socket dir is the access boundary of the
        unix socket.
        """
        for path in (self.root, self.socket_dir, self.log_dir, self.backups_dir):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if os.name == "posix":
                os.chmod(path, 0o700)


def socket_path(socket_dir: Path, port: int) -> str:
    """The socket file PostgreSQL creates: ``<dir>/.s.PGSQL.<port>``."""
    return f"{socket_dir}/.s.PGSQL.{port}"


def check_socket_dir(socket_dir: Path, port: int) -> None:
    """Refuse a socket dir PostgreSQL cannot use (typed ``path_invalid``).

    * the full socket path must fit in ``sun_path`` (107 bytes, UTF-8);
    * no ``,`` or ``"`` - ``unix_socket_directories`` is a comma-separated,
      quote-aware list, so either would silently change its meaning;
    * absolute - a relative path would resolve against the data dir.
    """
    text = str(socket_dir)
    details: dict[str, object] = {"socket_dir": text, "port": port}
    if not socket_dir.is_absolute():
        raise ManagedDbError(
            f"the database socket directory {text} must be an absolute path",
            reason=ManagedDbReason.PATH_INVALID,
            details=details,
        )
    if "," in text or '"' in text:
        raise ManagedDbError(
            f"the database socket directory {text} must not contain ',' or '\"'",
            reason=ManagedDbReason.PATH_INVALID,
            details=details,
        )
    length = len(socket_path(socket_dir, port).encode("utf-8"))
    if length > MAX_SOCKET_PATH_BYTES:
        raise ManagedDbError(
            f"the database socket path in {text} is {length} bytes, more than "
            f"the {MAX_SOCKET_PATH_BYTES} a unix socket allows; choose a shorter "
            "socket directory",
            reason=ManagedDbReason.PATH_INVALID,
            details={**details, "length": length},
        )
