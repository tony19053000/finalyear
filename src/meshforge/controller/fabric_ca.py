"""The fabric CA and the controller's server certificate on disk (V0.7 G1, D134).

Layout (ARCHITECTURE §23; both folders ``0700``, every file ``0600`` / owner-only
ACL on Windows - the private-file rules of ADR-0013)::

    <data_dir>/controller/ca/ca.key       the fabric CA private key (never leaves this host)
    <data_dir>/controller/ca/ca.crt       the fabric CA certificate (agents/CLI pin it)
    <data_dir>/controller/ca.lock         serialises create/replace (never deleted)
    <data_dir>/controller/tls/server.key  the controller's TLS key (fresh at every issue)
    <data_dir>/controller/tls/server.crt  issued by the CA, 1 year, renewed at 2/3 life
    <data_dir>/controller/tls.lock

**A pair is installed atomically.** Both files are written into a fresh hidden
folder next to the target, then the folder is renamed into place (a directory
rename is atomic on Linux and Windows). Replacing (``--force``, a renewal) first
renames the old folder aside, then the new one in, then deletes the old one. A
crash at any point leaves either the old pair, the new pair, or no folder (only
during a replace) - never a key next to a certificate it does not match.
Leftover hidden folders are swept under the lock on the next create/issue, the
next controller start and ``fabric show`` (:func:`recover_interrupted`); a lone
``.ca.old-*`` with no ``ca/`` (a crash mid-replace) is **restored**, never deleted
(D149c).

**Loading is strict and fails closed** (:func:`load_fabric`): a missing file, a
loose key, an oversized/garbled file, a certificate that is not a self-signed CA,
an expired CA, or a key that does not match the certificate is a
:class:`FabricCaError` (``config_error``) naming the path and the remedy. The
server certificate is *derived* material: :func:`ensure_server_cert` re-issues it
(fresh key) whenever it is missing, unreadable, from another CA, past two thirds
of its life, or its SANs differ from the configured hosts - never serves a
doubtful one.

G1 creates and stores these; **no listener uses them yet** (TLS arrives in G3).
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from meshforge.common import pki
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.filelock import locked
from meshforge.common.ids import FabricId, new_fabric_id
from meshforge.common.logging import get_logger
from meshforge.common.netaddr import parse_bind
from meshforge.common.private_files import (
    PrivateFileError,
    private_problem,
    protect_private,
)

__all__ = [
    "CA_CERT_NAME",
    "CA_DIR_NAME",
    "CA_KEY_NAME",
    "SERVER_CERT_NAME",
    "SERVER_KEY_NAME",
    "TLS_DIR_NAME",
    "FabricCa",
    "FabricCaError",
    "FabricExistsError",
    "FabricMissingError",
    "ServerCert",
    "ServerCertAction",
    "create_fabric",
    "ensure_server_cert",
    "fabric_present",
    "load_fabric",
    "load_server_cert",
    "recover_interrupted",
    "server_cert_hosts",
]

log = get_logger(__name__)

CA_DIR_NAME: Final = "ca"
TLS_DIR_NAME: Final = "tls"
CA_KEY_NAME: Final = "ca.key"
CA_CERT_NAME: Final = "ca.crt"
SERVER_KEY_NAME: Final = "server.key"
SERVER_CERT_NAME: Final = "server.crt"
_LOCK_TIMEOUT_S: Final = 10.0
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_RECREATE: Final = (
    "run `meshforge-controller fabric create --force` (every node must re-join)"
)


class FabricCaError(MeshForgeError):
    """The fabric CA or server certificate files cannot be trusted or written."""

    default_failure_type = FailureType.CONFIG_ERROR
    exit_code = ExitCode.CONFIG_ERROR


class FabricMissingError(FabricCaError):
    """No fabric CA exists yet (``fabric create`` has not run)."""


class FabricExistsError(FabricCaError):
    """``fabric create`` without ``--force`` while a CA exists."""

    default_failure_type = FailureType.CONFLICT
    exit_code = ExitCode.CONFLICT


@dataclass(frozen=True, slots=True)
class FabricCa:
    """A loaded fabric CA. The key is excluded from ``repr``."""

    authority: pki.CertificateAuthority = field(repr=False)
    path: Path

    @property
    def fabric_id(self) -> FabricId:
        return self.authority.fabric_id

    @property
    def cert(self) -> x509.Certificate:
        return self.authority.cert

    @property
    def fingerprint(self) -> str:
        return pki.cert_fingerprint(self.authority.cert)

    @property
    def short_fingerprint(self) -> str:
        return pki.short_fingerprint(self.authority.cert)


class ServerCertAction(StrEnum):
    """What :func:`ensure_server_cert` did (logged as ``action``)."""

    KEPT = "kept"
    ISSUED = "issued"
    """There was none."""
    RENEWED = "renewed"
    """Past :data:`~meshforge.common.pki.RENEW_FRACTION` of its life."""
    REISSUED = "reissued"
    """Present but unusable (``reason`` says why)."""


@dataclass(frozen=True, slots=True)
class ServerCert:
    """The controller's TLS key + certificate and what was done to get them."""

    issued: pki.IssuedCert = field(repr=False)
    path: Path
    action: ServerCertAction
    reason: str | None = None

    @property
    def cert(self) -> x509.Certificate:
        return self.issued.cert

    @property
    def key(self) -> ec.EllipticCurvePrivateKey:
        return self.issued.key


