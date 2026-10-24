"""The one-time setup token (V0.17 G2, Q20).

``<data_dir>/controller/setup-token``: 43 URL-safe characters (256 bits), one
line, ``0600`` owned by the service user. Made by ``meshforge-controller setup
url`` (the package's postinst runs it and prints the URL), checked in constant
time, deleted when the fabric has been created. It reaches the browser only in a
URL *fragment* (never sent to a server, never in a log) and is swapped there for
an HttpOnly session cookie.
"""

from __future__ import annotations

import contextlib
import hmac
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
    "TOKEN_BYTES",
    "ensure_setup_token",
    "read_setup_token",
    "remove_setup_token",
    "setup_token_matches",
]

TOKEN_BYTES: Final = 32
_MAX_FILE_BYTES: Final = 256
_MAX_PRESENTED_CHARS: Final = 128
_WHAT: Final = "setup token file"


def ensure_setup_token(path: Path) -> tuple[SecretStr, bool]:
    """The token at ``path``, created (``0600``) if missing: ``(token, created)``.
    A token file other users can read is refused (``config_error``)."""
    created = False
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        value = secrets.token_urlsafe(TOKEN_BYTES)
        try:
            atomic_write_bytes(
                path, (value + "\n").encode("ascii"), mode=0o600, exclusive=True
            )
        except FileExistsError:
            pass  # another creator won the race: read theirs
        else:
            protect_private(path)
            created = True
    token = read_setup_token(path)
    if token is None:
        raise PrivateFileError(
            f"the {_WHAT} {path} disappeared while it was being read",
            details={"path": str(path)},
            reason=PrivateFileReason.MISSING,
        )
    return token, created


def read_setup_token(path: Path) -> SecretStr | None:
    """The token, or None when there is no file (setup not offered / done)."""
    try:
        raw = read_private(path, limit=_MAX_FILE_BYTES, what=_WHAT)
    except PrivateFileError as exc:
        if exc.reason is PrivateFileReason.MISSING:
            return None
        raise
    lines = raw.decode("ascii", errors="replace").splitlines()
    value = lines[0].strip() if lines else ""
    if not value:
        raise PrivateFileError(
            f"the {_WHAT} {path} is empty",
            details={"path": str(path)},
            reason=PrivateFileReason.UNREADABLE,
        )
    return SecretStr(value)


def setup_token_matches(stored: SecretStr | None, presented: str) -> bool:
    """Constant-time comparison; False when there is no token or the presented
    value is absurdly long (refused before comparing)."""
    if stored is None or not presented or len(presented) > _MAX_PRESENTED_CHARS:
        return False
    return hmac.compare_digest(
        stored.get_secret_value().encode("utf-8"), presented.encode("utf-8")
    )


def remove_setup_token(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
