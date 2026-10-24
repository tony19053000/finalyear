"""Joining a fabric: ``meshforge-agent join <code>`` (V0.7 G2, 0.7.4, D136/D150).

The flow, in this order - **the join token never leaves this laptop before the
CA is pinned**:

1. :func:`~meshforge.common.join_code.decode` the code (a typo is ``bad_code``
   here, never a round trip);
2. refuse early if this laptop is already enrolled and ``--force`` was not given
   (the code is not spent);
3. :func:`fetch_presented_ca` - a throwaway TLS handshake with verification
   **off** (ALPN ``h2``, no bytes sent after it) to read the chain the
   enrolment listener presents; :func:`pin_ca` keeps only a **self-signed CA**
   certificate whose SHA-256 starts with the code's 128-bit pin (constant-time,
   :func:`pki.fingerprint_matches(require_full=False)`) and that issued the
   server's own certificate. Anything else is ``pin_mismatch`` and nothing more
   is sent;
4. a CSR signed by the node's ADR-0013 key, naming its ``node_id`` and the
   pinned CA's ``fabric_id`` (:func:`pki.build_csr`);
5. ``Enroll(token, CSR)`` over a **verifying** ``grpc.secure_channel`` whose only
   root is the pinned CA (target name ``meshforge-controller``);
6. :func:`check_issued` - the returned certificate must be issued by the pinned
   CA, carry **our** public key, name **our** ``node_id`` in **our** fabric, and
   be valid now; the returned CA must be the pinned one;
7. :func:`store` - ``ca.crt`` then ``node.crt`` beside ``node.key``, under the
   identity lock, owner-only (``0600`` / Windows ACL): both are staged in full
   first, then swapped in with ``os.replace``, so a failed (``--force``) re-join
   keeps the old enrolment (D153 item 2).

:func:`load` reads an enrolment back strictly (``identity show``; G3 uses it for
mTLS): regular owner-only files (``private_files.read_private``), the pair must
verify against each other and against the identity key.

Every failure is a :class:`JoinError` with a stable ``reason``; nothing here
ever logs or prints the code or the token.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import secrets
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

import grpc
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from meshforge.agent.identity import (
    CA_CERT_FILE_NAME,
    DEFAULT_LOCK_TIMEOUT_S,
    NODE_CERT_FILE_NAME,
    NodeIdentity,
    locked_identity,
)
from meshforge.common import join_code, pki
from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.ids import FabricId
from meshforge.common.logging import get_logger
from meshforge.common.private_files import PrivateFileError, protect_private
from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.common.tls_pin import (
    chain_der,
    is_self_signed_ca,
    pinned_ca,
    presented_chain,
)

__all__ = [
    "REASON_METADATA_KEY",
    "Enrollment",
    "JoinError",
    "JoinReason",
    "JoinResult",
    "check_issued",
    "check_renewed",
    "enroll",
    "fetch_presented_ca",
    "is_enrolled",
    "join",
    "load",
    "pin_ca",
    "replace_node_cert",
    "store",
]

log = get_logger(__name__)

REASON_METADATA_KEY: Final = "meshforge-reason"
DEFAULT_TIMEOUT_S: Final = 15.0


class JoinReason(StrEnum):
    BAD_CODE = "bad_code"
    ALREADY_ENROLLED = "already_enrolled"
    UNREACHABLE = "unreachable"
    TLS_ERROR = "tls_error"
    PIN_MISMATCH = "pin_mismatch"
    REFUSED = "refused"
    BAD_RESPONSE = "bad_response"
    STORAGE = "storage"
    NOT_ENROLLED = "not_enrolled"
    INVALID_ENROLLMENT = "invalid_enrollment"


_EXIT: Final = {
    JoinReason.BAD_CODE: ExitCode.USAGE_ERROR,
    JoinReason.ALREADY_ENROLLED: ExitCode.CONFLICT,
    JoinReason.UNREACHABLE: ExitCode.UNAVAILABLE,
    JoinReason.STORAGE: ExitCode.CONFIG_ERROR,
    JoinReason.INVALID_ENROLLMENT: ExitCode.CONFIG_ERROR,
}


class JoinError(MeshForgeError):
    """Joining failed (or a stored enrolment is unusable). ``reason`` says
    where; ``controller_reason`` is the controller's refusal code, if any."""

    default_failure_type = FailureType.UNAUTHORIZED

    def __init__(
        self,
        reason: JoinReason,
        message: str,
        *,
        controller_reason: str | None = None,
        failure_type: FailureType | None = None,
    ) -> None:
        details: dict[str, object] = {"reason": reason.value}
        if controller_reason:
            details["controller_reason"] = controller_reason
        super().__init__(message, failure_type=failure_type, details=details)
        self.reason = reason
        self.controller_reason = controller_reason

    @property
    def exit_code_for(self) -> ExitCode:
        return _EXIT.get(self.reason, ExitCode.FAILURE)