# --------------------------------------------------------------------- filesystem


def _ensure_parent(directory: Path) -> None:
    try:
        existed = directory.is_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        if not existed:
            protect_private(directory)
    except OSError as exc:
        raise FabricCaError(
            f"cannot create the folder {directory}: {exc.strerror or exc}",
            details={"path": str(directory)},
        ) from None
    except PrivateFileError as exc:
        raise FabricCaError(exc.message, details=exc.details) from None


@contextlib.contextmanager
def _lock(target: Path) -> Iterator[None]:
    _ensure_parent(target.parent)
    lock_path = target.with_name(target.name + ".lock")
    with contextlib.ExitStack() as stack:
        # Only *taking* the lock is translated here; errors raised by the caller's
        # block pass through unchanged.
        try:
            stack.enter_context(
                locked(lock_path, timeout_s=_LOCK_TIMEOUT_S, mode=_FILE_MODE)
            )
        except TimeoutError:
            raise FabricCaError(
                f"{lock_path} is held by another process; retry",
                details={"path": str(lock_path)},
            ) from None
        except OSError as exc:  # unwritable folder, read-only fs, disk full
            raise FabricCaError(
                f"cannot create the lock file {lock_path}: "
                f"{exc.strerror or exc}; the folder {lock_path.parent} must be "
                "writable by the user running the controller",
                details={"path": str(lock_path.parent)},
            ) from None
        yield


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":
        return
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _sweep(target: Path) -> None:
    """Clean up what a crash during :func:`_install_dir` left (caller holds the lock).

    * ``target`` exists: every hidden ``.<name>.new-*`` / ``.<name>.old-*``
      folder is debris and is removed.
    * ``target`` is missing and exactly **one** ``.<name>.old-*`` folder exists:
      the crash hit a replace between "old aside" and "new in". That folder is
      the only copy of the previous pair (for ``ca/``: the CA key every node
      trusts) - it is **renamed back**, never deleted (D149c). The staging folder
      is then debris.
    * ``target`` is missing and several ``.old-*`` folders exist: which one is
      current cannot be known - a typed :class:`FabricCaError` naming them,
      nothing deleted (the operator decides; OPERATIONS §0a2).
    """
    parent = target.parent
    olds = sorted(
        p
        for p in parent.glob(f".{target.name}.old-*")
        if p.is_dir() and not p.is_symlink()
    )
    if not _present(target) and olds:
        if len(olds) > 1:
            raise FabricCaError(
                f"{target} is missing and {len(olds)} earlier copies were left by "
                f"interrupted replaces ({', '.join(p.name for p in olds)}); rename "
                f"the right one to {target.name} by hand",
                details={"path": str(target)},
            )
        try:
            os.rename(olds[0], target)
        except OSError as exc:
            raise FabricCaError(
                f"cannot restore {target} from {olds[0].name} after an interrupted "
                f"replace: {exc.strerror or exc}",
                details={"path": str(target)},
            ) from None
        _fsync_dir(parent)
        log.warning(
            "controller.fabric.interrupted_replace_restored",
            path=target.name,
            restored_from=olds[0].name,
        )
    for leftover in parent.glob(f".{target.name}.*-*"):
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover, ignore_errors=True)
            log.info("controller.fabric.leftover_removed", path=leftover.name)


