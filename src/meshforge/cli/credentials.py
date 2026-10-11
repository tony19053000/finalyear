"""What ``compute login`` remembers (V0.7 G4, D141): one API token and the
controller's CA certificate, in private files of the CLI's config dir.

``<credentials dir>`` (``[cli] credentials_dir``, default ``<config dir>/compute``)
holds:

* ``login.json`` - ``{controller_url, token, token_id, user, role, expires_at}``,
  mode ``0600`` (an owner-only ACL on Windows), written atomically; read back
  only if it is still private (another user able to read it = refused, typed);
* ``ca.crt`` - the fabric CA the login verified the controller with; later
  commands trust exactly it (unless ``--ca`` / ``[cli] ca_file`` say otherwise).

The token never appears in argv, a URL, a log line or an error message; it is a
:class:`~pydantic.SecretStr` from the moment it is read.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from cryptography import x509
from pydantic import SecretStr

from meshforge.common import pki
from meshforge.common.api_token import is_api_token
from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.config import default_config_dir
from meshforge.common.errors import ConfigError
from meshforge.common.private_files import (
    PrivateFileError,
    protect_private,
    read_private,
)

__all__ = [
    "CA_FILE_NAME",
    "LOGIN_FILE_NAME",
    "StoredLogin",
    "credentials_dir",
    "forget_login",
    "load_login",
    "save_login",
]

LOGIN_FILE_NAME: Final = "login.json"
CA_FILE_NAME: Final = "ca.crt"
_MAX_LOGIN_BYTES: Final = 16 * 1024
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600


@dataclass(frozen=True, slots=True)
class StoredLogin:
    controller_url: str
    token: SecretStr
    token_id: str | None
    user: str
    role: str
    expires_at: str | None
    ca_file: Path | None
    """``ca.crt`` beside the login (None: an insecure-dev, plain-HTTP login)."""

    def as_public_dict(self) -> dict[str, Any]:
        """Everything but the token (for ``whoami --json`` / messages)."""
        return {
            "controller_url": self.controller_url,
            "token_id": self.token_id,
            "user": self.user,
            "role": self.role,
            "expires_at": self.expires_at,
        }


def credentials_dir(configured: Path | None) -> Path:
    return configured if configured is not None else default_config_dir() / "compute"


def _ensure_dir(directory: Path) -> None:
    try:
        existed = directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        if not existed:
            protect_private(directory)
    except OSError as exc:
        raise ConfigError(
            f"cannot create the credentials folder {directory}: "
            f"{exc.strerror or type(exc).__name__}",
            details={"path": str(directory)},
        ) from None
    except PrivateFileError as exc:
        raise ConfigError(exc.message, details=exc.details) from None


def _write(path: Path, data: bytes) -> None:
    try:
        atomic_write_bytes(path, data, mode=_FILE_MODE)
        protect_private(path)
    except OSError as exc:
        raise ConfigError(
            f"cannot write {path}: {exc.strerror or type(exc).__name__}",
            details={"path": str(path)},
        ) from None
    except PrivateFileError as exc:
        raise ConfigError(exc.message, details=exc.details) from None


def save_login(
    directory: Path,
    *,
    controller_url: str,
    token: SecretStr,
    token_id: str | None,
    user: str,
    role: str,
    expires_at: str | None,
    ca_cert: x509.Certificate | None,
) -> StoredLogin:
    """Write ``ca.crt`` (if any) then ``login.json``, both private and atomic."""
    _ensure_dir(directory)
    ca_path = directory / CA_FILE_NAME
    if ca_cert is not None:
        _write(ca_path, pki.cert_to_pem(ca_cert))
    else:
        with contextlib.suppress(FileNotFoundError):
            ca_path.unlink()
    body = {
        "controller_url": controller_url,
        "token": token.get_secret_value(),
        "token_id": token_id,
        "user": user,
        "role": role,
        "expires_at": expires_at,
    }
    _write(directory / LOGIN_FILE_NAME, json.dumps(body, indent=2).encode("utf-8"))
    return StoredLogin(
        controller_url=controller_url,
        token=token,
        token_id=token_id,
        user=user,
        role=role,
        expires_at=expires_at,
        ca_file=ca_path if ca_cert is not None else None,
    )


def _text(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) and len(value) <= 512 else None


def load_login(directory: Path) -> StoredLogin | None:
    """The stored login, or ``None`` when there is none.

    A file that is not private, malformed or holds no valid token shape is a
    :class:`ConfigError` naming the fix (``compute logout`` / log in again),
    never silently ignored - the user expects to be logged in.
    """
    path = directory / LOGIN_FILE_NAME
    if not path.exists():
        return None
    try:
        raw = read_private(path, limit=_MAX_LOGIN_BYTES, what="login file")
    except PrivateFileError as exc:
        raise ConfigError(exc.message, details=exc.details) from None
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    token = _text(body, "token") if isinstance(body, dict) else None
    url = _text(body, "controller_url") if isinstance(body, dict) else None
    if not isinstance(body, dict) or not is_api_token(token) or not url:
        raise ConfigError(
            f"the login file {path} is damaged; run `compute logout` and "
            "`compute login` again",
            details={"path": str(path)},
        )
    assert token is not None  # noqa: S101 - narrowed by is_api_token
    ca_path = directory / CA_FILE_NAME
    return StoredLogin(
        controller_url=url,
        token=SecretStr(token),
        token_id=_text(body, "token_id"),
        user=_text(body, "user") or "?",
        role=_text(body, "role") or "?",
        expires_at=_text(body, "expires_at"),
        ca_file=ca_path if ca_path.exists() else None,
    )


def forget_login(directory: Path) -> bool:
    """Delete the stored login (and its ``ca.crt``); True when there was one."""
    found = False
    for name in (LOGIN_FILE_NAME, CA_FILE_NAME):
        with contextlib.suppress(FileNotFoundError):
            (directory / name).unlink()
            found = found or name == LOGIN_FILE_NAME
    return found
