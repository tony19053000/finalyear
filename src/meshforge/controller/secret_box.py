"""Job secrets at rest: the controller master key and AES-256-GCM (V0.7 G6, D144).

The master key is ``<data_dir>/controller/secrets.key``: 32 random bytes from
:mod:`secrets`, written once (atomically, ``0600`` from creation, under a lock
file, in a ``0700`` folder - the ``peer_signing.key`` rules, D149a) the first
time a secret is stored or read. It is **loaded strictly**: a symlink, FIFO or
device, a file other users can read or another user owns, or a file that is not
exactly 32 bytes is a :class:`SecretKeyError` (``config_error``) - the
controller refuses to encrypt or decrypt rather than use a key it cannot trust.

Each value is sealed with a fresh 96-bit nonce; the **associated data** binds the
ciphertext to its owner and name (``meshforge-secret-v1 | owner_user_id |
name``), so a row copied to another owner or renamed in the database fails to
decrypt (:class:`SecretIntegrityError`, ``integrity_error``) instead of handing
one user another user's value. ``key_id`` (16 hex chars of a SHA-256 over a
label and the key) says which master key sealed a row - the V0.17 master-key
rotation reads it; a row of another key fails typed (``SecretKeyMismatchError``).

**Losing ``secrets.key`` loses every stored secret** (they cannot be decrypted;
users set them again). Back it up with the CA (``docs/OPERATIONS.md``).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.fabric_token import CONTROLLER_DIR_NAME
from meshforge.common.filelock import locked
from meshforge.common.logging import get_logger
from meshforge.common.private_files import (
    PrivateFileError,
    PrivateFileReason,
    protect_private,
    read_private,
)

__all__ = [
    "KEY_BYTES",
    "KEY_FILE_NAME",
    "NONCE_BYTES",
    "SealedSecret",
    "SecretBox",
    "SecretIntegrityError",
    "SecretKeyError",
    "SecretKeyMismatchError",
    "default_key_path",
    "load_or_create_master_key",
]

log = get_logger(__name__)

KEY_FILE_NAME: Final = "secrets.key"
KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
_AAD_LABEL: Final = b"meshforge-secret-v1"
_KEY_ID_LABEL: Final = b"meshforge-secret-key-id-v1"
_LOCK_TIMEOUT_S: Final = 10.0
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600


class SecretKeyError(MeshForgeError):
    """The master key file is unreadable, malformed or not private."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR


class SecretIntegrityError(MeshForgeError):
    """A stored secret does not decrypt for its owner and name (tampered, copied
    to another owner/name, or truncated)."""

    default_failure_type = FailureType.INTEGRITY_ERROR


class SecretKeyMismatchError(SecretIntegrityError):
    """The row was sealed with another master key (``key_id`` differs)."""


def default_key_path(data_dir: Path) -> Path:
    """``<data_dir>/controller/secrets.key``."""
    return data_dir / CONTROLLER_DIR_NAME / KEY_FILE_NAME


def _ensure_dir(directory: Path) -> None:
    try:
        existed = directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        if not existed:
            protect_private(directory)
    except OSError as exc:
        raise SecretKeyError(
            f"cannot create the key folder {directory}: {exc.strerror or exc}",
            details={"path": str(directory)},
        ) from None
    except PrivateFileError as exc:
        raise SecretKeyError(exc.message, details=exc.details) from None


def _read(path: Path) -> bytes:
    try:
        raw = read_private(path, limit=KEY_BYTES, what="secrets master key")
    except PrivateFileError as exc:
        raise SecretKeyError(
            exc.message, details={"path": str(path), "reason": exc.reason.value}
        ) from None
    if len(raw) != KEY_BYTES:
        raise SecretKeyError(
            f"the secrets master key {path} is not {KEY_BYTES} bytes",
            details={"path": str(path), "reason": "malformed"},
        )
    return raw


def load_or_create_master_key(path: Path) -> tuple[bytes, bool]:
    """``(key, created)``; creates the file when it does not exist."""
    _ensure_dir(path.parent)
    try:
        with locked(
            path.with_name(path.name + ".lock"),
            timeout_s=_LOCK_TIMEOUT_S,
            mode=_FILE_MODE,
        ):
            try:
                return _read(path), False
            except SecretKeyError as exc:
                if exc.details.get("reason") != PrivateFileReason.MISSING.value:
                    raise
            key = secrets.token_bytes(KEY_BYTES)
            try:
                atomic_write_bytes(path, key, mode=_FILE_MODE, exclusive=True)
                protect_private(path)
            except OSError as exc:
                raise SecretKeyError(
                    f"cannot write the secrets master key {path}: "
                    f"{exc.strerror or exc}",
                    details={"path": str(path)},
                ) from None
            except PrivateFileError as exc:
                raise SecretKeyError(exc.message, details=exc.details) from None
    except TimeoutError:
        raise SecretKeyError(
            "the secrets master key is locked by another process; retry",
            details={"path": str(path)},
        ) from None
    log.info("controller.secrets.key_created", key_id=key_id_of(key))
    return key, True


def key_id_of(key: bytes) -> str:
    """16 hex chars naming a master key (never the key; loggable)."""
    return hashlib.sha256(_KEY_ID_LABEL + b"\0" + key).hexdigest()[:16]


def _aad(owner_user_id: str, name: str) -> bytes:
    return b"\0".join((_AAD_LABEL, owner_user_id.encode("ascii"), name.encode("ascii")))


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """What the database stores for one value (never the plaintext)."""

    ciphertext: bytes
    nonce: bytes
    key_id: str


class SecretBox:
    """Seal/open values with one master key (module docstring)."""

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise SecretKeyError("the secrets master key must be 32 bytes")
        self._aes = AESGCM(key)
        self.key_id = key_id_of(key)

    @classmethod
    def at(cls, path: Path) -> SecretBox:
        key, _created = load_or_create_master_key(path)
        return cls(key)

    def seal(self, value: bytes, *, owner_user_id: str, name: str) -> SealedSecret:
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = self._aes.encrypt(nonce, value, _aad(owner_user_id, name))
        return SealedSecret(ciphertext, nonce, self.key_id)

    def open(self, sealed: SealedSecret, *, owner_user_id: str, name: str) -> bytes:
        if not hmac.compare_digest(sealed.key_id, self.key_id):
            raise SecretKeyMismatchError(
                f"secret {name} was stored with another master key; set it again",
                details={"name": name},
            )
        if len(sealed.nonce) != NONCE_BYTES:
            raise SecretIntegrityError(
                f"secret {name} is damaged; set it again", details={"name": name}
            )
        try:
            return self._aes.decrypt(
                sealed.nonce, sealed.ciphertext, _aad(owner_user_id, name)
            )
        except InvalidTag:
            raise SecretIntegrityError(
                f"secret {name} does not decrypt for its owner and name (copied, "
                "renamed or damaged); set it again",
                details={"name": name},
            ) from None