def recover_interrupted(target: Path) -> None:
    """Take the lock and run the crash clean-up on ``target`` (``ca/`` or
    ``tls/``): the controller start and ``fabric show`` call it so an interrupted
    ``fabric create --force`` is repaired before anything reads the folder."""
    if not target.parent.is_dir():
        return
    with _lock(target):
        _sweep(target)


def _install_dir(target: Path, files: Mapping[str, bytes]) -> None:
    """Atomically make ``target`` a folder holding exactly ``files`` (see module doc).

    Caller holds the lock. Raises :class:`FabricCaError`; on failure ``target`` is
    the old folder (or, if the swap itself failed half way, restored to it).
    """
    parent = target.parent
    staging = parent / f".{target.name}.new-{secrets.token_hex(4)}"
    aside = parent / f".{target.name}.old-{secrets.token_hex(4)}"
    moved_aside = False
    try:
        os.mkdir(staging, _DIR_MODE)
        protect_private(staging)
        for name, data in files.items():
            path = staging / name
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            protect_private(path)
        _fsync_dir(staging)
        if target.exists() or target.is_symlink():
            os.rename(target, aside)
            moved_aside = True
        os.rename(staging, target)
    except (OSError, PrivateFileError) as exc:
        if moved_aside and not target.exists():
            with contextlib.suppress(OSError):
                os.rename(aside, target)
                moved_aside = False
        shutil.rmtree(staging, ignore_errors=True)
        message = (
            exc.message
            if isinstance(exc, PrivateFileError)
            else (exc.strerror or str(exc))
        )
        raise FabricCaError(
            f"cannot write {target}: {message}", details={"path": str(target)}
        ) from None
    _fsync_dir(parent)
    if moved_aside:
        shutil.rmtree(aside, ignore_errors=True)


def _check_private_dir(directory: Path) -> None:
    """Folder is private (mode) and, on POSIX, owned by the user running us."""
    problem = private_problem(directory)
    if problem is not None:
        raise FabricCaError(
            f"the folder {directory} is {problem.replace('chmod 600', 'chmod 700')}",
            details={"path": str(directory)},
        )
    if hasattr(os, "geteuid"):
        owner = directory.stat().st_uid
        if owner != os.geteuid():
            raise FabricCaError(
                f"the folder {directory} is owned by another user (uid {owner}); it "
                "must belong to the user running the controller (chown it)",
                details={"path": str(directory)},
            )


# ---------------------------------------------------------------------------- CA


def _present(folder: Path) -> bool:
    """Something (folder, file, even a dangling symlink) is at ``folder``."""
    return folder.exists() or folder.is_symlink()


def fabric_present(ca_dir: Path) -> bool:
    """Whether a CA folder exists (complete or not - :func:`load_fabric` judges)."""
    return _present(ca_dir)


def _verify_ca_pair(
    key: ec.EllipticCurvePrivateKey, cert: x509.Certificate, now: datetime | None
) -> FabricId:
    """The CA cert is a valid self-signed CA for ``key``; return its fabric id."""
    fabric_id = pki.parse_fabric_id(cert)
    bc = pki.cert_extensions(cert).get_extension_for_class(x509.BasicConstraints).value
    if not bc.ca:
        raise pki.PkiError(pki.PkiReason.NOT_A_CA, "certificate is not a CA")
    pki.verify_issued_by(cert, cert)
    if not pki.public_keys_equal(key.public_key(), cert.public_key()):
        raise pki.PkiError(
            pki.PkiReason.KEY_MISMATCH, "the key does not match the certificate"
        )
    pki.check_validity(cert, now)
    return fabric_id


