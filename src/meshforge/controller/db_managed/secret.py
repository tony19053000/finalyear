"""The generated database password: created once, owner-only, never logged.

The file holds the password and nothing else (one line) because ``initdb
--pwfile`` reads exactly that - so the password never appears on a command line
(``/proc/<pid>/cmdline`` is world-readable). Clients that are separate programs
(``pg_dump``/``pg_restore``) get it through a short-lived ``PGPASSFILE``
(:func:`pgpass_line`), never ``PGPASSWORD`` and never argv.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.private_files import (
    PrivateFileError,
    PrivateFileReason,
    protect_private,
    read_private,
)

__all__ = [
    "PASSWORD_BYTES",
    "SCRAM_ITERATIONS",
    "ensure_password_file",
    "generate_password",
    "pgpass_line",
    "read_password_file",
    "scram_sha256_verifier",
]

PASSWORD_BYTES: Final = 32
"""Entropy of the generated password (256 bits, 43 URL-safe characters)."""
_MAX_FILE_BYTES: Final = 1024
_WHAT: Final = "database password file"


def generate_password() -> SecretStr:
    """A fresh random password from the URL-safe alphabet (no ``:`` or ``\\``,
    so it needs no escaping in a pgpass line or a URL)."""
    return SecretStr(secrets.token_urlsafe(PASSWORD_BYTES))


def ensure_password_file(path: Path) -> tuple[SecretStr, bool]:
    """Read the password at ``path``, creating it (``0600``) if missing.

    Returns ``(password, created)``. An existing file must be private to the
    current user (POSIX mode/owner checks of
    :func:`~meshforge.common.private_files.read_private`), otherwise
    :class:`~meshforge.common.private_files.PrivateFileError` (``config_error``)
    is raised - a password other users can read is refused, not silently used.
    """
    created = False
    if not path.exists():
        password = generate_password()
        try:
            atomic_write_bytes(
                path,
                (password.get_secret_value() + "\n").encode("ascii"),
                mode=0o600,
                exclusive=True,
            )
        except FileExistsError:
            pass  # lost a race with another creator: read theirs below
        else:
            protect_private(path)
            created = True
    return read_password_file(path), created


def read_password_file(path: Path) -> SecretStr:
    """The password in ``path`` (first line, stripped); refuses a non-private file."""
    raw = read_private(path, limit=_MAX_FILE_BYTES, what=_WHAT)
    try:
        first = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        first = []
    value = first[0].strip() if first else ""
    if not value:
        raise PrivateFileError(
            f"the {_WHAT} {path} is empty or not text",
            details={"path": str(path)},
            reason=PrivateFileReason.UNREADABLE,
        )
    return SecretStr(value)


SCRAM_ITERATIONS: Final = 4096
"""PostgreSQL's default ``scram_iterations``."""


def scram_sha256_verifier(
    password: SecretStr,
    *,
    salt: bytes | None = None,
    iterations: int = SCRAM_ITERATIONS,
) -> str:
    """The ``SCRAM-SHA-256$...`` verifier PostgreSQL stores for ``password``.

    ``ALTER ROLE ... PASSWORD '<verifier>'`` sets a password without the
    plaintext ever reaching the server (or its log, should the statement be
    logged). RFC 5802/7677 as PostgreSQL implements it; our generated passwords
    are ASCII, so SASLprep leaves them unchanged.
    """
    salt = os.urandom(16) if salt is None else salt
    salted = hashlib.pbkdf2_hmac(
        "sha256", password.get_secret_value().encode("utf-8"), salt, iterations
    )
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def pgpass_line(user: str, password: SecretStr) -> str:
    """One ``.pgpass`` line matching any host/port/database for ``user``.

    ``:`` and ``\\`` are escaped as libpq requires.
    """

    def esc(value: str) -> str:
        return value.replace("\\", "\\\\").replace(":", "\\:")

    return f"*:*:*:{esc(user)}:{esc(password.get_secret_value())}\n"
