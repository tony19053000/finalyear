"""The fabric token: its format, its default location and a safe reader (D44).

ADR-0014 (Proposed): until V0.7's mTLS and user identity, every mutating REST call,
every ``/v1/jobs*`` read and every ``/v1/blobs*`` call must carry
``Authorization: Bearer <fabric token>``. The **controller** creates and rotates the
token (``meshforge.controller.fabric_token``); the **CLI** reads it. Both need the
format and the default path, and neither may import the other - so those live here.

* Format: ``secrets.token_urlsafe(32)`` - 43 URL-safe base64 characters (256 bits).
  A file may hold any URL-safe token of :data:`MIN_TOKEN_CHARS` to
  :data:`MAX_TOKEN_CHARS` characters (an operator can paste one in), plus an
  optional trailing newline.
* Default path: ``<data_dir>/controller/fabric.token`` (ARCHITECTURE §23). On the
  controller's laptop the CLI falls back to it, so ``compute`` works there with no
  configuration; every other laptop sets ``[cli] token`` / ``MESHFORGE_CLI__TOKEN``
  / ``--token-file``.

The token is a :class:`~pydantic.SecretStr` everywhere after it is read: masked in
``repr`` and redacted by the logging pipeline. It never appears in a URL, a log
line or an error message.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from meshforge.common.api_token import API_TOKEN_PREFIX
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError

__all__ = [
    "CONTROLLER_DIR_NAME",
    "MAX_TOKEN_CHARS",
    "MAX_TOKEN_FILE_BYTES",
    "MIN_TOKEN_CHARS",
    "TOKEN_FILE_NAME",
    "FabricTokenError",
    "default_token_path",
    "generate_token",
    "parse_token",
    "read_token_file",
]

CONTROLLER_DIR_NAME: Final = "controller"
TOKEN_FILE_NAME: Final = "fabric.token"  # noqa: S105 - a file name, not a secret
MIN_TOKEN_CHARS: Final = 32
MAX_TOKEN_CHARS: Final = 128
MAX_TOKEN_FILE_BYTES: Final = 1024
_TOKEN_RE: Final = re.compile(r"[A-Za-z0-9_-]+")


class FabricTokenError(MeshForgeError):
    """The token file is missing, unreadable, malformed or not private."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR


def default_token_path(data_dir: Path) -> Path:
    """``<data_dir>/controller/fabric.token``."""
    return data_dir / CONTROLLER_DIR_NAME / TOKEN_FILE_NAME


def generate_token() -> SecretStr:
    """A fresh 256-bit token (43 URL-safe characters)."""
    return SecretStr(secrets.token_urlsafe(32))


def parse_token(text: str) -> SecretStr:
    """Validate a token's shape. The message never contains the value."""
    value = text.strip()
    if not MIN_TOKEN_CHARS <= len(value) <= MAX_TOKEN_CHARS or not _TOKEN_RE.fullmatch(
        value
    ):
        if value.startswith(API_TOKEN_PREFIX):
            # V0.7 G4 (D157): a damaged API token (`compute --token-file`) gets
            # the API token's wording, not the fabric token's.
            raise FabricTokenError(
                f"this looks like a damaged API token: an API token is "
                f"'{API_TOKEN_PREFIX}' followed by 43 URL-safe characters "
                "(A-Z a-z 0-9 _ -); copy it again, or run `compute login` for a "
                "new one"
            )
        raise FabricTokenError(
            f"a fabric token is {MIN_TOKEN_CHARS}-{MAX_TOKEN_CHARS} URL-safe "
            "characters (A-Z a-z 0-9 _ -); run `meshforge-controller token show` "
            "on the controller laptop to see the real one"
        )
    return SecretStr(value)


def read_token_file(path: Path, *, require_private: bool = False) -> SecretStr:
    """Read and validate a token file (bounded size).

    Always a regular file (no symlink, FIFO or device). ``require_private``
    additionally refuses a POSIX file that other users can read or that another
    user owns (the controller insists on it for its own file; a CLI reading a copy
    does not).
    """
    from meshforge.common.private_files import (
        PrivateFileError,
        PrivateFileReason,
        read_bounded,
        stat_problem,
    )

    details = {"path": str(path)}
    try:
        # D149a: a regular file only (a FIFO would hang the read, a symlink could
        # point anywhere), bounded; the controller also wants it owner-only.
        raw, info = read_bounded(
            path,
            limit=MAX_TOKEN_FILE_BYTES,
            what="token file",
            follow_symlinks=not require_private,
        )
    except PrivateFileError as exc:
        if exc.reason is PrivateFileReason.MISSING:
            raise FabricTokenError(
                f"token file not found: {path}", details=details
            ) from None
        raise FabricTokenError(exc.message, details=details) from None
    if require_private:
        problem = stat_problem(info)
        if problem is not None:
            raise FabricTokenError(f"token file {path} is {problem}", details=details)
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise FabricTokenError(
            f"token file {path} is not ASCII text", details={"path": str(path)}
        ) from None
    try:
        return parse_token(text)
    except FabricTokenError as exc:
        raise FabricTokenError(
            f"token file {path}: {exc.message}", details={"path": str(path)}
        ) from None
