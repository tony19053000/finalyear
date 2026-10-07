"""Persistent node identity: ECDSA P-256 keypair + ULID ``node_id`` (ADR-0013).

Layout under the identity directory (default ``platform_dirs().data / "identity"``,
i.e. ``~/.local/share/meshforge/identity`` on Linux, ``%LOCALAPPDATA%\\MeshForge\\identity``
on Windows)::

    node.key       PKCS#8 PEM, unencrypted, ECDSA secp256r1 (the V0.7 CSR key)
    node.json      {"format": 1, "node_id", "session_epoch", "created_at",
                    "key_fingerprint_sha256"}
    identity.lock  empty advisory lock file (``common.filelock``), never deleted

Rules (ADR-0013 §6):

* A fresh identity is minted **only when neither file exists**. Exactly one file
  present is a *partial identity* and is refused - the agent must never mint a new
  ``node_id`` next to an old key.
* Corrupt PEM/JSON, a wrong ``format``, an invalid ``node_id`` or a fingerprint that
  does not match the key are refused with :class:`IdentityError` naming the path
  and the remedy.
* ``session_epoch`` is incremented and **persisted before** every new stream
  (ADR-0002 Updates §1) via :func:`next_epoch`; :func:`load_or_create` never
  changes it.
* Protection: POSIX directory ``0700``, files ``0600`` (created that way, never
  loosened); Windows ``icacls`` restricted to the current user's SID. A protection
  failure is fatal.
* Writes are atomic (:mod:`meshforge.common.atomic`): a crash leaves the old
  identity or none, never a half-written one.
* Processes are serialised: :func:`load_or_create`, :func:`next_epoch` and
  :func:`reset` hold ``identity.lock`` for their whole duration and re-check the
  on-disk state *inside* the lock, so two agents racing on an empty directory end
  with one identity and the same ``node_id``, and two racing ``next_epoch`` calls
  never persist the same epoch. As a second line of defence the create path writes
  the final files exclusively (``atomic_write_bytes(..., exclusive=True)``). The
  lock file is never deleted (``reset`` leaves it): unlinking a lock another
  process is waiting on would let two processes own it. A lock still held after
  the bounded wait is an :class:`IdentityError`.
* Key material is never logged, never in ``repr`` and never in an exception.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import IdentityError, InvalidIdError
from meshforge.common.filelock import locked
from meshforge.common.ids import NodeId, new_node_id, validate_node_id
from meshforge.common.logging import get_logger

__all__ = [
    "CA_CERT_FILE_NAME",
    "DEFAULT_LOCK_TIMEOUT_S",
    "IDENTITY_FORMAT",
    "JSON_FILE_NAME",
    "KEY_FILE_NAME",
    "LOCK_FILE_NAME",
    "MAX_SESSION_EPOCH",
    "NODE_CERT_FILE_NAME",
    "IdentityError",
    "NodeIdentity",
    "load_or_create",
    "locked_identity",
    "next_epoch",
    "reset",
]

log = get_logger(__name__)

KEY_FILE_NAME: Final = "node.key"
JSON_FILE_NAME: Final = "node.json"
NODE_CERT_FILE_NAME: Final = "node.crt"
"""V0.7 G2: the node certificate from `meshforge-agent join` (agent/enrollment.py)."""
CA_CERT_FILE_NAME: Final = "ca.crt"
"""V0.7 G2: the fabric CA certificate pinned at join."""
LOCK_FILE_NAME: Final = "identity.lock"
IDENTITY_FORMAT: Final = 1
DEFAULT_LOCK_TIMEOUT_S: Final = 10.0
MAX_SESSION_EPOCH: Final = 2**64 - 1
"""``Hello.session_epoch`` is a proto3 ``uint64`` (ADR-0002 §1); larger cannot be sent."""
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_REMEDY: Final = (
    "run `meshforge-agent identity reset` (or delete the identity directory) to "
    "mint a fresh identity; the node will get a NEW node_id"
)
_LOCK_REMEDY: Final = (
    "another meshforge-agent (or `identity` command) is using this identity "
    "directory; stop it, or wait and retry"
)


@dataclass(frozen=True, slots=True)
class NodeIdentity:
    """A loaded node identity. ``private_key`` is excluded from ``repr``."""

    node_id: NodeId
    private_key: ec.EllipticCurvePrivateKey = field(repr=False)
    session_epoch: int
    fingerprint: str
    """Hex SHA-256 of the DER SubjectPublicKeyInfo of the public key."""
    path: Path
    """The identity directory."""


# ----------------------------------------------------------------------------- public


def load_or_create(
    directory: Path, *, lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S
) -> NodeIdentity:
    """Load the identity in ``directory``; create it when neither file exists.

    Idempotent: calling it twice returns equal identities and writes nothing the
    second time. Safe across processes: the whole check-create-verify runs under
    ``identity.lock``, so a second process that lost the first-create race loads
    the winner's identity. Raises :class:`IdentityError` on partial, corrupt or
    unprotectable identity files, or when the lock is still held by another
    process after ``lock_timeout_s`` seconds.
    """
    directory = Path(directory)
    key_path = directory / KEY_FILE_NAME
    json_path = directory / JSON_FILE_NAME
    _ensure_dir(directory)

    with _locked(directory, lock_timeout_s):
        # State is decided *inside* the lock (double-check): whatever we saw
        # before acquiring it may have been changed by the process that held it.
        has_key, has_json = key_path.exists(), json_path.exists()
        if has_key != has_json:
            missing = json_path if has_key else key_path
            raise IdentityError(
                f"partial identity: {missing.name} is missing but its companion exists",
                missing,
                _REMEDY,
            )
        if not has_key:
            identity = _create(directory, key_path, json_path)
            # No path: checklist item 7 (the identity dir sits under the user's
            # home, so it would name the user). node_id + fingerprint identify it.
            log.info(
                "identity.created",
                node_id=identity.node_id,
                key_fingerprint=identity.fingerprint,
                session_epoch=identity.session_epoch,
            )
            return identity

        identity = _load(directory, key_path, json_path)
    log.info(
        "identity.loaded",
        node_id=identity.node_id,
        key_fingerprint=identity.fingerprint,
        session_epoch=identity.session_epoch,
    )
    return identity


def next_epoch(
    identity: NodeIdentity, *, lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S
) -> NodeIdentity:
    """Increment ``session_epoch``, persist it atomically, return the new identity.

    Call it before opening every new stream (ADR-0002 Updates §1). The write
    happens *before* the return, so a crash after this call can never reuse an
    epoch the controller may already have seen. Read-compute-write runs under
    ``identity.lock``: two processes racing here get distinct epochs (1 and 2),
    never the same one.
    """
    json_path = identity.path / JSON_FILE_NAME
    with _locked(identity.path, lock_timeout_s):
        current = _read_json(json_path)
        # The file may no longer be *this* identity: an `identity reset` (or a
        # hand edit) while the agent runs replaces key and json. Writing our
        # node_id next to someone else's key would wedge the node (ADR-0013 §6).
        if (
            current.get("node_id") != str(identity.node_id)
            or current.get("key_fingerprint_sha256") != identity.fingerprint
        ):
            raise IdentityError(
                "identity on disk changed underneath this process",
                json_path,
                "restart the agent so it loads the current identity",
            )
        created_at = current.get("created_at")
        if not isinstance(created_at, str):
            raise IdentityError(
                "corrupt identity: created_at missing", json_path, _REMEDY
            )
        # Never reuse an epoch: if the file on disk is already ahead (another
        # process of this node advanced it), continue from the higher value.
        on_disk = current.get("session_epoch")
        base = max(identity.session_epoch, on_disk if isinstance(on_disk, int) else 0)
        if base >= MAX_SESSION_EPOCH:
            raise IdentityError(
                f"session_epoch exhausted (would exceed {MAX_SESSION_EPOCH})",
                json_path,
                _REMEDY,
            )
        updated = NodeIdentity(
            node_id=identity.node_id,
            private_key=identity.private_key,
            session_epoch=base + 1,
            fingerprint=identity.fingerprint,
            path=identity.path,
        )
        _write_json(json_path, updated, created_at)
    log.info(
        "identity.epoch_advanced",
        node_id=updated.node_id,
        session_epoch=updated.session_epoch,
    )
    return updated


def reset(directory: Path, *, lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S) -> None:
    """Delete the identity files under the lock - and, since V0.7 G2, the
    enrolment (``node.crt``, ``ca.crt``): a certificate for the old key is
    worthless next to a new one. Missing files/directory are fine.

    The directory and ``identity.lock`` stay: deleting a lock file another process
    is blocked on would let two processes own the lock (see ``common.filelock``).
    """
    directory = Path(directory)
    if not directory.is_dir():
        return  # nothing to delete, and no reason to create a directory
    with _locked(directory, lock_timeout_s):
        # Certificate first: a crash part-way never leaves a cert for a new key.
        for name in (
            NODE_CERT_FILE_NAME,
            CA_CERT_FILE_NAME,
            KEY_FILE_NAME,
            JSON_FILE_NAME,
        ):
            path = directory / name
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise IdentityError(
                    f"could not delete identity file: {exc.strerror}",
                    path,
                    "check permissions",
                ) from exc
    log.info("identity.reset")  # no path: it would name the user (checklist 7)


# ------------------------------------------------------------------------------ lock


@contextmanager
def locked_identity(
    directory: Path, *, lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S
) -> Iterator[None]:
    """Hold ``identity.lock`` (V0.7 G2: the enrolment files ``node.crt`` /
    ``ca.crt`` live in the same folder and are written under the same lock)."""
    with _locked(Path(directory), lock_timeout_s):
        yield


@contextmanager
def _locked(directory: Path, timeout_s: float) -> Iterator[None]:
    """Hold ``identity.lock`` in ``directory``; every failure is an :class:`IdentityError`."""
    lock_path = directory / LOCK_FILE_NAME
    with ExitStack() as stack:
        # Only *acquiring* the lock is translated here; an OSError raised by the
        # body keeps its own meaning (it is not a lock problem).
        try:
            created = stack.enter_context(
                locked(lock_path, timeout_s=timeout_s, mode=_FILE_MODE)
            )
        except TimeoutError as exc:
            raise IdentityError(
                "identity directory is locked by another process",
                lock_path,
                _LOCK_REMEDY,
            ) from exc
        except OSError as exc:
            raise IdentityError(
                f"could not open identity lock file: {exc.strerror or exc}",
                lock_path,
                "check permissions",
            ) from exc
        if created:
            _protect(lock_path)
        _sweep_temp_files(directory)
        yield


def _sweep_temp_files(directory: Path) -> None:
    """Remove ``.<name>.<hex>.tmp`` debris left by a writer that was killed mid-write.

    Called under the lock, so nobody else's in-flight temp file can be swept.
    """
    for tmp in directory.glob(".*.tmp"):
        with suppress(OSError):
            tmp.unlink()


# ---------------------------------------------------------------------------- create


def _create(directory: Path, key_path: Path, json_path: Path) -> NodeIdentity:
    private_key = ec.generate_private_key(ec.SECP256R1())
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    identity = NodeIdentity(
        node_id=new_node_id(),
        private_key=private_key,
        session_epoch=0,
        fingerprint=_fingerprint(private_key),
        path=directory,
    )
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    # Key first: if we crash between the two writes the result is a partial
    # identity, which the next start refuses loudly instead of forking an id.
    # ``exclusive``: the lock already serialises creators; this makes an
    # overwrite of an existing identity impossible even if it did not.
    _write_protected(key_path, pem, exclusive=True)
    _write_json(json_path, identity, created_at, exclusive=True)
    return identity


def _write_json(
    json_path: Path, identity: NodeIdentity, created_at: str, *, exclusive: bool = False
) -> None:
    payload = {
        "format": IDENTITY_FORMAT,
        "node_id": str(identity.node_id),
        "session_epoch": identity.session_epoch,
        "created_at": created_at,
        "key_fingerprint_sha256": identity.fingerprint,
    }
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _write_protected(json_path, data, exclusive=exclusive)


def _write_protected(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    try:
        atomic_write_bytes(path, data, mode=_FILE_MODE, exclusive=exclusive)
    except FileExistsError as exc:
        raise IdentityError(
            "identity file appeared while creating a fresh identity (another "
            "process wrote it); this must not happen under the lock",
            path,
            "retry; if it persists, report it",
        ) from exc
    except OSError as exc:
        raise IdentityError(
            f"could not write identity file: {exc.strerror or exc}",
            path,
            "check permissions",
        ) from exc
    _protect(path)


# ------------------------------------------------------------------------------ load


def _load(directory: Path, key_path: Path, json_path: Path) -> NodeIdentity:
    private_key = _read_key(key_path)
    meta = _read_json(json_path)

    if meta.get("format") != IDENTITY_FORMAT:
        raise IdentityError(
            f"unsupported identity format {meta.get('format')!r} "
            f"(this agent writes format {IDENTITY_FORMAT})",
            json_path,
            "upgrade the agent, or " + _REMEDY,
        )
    try:
        node_id = validate_node_id(meta.get("node_id"))
    except InvalidIdError as exc:
        raise IdentityError(f"corrupt identity: {exc}", json_path, _REMEDY) from exc
    epoch = meta.get("session_epoch")
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or not 0 <= epoch <= MAX_SESSION_EPOCH
    ):
        raise IdentityError(
            "corrupt identity: session_epoch is not an integer in "
            f"[0, {MAX_SESSION_EPOCH}] (Hello.session_epoch is a uint64)",
            json_path,
            _REMEDY,
        )
    fingerprint = _fingerprint(private_key)
    if meta.get("key_fingerprint_sha256") != fingerprint:
        raise IdentityError(
            "identity key does not match node.json (key_fingerprint_sha256 differs); "
            "the key file was replaced or node.json belongs to another key",
            key_path,
            _REMEDY,
        )
    _check_protected(directory, key_path, json_path)
    return NodeIdentity(
        node_id=node_id,
        private_key=private_key,
        session_epoch=epoch,
        fingerprint=fingerprint,
        path=directory,
    )


def _read_key(key_path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        raw = key_path.read_bytes()
    except OSError as exc:
        raise IdentityError(
            f"could not read identity key: {exc.strerror or exc}",
            key_path,
            "check permissions",
        ) from exc
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (ValueError, TypeError) as exc:
        # The exception text from cryptography never includes the key bytes.
        raise IdentityError(
            f"corrupt identity key (not an unencrypted PKCS#8 PEM): {exc}",
            key_path,
            _REMEDY,
        ) from exc
    except Exception as exc:
        raise IdentityError(
            f"unreadable identity key: {exc}", key_path, _REMEDY
        ) from exc
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
        key.curve, ec.SECP256R1
    ):
        raise IdentityError(
            "identity key is not an ECDSA P-256 (secp256r1) key", key_path, _REMEDY
        )
    return key


def _read_json(json_path: Path) -> dict[str, Any]:
    try:
        raw = json_path.read_bytes()
    except OSError as exc:
        raise IdentityError(
            f"could not read identity metadata: {exc.strerror or exc}",
            json_path,
            "check permissions",
        ) from exc
    try:
        meta = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise IdentityError(
            f"corrupt identity metadata (not JSON): {exc}", json_path, _REMEDY
        ) from exc
    if not isinstance(meta, dict):
        raise IdentityError(
            "corrupt identity metadata (not a JSON object)", json_path, _REMEDY
        )
    return meta


def _fingerprint(private_key: ec.EllipticCurvePrivateKey) -> str:
    spki = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(spki).hexdigest()


# ------------------------------------------------------------------------ protection


def _ensure_dir(directory: Path) -> None:
    try:
        existed = directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    except OSError as exc:
        raise IdentityError(
            f"could not create identity directory: {exc.strerror or exc}",
            directory,
            "check that the data directory is writable",
        ) from exc
    if existed and os.name == "posix":
        # mkdir(mode=) only applies to a directory we create; a pre-existing one
        # may have been created loosely (e.g. by an installer). Tighten it.
        try:
            directory.chmod(_DIR_MODE)
        except OSError as exc:
            raise IdentityError(
                f"could not restrict identity directory: {exc.strerror or exc}",
                directory,
                "check ownership of the directory",
            ) from exc
    _protect(directory)


def _protect(path: Path) -> None:
    """Restrict ``path`` to the current user - one function holds the OS split."""
    if sys.platform.startswith("win"):
        _protect_windows(path)
    # POSIX: files are created 0600 and the directory 0700 by construction
    # (O_CREAT|O_EXCL mode / mkdir mode), so nothing to do here.


def _protect_windows(path: Path) -> None:
    sid = _current_user_sid()
    # Argument list, no shell; icacls resolves via PATH (system32).
    argv = [
        "icacls",
        str(path),
        "/inheritance:r",
        "/grant:r",
        f"*{sid}:(F)",
    ]
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, values are a path and a SID
            argv, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise IdentityError(
            f"icacls could not be run: {exc}", path, "icacls.exe is part of Windows"
        ) from exc
    if result.returncode != 0:
        raise IdentityError(
            f"icacls failed (exit {result.returncode}): {result.stderr.strip()}",
            path,
            "the identity must be private to the current user; check ownership",
        )


def _current_user_sid() -> str:
    """SID of the current Windows user via ``whoami /user /fo csv`` (locale-independent)."""
    try:
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv"],  # noqa: S607 - Windows built-in
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise IdentityError(
            f"whoami could not be run: {exc}",
            Path("whoami"),
            "whoami.exe is part of Windows",
        ) from exc
    rows = list(csv.reader(result.stdout.splitlines()))
    if result.returncode != 0 or len(rows) < 2 or len(rows[1]) < 2:
        raise IdentityError(
            f"could not determine the current user's SID (exit {result.returncode})",
            Path("whoami"),
            "run `whoami /user` by hand and report the output",
        )
    sid = rows[1][1].strip()
    if not sid.startswith("S-1-"):
        raise IdentityError(
            "whoami returned an unexpected SID",
            Path("whoami"),
            "report `whoami /user` output",
        )
    return sid


def _check_protected(directory: Path, key_path: Path, json_path: Path) -> None:
    """On POSIX refuse an identity whose files are readable by others."""
    if os.name != "posix":
        return
    for path in (key_path, json_path):
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise IdentityError(
                f"identity file is accessible to other users (mode {mode:04o})",
                path,
                f"chmod 600 {path} (and 700 {directory}), or " + _REMEDY,
            )
