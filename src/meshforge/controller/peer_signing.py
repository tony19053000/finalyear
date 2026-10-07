"""The controller's peer-token signing key (V0.5 D98).

``peer_signing.key`` lives next to ``fabric.token`` in ``<data_dir>/controller/``
(ARCHITECTURE §23) and follows the same rules (D44, ADR-0013/0014):

* **Create once, atomically**, ``0600`` from creation, under a lock file, in a
  ``0700`` folder (owner-only ACL on Windows) - two processes racing on an empty
  folder end with one key.
* **Load strictly** (:func:`~meshforge.common.private_files.read_private`, D149a):
  a symlink, FIFO or device, a file other users can read or another user owns,
  an oversized file or anything that is not an unencrypted PKCS#8 Ed25519 PEM key is a
  :class:`PeerSigningKeyError` (``config_error``) - the controller refuses to
  start rather than sign with a key it cannot trust.
* The key persists, so a controller restart does **not** invalidate peer tokens
  already handed out (D98). Never logged; only the SHA-256 fingerprint prefix of
  the *public* key is.

:class:`PeerTokenIssuer` signs tokens (:mod:`meshforge.common.peer_token`) with
an injected wall clock.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.fabric_token import CONTROLLER_DIR_NAME
from meshforge.common.filelock import locked
from meshforge.common.hashing import Digest
from meshforge.common.ids import AttemptId, NodeId
from meshforge.common.logging import get_logger
from meshforge.common.peer_token import (
    MAX_TTL_S,
    PROBE_MAX_TTL_S,
    sign_peer_token,
    sign_probe_token,
)
from meshforge.common.private_files import (
    PrivateFileError,
    protect_private,
    read_private,
)

__all__ = [
    "KEY_FILE_NAME",
    "MAX_KEY_FILE_BYTES",
    "PeerSigningKeyError",
    "PeerTokenIssuer",
    "default_key_path",
    "load_or_create_signing_key",
    "public_key_bytes",
    "public_key_fingerprint",
]

log = get_logger(__name__)

KEY_FILE_NAME: Final = "peer_signing.key"
MAX_KEY_FILE_BYTES: Final = 4096
_LOCK_TIMEOUT_S: Final = 10.0
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600


class PeerSigningKeyError(MeshForgeError):
    """The signing key file is unreadable, malformed or not private."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR


def default_key_path(data_dir: Path) -> Path:
    """``<data_dir>/controller/peer_signing.key``."""
    return data_dir / CONTROLLER_DIR_NAME / KEY_FILE_NAME


def public_key_bytes(key: Ed25519PrivateKey) -> bytes:
    """The raw 32-byte public key sent in ``HelloAck.peer_token_key``."""
    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def public_key_fingerprint(key: Ed25519PrivateKey) -> str:
    """First 12 hex characters of SHA-256(public key): loggable."""
    return hashlib.sha256(public_key_bytes(key)).hexdigest()[:12]


def _ensure_dir(directory: Path) -> None:
    try:
        existed = directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        if not existed:
            protect_private(directory)
    except OSError as exc:
        raise PeerSigningKeyError(
            f"cannot create the key folder {directory}: {exc.strerror or exc}",
            details={"path": str(directory)},
        ) from None
    except PrivateFileError as exc:
        raise PeerSigningKeyError(exc.message, details=exc.details) from None


def _read(path: Path) -> Ed25519PrivateKey:
    details = {"path": str(path)}
    try:
        # D149a: regular file only (no symlink/FIFO/device), bounded, and on
        # POSIX owner-only *and* owned by us - the same reader as the CA key.
        raw = read_private(path, limit=MAX_KEY_FILE_BYTES, what="peer signing key")
    except PrivateFileError as exc:
        raise PeerSigningKeyError(exc.message, details=details) from None
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (ValueError, TypeError):
        raise PeerSigningKeyError(
            f"the peer signing key {path} is not an unencrypted PEM key; delete it "
            "to create a new one (peer tokens issued with the old key stop working)",
            details=details,
        ) from None
    if not isinstance(key, Ed25519PrivateKey):
        raise PeerSigningKeyError(
            f"the peer signing key {path} is not an Ed25519 key", details=details
        )
    return key


def load_or_create_signing_key(path: Path) -> tuple[Ed25519PrivateKey, bool]:
    """Return ``(key, created)``; create the file when it does not exist."""
    _ensure_dir(path.parent)
    try:
        with locked(
            path.with_name(path.name + ".lock"),
            timeout_s=_LOCK_TIMEOUT_S,
            mode=_FILE_MODE,
        ):
            if path.exists():
                return _read(path), False
            key = Ed25519PrivateKey.generate()
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
            try:
                atomic_write_bytes(path, pem, mode=_FILE_MODE, exclusive=True)
                protect_private(path)
            except OSError as exc:
                raise PeerSigningKeyError(
                    f"cannot write the peer signing key {path}: {exc.strerror or exc}",
                    details={"path": str(path)},
                ) from None
            except PrivateFileError as exc:
                raise PeerSigningKeyError(exc.message, details=exc.details) from None
    except TimeoutError:
        raise PeerSigningKeyError(
            "the peer signing key is locked by another process; retry",
            details={"path": str(path)},
        ) from None
    log.info("controller.peer_key.created", fingerprint=public_key_fingerprint(key))
    return key, True


class PeerTokenIssuer:
    """Signs peer tokens with the controller key (D98); clock is wall time."""

    def __init__(
        self,
        key: Ed25519PrivateKey,
        *,
        ttl_s: int = MAX_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 1 <= ttl_s <= MAX_TTL_S:
            raise ValueError(f"peer token ttl must be 1..{MAX_TTL_S} s")
        self._key = key
        self._ttl_s = ttl_s
        self._clock = clock
        self.public_key = public_key_bytes(key)

    def issue(
        self,
        *,
        tree: Digest,
        requester: NodeId,
        holder: NodeId,
        attempt_id: AttemptId,
    ) -> str:
        return sign_peer_token(
            self._key,
            tree=tree,
            requester=requester,
            holder=holder,
            attempt_id=attempt_id,
            now=self._clock(),
            ttl_s=self._ttl_s,
        )

    def issue_probe(
        self, *, requester: str, holder: str, ttl_s: int = PROBE_MAX_TTL_S
    ) -> str:
        """A probe token (V0.6 G4, D111): ``requester`` may call ``holder``'s
        ``/v1/probe`` for ``ttl_s`` (<= 60) seconds."""
        return sign_probe_token(
            self._key,
            requester=requester,
            holder=holder,
            now=self._clock(),
            ttl_s=ttl_s,
        )