@dataclass(frozen=True, slots=True)
class Enrollment:
    """A stored, verified enrolment."""

    node_cert: x509.Certificate = field(repr=False)
    ca_cert: x509.Certificate = field(repr=False)
    fabric_id: FabricId
    node_id: str
    path: Path

    @property
    def not_after(self) -> datetime:
        return self.node_cert.not_valid_after_utc

    @property
    def cert_fingerprint(self) -> str:
        return pki.cert_fingerprint(self.node_cert)

    @property
    def ca_fingerprint(self) -> str:
        return pki.cert_fingerprint(self.ca_cert)


# ------------------------------------------------------------------- TLS + pin


def _chain_der(sock: ssl.SSLSocket) -> list[bytes]:
    """The certificate chain the peer sent, unverified, as DER
    (:func:`meshforge.common.tls_pin.chain_der`, shared with ``compute login``)."""
    der = chain_der(sock)
    if der is None:
        raise JoinError(
            JoinReason.TLS_ERROR,
            "this Python cannot read the controller's certificate chain",
        )
    return der


def fetch_presented_ca(
    host: str, port: int, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> list[x509.Certificate]:
    """The certificates the enrolment listener presents (verification OFF).

    A plain TLS handshake (ALPN ``h2``, server name ``meshforge-controller``),
    then the socket is closed: nothing is sent after the handshake, so a fake
    controller learns nothing but that someone connected.
    """
    try:
        return presented_chain(host, port, timeout_s=timeout_s, alpn=("h2",))
    except ssl.SSLError as exc:
        raise JoinError(
            JoinReason.TLS_ERROR,
            f"the controller at {host}:{port} did not complete a TLS handshake "
            f"({exc.reason or type(exc).__name__}); is this the enrolment port?",
            failure_type=FailureType.NETWORK_ERROR,
        ) from None
    except OSError as exc:
        raise JoinError(
            JoinReason.UNREACHABLE,
            f"cannot reach the controller at {host}:{port} "
            f"({exc.strerror or type(exc).__name__})",
            failure_type=FailureType.NETWORK_ERROR,
        ) from None


_is_self_signed_ca = is_self_signed_ca


def pin_ca(presented: list[x509.Certificate], pin: str) -> x509.Certificate:
    """The presented CA certificate that matches the code's 128-bit ``pin`` and
    issued the server's certificate; else ``pin_mismatch`` (send nothing)."""
    if not presented:
        raise JoinError(
            JoinReason.PIN_MISMATCH, "the controller presented no certificate"
        )
    found = pinned_ca(presented, pin, require_full=False)
    if found is not None:
        return found
    raise JoinError(
        JoinReason.PIN_MISMATCH,
        "the controller at this address does not hold the fabric CA your code "
        "names (wrong controller, another fabric, or a CA replaced since the code "
        "was made). Nothing was sent; ask for a new code and check the address",
    )


# ---------------------------------------------------------------------- Enroll


def enroll(
    address: str,
    ca_cert: x509.Certificate,
    token: bytes,
    csr: x509.CertificateSigningRequest,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> enroll_pb2.EnrollResponse:
    """``Enroll`` over a channel that trusts only ``ca_cert`` (D150)."""
    credentials = grpc.ssl_channel_credentials(
        root_certificates=pki.cert_to_pem(ca_cert)
    )
    options = [("grpc.ssl_target_name_override", pki.SERVER_NAME)]
    request = enroll_pb2.EnrollRequest(
        join_token=token, csr_pem=csr.public_bytes(serialization.Encoding.PEM)
    )
    with grpc.secure_channel(address, credentials, options=options) as channel:
        stub = enroll_pb2_grpc.EnrollmentStub(channel)
        try:
            response: enroll_pb2.EnrollResponse = stub.Enroll(
                request, timeout=timeout_s
            )
        except grpc.RpcError as exc:
            raise _rpc_error(exc, address) from None
    return response


def _rpc_error(exc: grpc.RpcError, address: str) -> JoinError:
    # The sync stub raises an object that is both RpcError and Call; read its
    # status defensively (anything missing -> None).
    call: Any = exc
    code: grpc.StatusCode | None = _call(call, "code")
    details = str(_call(call, "details") or "")
    reason = None
    for key, value in _call(call, "trailing_metadata") or ():
        if key == REASON_METADATA_KEY and isinstance(value, str):
            reason = value[:64]
    safe = "".join(ch if ch.isprintable() else " " for ch in details)[:300]
    name = code.name if code is not None else "error"
    if reason is None and code in (
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
    ):
        return JoinError(
            JoinReason.UNREACHABLE,
            f"the controller at {address} did not answer ({name}"
            f"{': ' + safe if safe else ''})",
            failure_type=FailureType.NETWORK_ERROR,
        )
    return JoinError(
        JoinReason.REFUSED,
        f"the controller refused to enrol this laptop ({reason or name}): "
        f"{safe or 'no detail'}",
        controller_reason=reason,
    )


def _call(obj: Any, method: str) -> Any:
    fn = getattr(obj, method, None)
    return fn() if callable(fn) else None


def _check_response(
    response: enroll_pb2.EnrollResponse,
    pinned_ca: x509.Certificate,
    identity: NodeIdentity,
    now: datetime,
) -> tuple[x509.Certificate, FabricId]:
    """Raises :class:`pki.PkiError` on the first mismatch."""
    ca = pki.cert_from_pem(bytes(response.ca_cert_pem))
    cert = pki.cert_from_pem(bytes(response.node_cert_pem))
    fabric_id = pki.parse_fabric_id(pinned_ca)
    if pki.cert_fingerprint(ca) != pki.cert_fingerprint(pinned_ca):
        raise pki.PkiError(pki.PkiReason.NOT_ISSUED_BY_CA, "another CA came back")
    _check_ours(cert, pinned_ca, identity, fabric_id)
    if response.node_id != identity.node_id:
        raise pki.PkiError(pki.PkiReason.NODE_MISMATCH, "another node_id")
    if response.fabric_id != fabric_id:
        raise pki.PkiError(pki.PkiReason.FABRIC_MISMATCH, "another fabric_id")
    pki.check_validity(cert, now)
    return cert, fabric_id


def _check_ours(
    cert: x509.Certificate,
    ca: x509.Certificate,
    identity: NodeIdentity,
    fabric_id: FabricId,
) -> None:
    """``cert`` was issued by ``ca``, holds our key and names our node."""
    pki.verify_issued_by(cert, ca)
    if not pki.public_keys_equal(identity.private_key.public_key(), cert.public_key()):
        raise pki.PkiError(
            pki.PkiReason.KEY_MISMATCH, "the certificate is for another key"
        )
    if pki.parse_node_id(cert, fabric_id=fabric_id) != identity.node_id:
        raise pki.PkiError(
            pki.PkiReason.NODE_MISMATCH, "the certificate names another node"
        )


def check_issued(
    response: enroll_pb2.EnrollResponse,
    *,
    pinned_ca: x509.Certificate,
    identity: NodeIdentity,
    now: datetime | None = None,
) -> tuple[x509.Certificate, FabricId]:
    """Verify what the controller returned (see module docstring, step 6)."""
    try:
        return _check_response(response, pinned_ca, identity, now or datetime.now(UTC))
    except pki.PkiError as exc:
        raise JoinError(
            JoinReason.BAD_RESPONSE,
            f"the controller returned a certificate this laptop cannot accept "
            f"({exc.reason.value}: {exc.message}); nothing was stored",
        ) from None


# --------------------------------------------------------------------- storage


def _paths(directory: Path) -> tuple[Path, Path]:
    return directory / NODE_CERT_FILE_NAME, directory / CA_CERT_FILE_NAME


def is_enrolled(directory: Path) -> bool:
    node_crt, _ = _paths(directory)
    return node_crt.exists() or node_crt.is_symlink()


def store(
    identity: NodeIdentity,
    node_cert: x509.Certificate,
    ca_cert: x509.Certificate,
    *,
    force: bool,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
) -> Path:
    """Write ``ca.crt`` then ``node.crt`` beside ``node.key`` (see module doc).

    Both new files are first written **in full** to private temporary files
    beside the targets (``0600`` / owner-only ACL, fsynced); a failure there
    removes them and leaves the old enrolment exactly as it was (D153 item 2).
    Only then is the old ``node.crt`` removed and the new pair swapped in with
    ``os.replace`` (``ca.crt`` first) and the folder entry fsynced. An
    interruption inside that last step leaves "not enrolled" (``join`` again),
    never an old certificate next to a new CA.
    """
    node_crt, ca_crt = _paths(identity.path)
    with locked_identity(identity.path, lock_timeout_s=lock_timeout_s):
        if is_enrolled(identity.path) and not force:
            raise JoinError(
                JoinReason.ALREADY_ENROLLED,
                "this laptop is already enrolled (meshforge-agent identity "
                "show); use --force to replace the enrolment",
                failure_type=FailureType.CONFLICT,
            )
        staged: list[Path] = []
        try:
            ca_tmp = _stage(ca_crt, pki.cert_to_pem(ca_cert), staged)
            node_tmp = _stage(node_crt, pki.cert_to_pem(node_cert), staged)
            # Everything that can realistically fail has happened; from here on
            # an interruption leaves "not enrolled", never old+new mixed.
            if is_enrolled(identity.path):
                node_crt.unlink()
            os.replace(ca_tmp, ca_crt)
            os.replace(node_tmp, node_crt)
            staged.clear()
            _fsync_dir(identity.path)
        except (OSError, PrivateFileError) as exc:
            message = (
                exc.message
                if isinstance(exc, PrivateFileError)
                else f"cannot write the enrolment in {identity.path}: "
                f"{exc.strerror or exc}"
            )
            raise JoinError(
                JoinReason.STORAGE, message, failure_type=FailureType.CONFIG_ERROR
            ) from None
        finally:
            for leftover in staged:
                with contextlib.suppress(OSError):
                    leftover.unlink()
    return node_crt


def replace_node_cert(
    identity: NodeIdentity,
    node_cert: x509.Certificate,
    *,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
) -> Path:
    """V0.7 G4 (D140): swap in a renewed ``node.crt`` (same key, same CA).

    Under the identity lock, the new certificate is written in full to a
    private temporary file beside ``node.crt`` (the :func:`store` staging),
    then moved over it with one ``os.replace`` and the folder entry fsynced:
    a crash leaves the old certificate or the new one, both valid for the key.
    Refuses (``not_enrolled``) when there is no enrolment to renew."""
    node_crt, _ca_crt = _paths(identity.path)
    with locked_identity(identity.path, lock_timeout_s=lock_timeout_s):
        if not is_enrolled(identity.path):
            raise JoinError(
                JoinReason.NOT_ENROLLED,
                "this laptop is not enrolled; nothing to renew",
                failure_type=FailureType.CONFIG_ERROR,
            )
        staged: list[Path] = []
        try:
            tmp = _stage(node_crt, pki.cert_to_pem(node_cert), staged)
            os.replace(tmp, node_crt)
            staged.clear()
            _fsync_dir(identity.path)
        except (OSError, PrivateFileError) as exc:
            message = (
                exc.message
                if isinstance(exc, PrivateFileError)
                else f"cannot write the renewed certificate in {identity.path}: "
                f"{exc.strerror or exc}"
            )
            raise JoinError(
                JoinReason.STORAGE, message, failure_type=FailureType.CONFIG_ERROR
            ) from None
        finally:
            for leftover in staged:
                with contextlib.suppress(OSError):
                    leftover.unlink()
    return node_crt


def _check_renewed(
    node_cert_pem: bytes,
    ca_cert_pem: bytes,
    current: Enrollment,
    key: ec.EllipticCurvePrivateKey,
    now: datetime | None,
) -> x509.Certificate:
    """Raises :class:`pki.PkiError` on the first mismatch."""
    ca = pki.cert_from_pem(ca_cert_pem)
    cert = pki.cert_from_pem(node_cert_pem)
    if pki.cert_fingerprint(ca) != current.ca_fingerprint:
        raise pki.PkiError(pki.PkiReason.NOT_ISSUED_BY_CA, "another CA came back")
    pki.verify_issued_by(cert, current.ca_cert)
    if not pki.public_keys_equal(key.public_key(), cert.public_key()):
        raise pki.PkiError(
            pki.PkiReason.KEY_MISMATCH, "the certificate is for another key"
        )
    if pki.parse_node_id(cert, fabric_id=current.fabric_id) != current.node_id:
        raise pki.PkiError(
            pki.PkiReason.NODE_MISMATCH, "the certificate names another node"
        )
    pki.check_validity(cert, now or datetime.now(UTC))
    return cert


def check_renewed(
    node_cert_pem: bytes,
    ca_cert_pem: bytes,
    *,
    current: Enrollment,
    key: ec.EllipticCurvePrivateKey,
    now: datetime | None = None,
) -> x509.Certificate:
    """V0.7 G4: what ``RenewCertificate`` returned, checked like a join's
    answer: the CA is the pinned one (byte-for-byte fingerprint), the
    certificate is issued by it, certifies ``key`` (the key the CSR was
    signed with), names this node in this fabric and is valid now. Raises
    ``bad_response`` (nothing is stored)."""
    try:
        cert = _check_renewed(node_cert_pem, ca_cert_pem, current, key, now)
    except pki.PkiError as exc:
        raise JoinError(
            JoinReason.BAD_RESPONSE,
            f"the controller returned a renewed certificate this laptop cannot "
            f"accept ({exc.reason.value}: {exc.message}); nothing was stored",
        ) from None
    return cert


def _stage(target: Path, data: bytes, staged: list[Path]) -> Path:
    """``data`` in a new private temporary file beside ``target`` (fsynced)."""
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    staged.append(tmp)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    protect_private(tmp)
    return tmp


def _fsync_dir(directory: Path) -> None:
    """Persist the renames (POSIX; Windows cannot open a folder for fsync)."""
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_pair(
    identity: NodeIdentity,
) -> tuple[x509.Certificate, x509.Certificate, FabricId]:
    node_crt, ca_crt = _paths(identity.path)
    cert = pki.read_cert(node_crt, what="node certificate", private=True)
    ca = pki.read_cert(ca_crt, what="fabric CA certificate", private=True)
    if not _is_self_signed_ca(ca):
        raise pki.PkiError(pki.PkiReason.NOT_A_CA, "ca.crt is not a CA")
    fabric_id = pki.parse_fabric_id(ca)
    _check_ours(cert, ca, identity, fabric_id)
    return cert, ca, fabric_id


def load(identity: NodeIdentity) -> Enrollment | None:
    """The stored enrolment, verified; ``None`` when this laptop never joined.

    A present but unusable enrolment (a missing ``ca.crt``, loose permissions, a
    symlink or FIFO, a certificate for another key / node / CA) is
    ``invalid_enrollment`` with the remedy. An **expired** certificate loads (the
    caller decides; ``identity show`` prints it, G4 renews before it happens).
    """
    if not is_enrolled(identity.path):
        return None
    try:
        cert, ca, fabric_id = _read_pair(identity)
    except pki.PkiError as exc:
        raise JoinError(
            JoinReason.INVALID_ENROLLMENT,
            f"the enrolment in {identity.path} is unusable ({exc.reason.value}: "
            f"{exc.message}); re-join: meshforge-agent join --force (with a new "
            "code)",
            failure_type=FailureType.CONFIG_ERROR,
        ) from None
    return Enrollment(
        node_cert=cert,
        ca_cert=ca,
        fabric_id=fabric_id,
        node_id=identity.node_id,
        path=identity.path,
    )


# ------------------------------------------------------------------------ join


@dataclass(frozen=True, slots=True)
class JoinResult:
    enrollment: Enrollment
    address: str


def join(
    identity: NodeIdentity,
    code_text: str,
    *,
    force: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    host: str | None = None,
) -> JoinResult:
    """The whole flow of the module docstring. Blocking (run it in a thread
    from async code).

    ``host`` (V0.17 G3, the console's "pick a controller found on this
    network"): connect to this address instead of the code's - for a
    controller whose LAN address changed since the code was made. The code's
    port, token and **CA pin** are unchanged, so a wrong pick is
    ``pin_mismatch`` before anything secret is sent."""
    try:
        code = join_code.decode(code_text)
    except join_code.JoinCodeError as exc:
        raise JoinError(
            JoinReason.BAD_CODE, exc.message, failure_type=FailureType.INVALID_INPUT
        ) from None
    if host is not None and host.strip():
        code = dataclasses.replace(code, host=host.strip().strip("[]").lower())
    if is_enrolled(identity.path) and not force:
        raise JoinError(
            JoinReason.ALREADY_ENROLLED,
            "this laptop is already enrolled (meshforge-agent identity show); use "
            "--force to replace the enrolment - the code was not used",
            failure_type=FailureType.CONFLICT,
        )
    presented = fetch_presented_ca(code.host, code.port, timeout_s=timeout_s)
    ca = pin_ca(presented, code.ca_pin)  # nothing secret has been sent yet
    fabric_id = pki.parse_fabric_id(ca)
    csr = pki.build_csr(
        identity.private_key, fabric_id=fabric_id, node_id=identity.node_id
    )
    log.info(
        "agent.join.pinned",
        node_id=identity.node_id,
        fabric_id=fabric_id,
        ca_fingerprint=pki.short_fingerprint(ca),
        controller=code.address,
    )
    response = enroll(code.address, ca, code.token, csr, timeout_s=timeout_s)
    cert, fabric_id = check_issued(response, pinned_ca=ca, identity=identity)
    store(identity, cert, ca, force=force)
    enrollment = load(identity)
    if enrollment is None:  # pragma: no cover - just written under the lock
        raise JoinError(JoinReason.STORAGE, "the enrolment vanished after writing")
    log.info(
        "agent.join.enrolled",
        node_id=identity.node_id,
        fabric_id=fabric_id,
        cert_fingerprint=pki.short_fingerprint(cert),
        not_after=cert.not_valid_after_utc.isoformat(),
    )
    return JoinResult(enrollment=enrollment, address=code.address)