def load_fabric(ca_dir: Path, *, now: datetime | None = None) -> FabricCa:
    """Load and verify the fabric CA (strict; see module doc)."""
    if not fabric_present(ca_dir):
        raise FabricMissingError(
            f"no fabric CA at {ca_dir}; run `meshforge-controller fabric create`",
            details={"path": str(ca_dir)},
        )
    if ca_dir.is_symlink() or not ca_dir.is_dir():
        raise FabricCaError(
            f"{ca_dir} is not a folder; {_RECREATE}", details={"path": str(ca_dir)}
        )
    _check_private_dir(ca_dir)
    key_path, cert_path = ca_dir / CA_KEY_NAME, ca_dir / CA_CERT_NAME
    missing = [p.name for p in (key_path, cert_path) if not p.is_file()]
    if missing:
        raise FabricCaError(
            f"the fabric CA in {ca_dir} is incomplete (missing {', '.join(missing)}); "
            f"{_RECREATE}",
            details={"path": str(ca_dir)},
        )
    try:
        key = pki.read_private_key(key_path, what="fabric CA key")
        cert = pki.read_cert(cert_path, what="fabric CA certificate")
        fabric_id = _verify_ca_pair(key, cert, now)
    except pki.PkiFileError as exc:
        raise FabricCaError(exc.message, details=exc.details) from None
    except pki.PkiError as exc:
        raise FabricCaError(
            f"the fabric CA in {ca_dir} is unusable ({exc.reason.value}: "
            f"{exc.message}); {_RECREATE}",
            details={"path": str(ca_dir), "reason": exc.reason.value},
        ) from None
    except (x509.ExtensionNotFound, ValueError):
        raise FabricCaError(
            f"the fabric CA certificate in {ca_dir} is malformed; {_RECREATE}",
            details={"path": str(ca_dir)},
        ) from None
    return FabricCa(
        authority=pki.CertificateAuthority(key=key, cert=cert, fabric_id=fabric_id),
        path=ca_dir,
    )


def create_fabric(
    ca_dir: Path,
    *,
    force: bool = False,
    now: datetime | None = None,
    lifetime: timedelta = pki.CA_LIFETIME,
) -> FabricCa:
    """Create the fabric CA (new ``fabric_id``, new P-256 key, 10 years).

    Refuses with :class:`FabricExistsError` when any CA folder exists, unless
    ``force`` - which replaces it atomically (every node cert from the old CA is
    then worthless; the caller warns).
    """
    with _lock(ca_dir):
        _sweep(ca_dir)
        if fabric_present(ca_dir) and not force:
            raise FabricExistsError(
                f"a fabric CA already exists in {ca_dir}; `meshforge-controller "
                "fabric show` prints it. `fabric create --force` replaces it and "
                "every node must re-join",
                details={"path": str(ca_dir)},
            )
        replaced = fabric_present(ca_dir)
        authority = pki.create_ca(new_fabric_id(), now=now, lifetime=lifetime)
        _install_dir(
            ca_dir,
            {
                CA_KEY_NAME: pki.key_to_pem(authority.key),
                CA_CERT_NAME: pki.cert_to_pem(authority.cert),
            },
        )
    fabric = FabricCa(authority=authority, path=ca_dir)
    log.info(
        "controller.fabric.created",
        fabric_id=fabric.fabric_id,
        ca_fingerprint=fabric.short_fingerprint,
        replaced=replaced,
    )
    return fabric


# ------------------------------------------------------------------- server cert


def server_cert_hosts(
    binds: Sequence[str],
    *,
    advertise_url: str | None = None,
    extra: Sequence[str] = (),
) -> list[str]:
    """SAN hosts for the server cert: bind hosts (not wildcards), the
    ``advertise_url`` host and ``extra``, deduplicated, order kept.
    :data:`~meshforge.common.pki.SERVER_NAME` is always added by the issuer."""
    hosts: list[str] = []
    for bind in binds:
        host = parse_bind(bind, allow_any=True).host
        if host not in ("0.0.0.0", "::"):  # noqa: S104 - filtering wildcards out
            hosts.append(host)
    if advertise_url:
        authority = advertise_url.split("://", 1)[-1]
        host = authority.rsplit(":", 1)[0].removeprefix("[").removesuffix("]")
        hosts.append(host)
    hosts.extend(extra)
    out: list[str] = []
    for host in hosts:
        if host not in out:
            out.append(host)
    return out


def _wanted_sans(hosts: Sequence[str]) -> list[str]:
    """The SAN list :func:`pki.issue_server_cert` would produce for ``hosts``."""
    names: list[x509.GeneralName] = [x509.DNSName(pki.SERVER_NAME)]
    for host in hosts:
        entry = pki.validate_san_host(host)
        if entry not in names:
            names.append(entry)
    return [str(n.value) for n in names]


def load_server_cert(tls_dir: Path) -> pki.IssuedCert:
    """Read ``server.key`` + ``server.crt`` strictly (pair must match)."""
    _check_private_dir(tls_dir)
    try:
        key = pki.read_private_key(tls_dir / SERVER_KEY_NAME, what="server key")
        cert = pki.read_cert(tls_dir / SERVER_CERT_NAME, what="server certificate")
    except pki.PkiFileError as exc:
        raise FabricCaError(exc.message, details=exc.details) from None
    if not pki.public_keys_equal(key.public_key(), cert.public_key()):
        raise FabricCaError(
            f"the server key in {tls_dir} does not match its certificate",
            details={"path": str(tls_dir)},
        )
    return pki.IssuedCert(key=key, cert=cert)


def _problem(
    current: pki.IssuedCert, fabric: FabricCa, wanted: list[str], now: datetime
) -> tuple[ServerCertAction, str] | None:
    try:
        pki.verify_issued_by(current.cert, fabric.cert)
    except pki.PkiError:
        return ServerCertAction.REISSUED, "other_ca"
    try:
        sans = pki.cert_sans(current.cert)
    except pki.PkiError as exc:  # duplicated/unsupported extensions (D149b)
        return ServerCertAction.REISSUED, exc.reason.value
    if sans != wanted:
        return ServerCertAction.REISSUED, "sans_changed"
    try:
        pki.check_validity(current.cert, now)
    except pki.PkiError as exc:
        return ServerCertAction.REISSUED, exc.reason.value
    if pki.renewal_due(current.cert, now):
        return ServerCertAction.RENEWED, "renewal_due"
    return None


def ensure_server_cert(
    tls_dir: Path,
    fabric: FabricCa,
    *,
    hosts: Sequence[str],
    now: datetime | None = None,
    lifetime: timedelta = pki.SERVER_CERT_LIFETIME,
) -> ServerCert:
    """Return a usable server cert, issuing a fresh key + cert when needed."""
    current_time = now or datetime.now(UTC)
    try:
        wanted = _wanted_sans(hosts)
    except pki.PkiError as exc:
        raise FabricCaError(
            f"cannot name the controller in its certificate: {exc.message} "
            "(check [controller] binds, [controller.api] advertise_url and "
            "[controller.tls] extra_sans)",
            details={"reason": exc.reason.value},
        ) from None
    with _lock(tls_dir):
        _sweep(tls_dir)
        action, reason = ServerCertAction.ISSUED, None
        if _present(tls_dir):
            try:
                current = load_server_cert(tls_dir)
            except FabricCaError as exc:
                action, reason = ServerCertAction.REISSUED, "unreadable"
                log.warning(
                    "controller.tls.server_cert_unusable",
                    path=str(tls_dir),
                    error=exc.message,
                )
            else:
                found = _problem(current, fabric, wanted, current_time)
                if found is None:
                    return ServerCert(
                        issued=current, path=tls_dir, action=ServerCertAction.KEPT
                    )
                action, reason = found
        try:
            issued = pki.issue_server_cert(
                fabric.authority, hosts=hosts, now=current_time, lifetime=lifetime
            )
        except pki.PkiError as exc:
            raise FabricCaError(
                f"cannot issue the server certificate: {exc.message}",
                details={"reason": exc.reason.value},
            ) from None
        _install_dir(
            tls_dir,
            {
                SERVER_KEY_NAME: pki.key_to_pem(issued.key),
                SERVER_CERT_NAME: pki.cert_to_pem(issued.cert),
            },
        )
    log.info(
        "controller.tls.server_cert_issued",
        action=action.value,
        reason=reason,
        fingerprint=pki.short_fingerprint(issued.cert),
        not_after=issued.cert.not_valid_after_utc.isoformat(),
        sans=pki.cert_sans(issued.cert),
    )
    return ServerCert(issued=issued, path=tls_dir, action=action, reason=reason)
