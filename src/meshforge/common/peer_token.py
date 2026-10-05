"""Peer blob tokens: sign (controller) and verify (agent peer server) (V0.5 D98).

A peer token lets node ``req`` read, from holder ``hold``'s peer blob server, the
blobs of exactly one tree for one attempt, until ``exp``. The controller signs it
with its Ed25519 key (``<data dir>/controller/peer_signing.key``); the holder
verifies it with the public key from its *current* session's ``HelloAck`` - no
call to the controller. Both sides use this module; neither imports the other.

Wire form::

    base64url(canonical JSON {"att","exp","hold","req","tree","v":1}) "." base64url(sig)

* Canonical JSON = ``json.dumps(sort_keys=True, separators=(",", ":"))``, ASCII
  only; the verifier re-serialises the parsed claims and refuses anything that
  is not byte-identical (no duplicate keys, no whitespace, no extra fields).
* ``exp`` is wall-clock UNIX seconds. A token is refused once ``now > exp +
  skew`` and also when ``exp`` lies more than :data:`MAX_TTL_S` + skew in the
  future (a token cannot outlive the 10-minute rule even if the signer erred).
* Bounded: the whole token is at most :data:`MAX_TOKEN_CHARS` characters and the
  payload at most :data:`MAX_PAYLOAD_BYTES` bytes, checked before decoding.

Every refusal is a :class:`PeerTokenError` with a stable ``reason`` code; the
token text is never put in a message or a log line.

**Probe tokens (V0.6 G4, D111)** are a second kind with their own field set -
``{"exp","hold","kind":"probe","req","v":1}``, at most :data:`PROBE_MAX_TTL_S`
(60 s) - signed with the same key. They open only the peer server's
``/v1/probe`` route; a blob token is refused there and a probe token is refused
for blobs (different field sets: each verifier rejects the other's payload as
``malformed``).
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from meshforge.common.errors import FailureType, InvalidIdError, MeshForgeError
from meshforge.common.hashing import Digest
from meshforge.common.ids import (
    AttemptId,
    NodeId,
    validate_attempt_id,
    validate_node_id,
)

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "MAX_TOKEN_CHARS",
    "MAX_TTL_S",
    "PROBE_KIND",
    "PROBE_MAX_TTL_S",
    "PUBLIC_KEY_BYTES",
    "SKEW_S",
    "TOKEN_VERSION",
    "PeerTokenClaims",
    "PeerTokenError",
    "ProbeTokenClaims",
    "load_public_key",
    "sign_peer_token",
    "sign_probe_token",
    "verify_peer_token",
    "verify_probe_token",
]

TOKEN_VERSION: Final = 1
MAX_TTL_S: Final = 600
"""A peer token is valid for at most 10 minutes after it is issued (D88/D98)."""
SKEW_S: Final = 30
"""Wall-clock disagreement tolerated between the controller and the holder."""
MAX_TOKEN_CHARS: Final = 512
MAX_PAYLOAD_BYTES: Final = 256
PUBLIC_KEY_BYTES: Final = 32
_SIGNATURE_BYTES: Final = 64
_SIG_B64_CHARS: Final = 86  # 64 bytes, base64url without padding
_B64_ALPHABET: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)
_FIELDS: Final = frozenset({"att", "exp", "hold", "req", "tree", "v"})
_PROBE_FIELDS: Final = frozenset({"exp", "hold", "kind", "req", "v"})
PROBE_KIND: Final = "probe"
PROBE_MAX_TTL_S: Final = 60
"""A probe token is valid for at most 60 s (D111)."""
_MAX_EXP: Final = 2**40  # far beyond any real clock; keeps the JSON bounded


class PeerTokenError(MeshForgeError):
    """A peer token was refused. ``reason`` is a stable snake_case code."""

    default_failure_type = FailureType.UNAUTHORIZED

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message, details={"reason": reason})

    @property
    def reason(self) -> str:
        return str(self.details.get("reason", "unknown"))


@dataclass(frozen=True, slots=True)
class PeerTokenClaims:
    """What a peer token grants (all fields validated)."""

    tree: Digest
    requester: NodeId
    holder: NodeId
    attempt_id: AttemptId
    exp: int

    def canonical_bytes(self) -> bytes:
        body = {
            "att": str(self.attempt_id),
            "exp": self.exp,
            "hold": str(self.holder),
            "req": str(self.requester),
            "tree": str(self.tree),
            "v": TOKEN_VERSION,
        }
        return json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str, what: str) -> bytes:
    if not text or not set(text) <= _B64_ALPHABET or len(text) % 4 == 1:
        raise PeerTokenError("malformed", f"the peer token's {what} is not base64url")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        raise PeerTokenError(
            "malformed", f"the peer token's {what} is not base64url"
        ) from None
    if _b64e(raw) != text:  # non-canonical trailing bits
        raise PeerTokenError("malformed", f"the peer token's {what} is not canonical")
    return raw


def load_public_key(raw: bytes) -> Ed25519PublicKey:
    """A raw 32-byte Ed25519 public key (``HelloAck.peer_token_key``)."""
    if len(raw) != PUBLIC_KEY_BYTES:
        raise PeerTokenError(
            "bad_key", f"a peer token key is {PUBLIC_KEY_BYTES} bytes, got {len(raw)}"
        )
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        raise PeerTokenError("bad_key", "the peer token key is not valid") from None


def sign_peer_token(
    key: Ed25519PrivateKey,
    *,
    tree: Digest,
    requester: NodeId,
    holder: NodeId,
    attempt_id: AttemptId,
    now: float,
    ttl_s: int = MAX_TTL_S,
) -> str:
    """Issue a token valid until ``now + ttl_s`` (wall clock, ``ttl_s`` <= 600)."""
    if not 1 <= ttl_s <= MAX_TTL_S:
        raise ValueError(f"peer token ttl must be 1..{MAX_TTL_S} s, got {ttl_s}")
    claims = PeerTokenClaims(
        tree=tree,
        requester=validate_node_id(requester),
        holder=validate_node_id(holder),
        attempt_id=validate_attempt_id(attempt_id),
        exp=int(now) + ttl_s,
    )
    payload = claims.canonical_bytes()
    return _b64e(payload) + "." + _b64e(key.sign(payload))


def _parse_claims(payload: bytes) -> PeerTokenClaims:
    try:
        body = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise PeerTokenError(
            "malformed", "the peer token payload is not JSON"
        ) from None
    if not isinstance(body, dict) or set(body) != _FIELDS:
        raise PeerTokenError("malformed", "the peer token has the wrong fields")
    if body["v"] != TOKEN_VERSION or isinstance(body["v"], bool):
        raise PeerTokenError("bad_version", "unsupported peer token version")
    exp = body["exp"]
    if isinstance(exp, bool) or not isinstance(exp, int) or not 0 < exp < _MAX_EXP:
        raise PeerTokenError("malformed", "the peer token expiry is not valid")
    try:
        claims = PeerTokenClaims(
            tree=Digest.parse(body["tree"]),
            requester=validate_node_id(body["req"]),
            holder=validate_node_id(body["hold"]),
            attempt_id=validate_attempt_id(body["att"]),
            exp=exp,
        )
    except (ValueError, InvalidIdError):
        raise PeerTokenError(
            "malformed", "the peer token names an invalid tree or id"
        ) from None
    if claims.canonical_bytes() != payload:
        raise PeerTokenError("malformed", "the peer token payload is not canonical")
    return claims


def verify_peer_token(
    public_key: Ed25519PublicKey,
    token: str,
    *,
    holder: str,
    now: float,
) -> PeerTokenClaims:
    """Check size, shape, signature, holder and expiry; return the claims.

    The signature is checked before the payload is parsed, so an attacker never
    reaches the JSON parser without the controller's key. Tree membership of a
    requested digest is the caller's check (it needs the holder's CAS).
    """
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
        raise PeerTokenError("oversized", "the peer token is too long")
    payload_b64, sep, sig_b64 = token.partition(".")
    if not sep or len(sig_b64) != _SIG_B64_CHARS:
        raise PeerTokenError("malformed", "the peer token is not <payload>.<sig>")
    if len(payload_b64) > (MAX_PAYLOAD_BYTES * 4 + 2) // 3:
        raise PeerTokenError("oversized", "the peer token payload is too long")
    payload = _b64d(payload_b64, "payload")
    signature = _b64d(sig_b64, "signature")
    if len(signature) != _SIGNATURE_BYTES:
        raise PeerTokenError("malformed", "the peer token signature has a bad size")
    try:
        public_key.verify(signature, payload)
    except InvalidSignature:
        raise PeerTokenError(
            "bad_signature", "the peer token signature is not valid"
        ) from None
    claims = _parse_claims(payload)
    if claims.holder != holder:
        raise PeerTokenError("wrong_holder", "the peer token is for another holder")
    if now > claims.exp + SKEW_S:
        raise PeerTokenError("expired", "the peer token has expired")
    if claims.exp > now + MAX_TTL_S + SKEW_S:
        raise PeerTokenError("too_long", "the peer token lives longer than allowed")
    return claims


# --------------------------------------------------------------------- probes


@dataclass(frozen=True, slots=True)
class ProbeTokenClaims:
    """What a probe token grants: ``requester`` may call ``holder``'s
    ``/v1/probe`` until ``exp`` (D111)."""

    requester: NodeId
    holder: NodeId
    exp: int

    def canonical_bytes(self) -> bytes:
        body = {
            "exp": self.exp,
            "hold": str(self.holder),
            "kind": PROBE_KIND,
            "req": str(self.requester),
            "v": TOKEN_VERSION,
        }
        return json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")


def sign_probe_token(
    key: Ed25519PrivateKey,
    *,
    requester: NodeId | str,
    holder: NodeId | str,
    now: float,
    ttl_s: int = PROBE_MAX_TTL_S,
) -> str:
    """A probe token valid until ``now + ttl_s`` (``ttl_s`` <= 60)."""
    if not 1 <= ttl_s <= PROBE_MAX_TTL_S:
        raise ValueError(f"probe token ttl must be 1..{PROBE_MAX_TTL_S} s, got {ttl_s}")
    claims = ProbeTokenClaims(
        requester=validate_node_id(requester),
        holder=validate_node_id(holder),
        exp=int(now) + ttl_s,
    )
    payload = claims.canonical_bytes()
    return _b64e(payload) + "." + _b64e(key.sign(payload))


def _parse_probe_claims(payload: bytes) -> ProbeTokenClaims:
    try:
        body = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise PeerTokenError(
            "malformed", "the probe token payload is not JSON"
        ) from None
    if not isinstance(body, dict) or set(body) != _PROBE_FIELDS:
        raise PeerTokenError("malformed", "the probe token has the wrong fields")
    if body["kind"] != PROBE_KIND:
        raise PeerTokenError("wrong_kind", "the token is not a probe token")
    if body["v"] != TOKEN_VERSION or isinstance(body["v"], bool):
        raise PeerTokenError("bad_version", "unsupported probe token version")
    exp = body["exp"]
    if isinstance(exp, bool) or not isinstance(exp, int) or not 0 < exp < _MAX_EXP:
        raise PeerTokenError("malformed", "the probe token expiry is not valid")
    try:
        claims = ProbeTokenClaims(
            requester=validate_node_id(body["req"]),
            holder=validate_node_id(body["hold"]),
            exp=exp,
        )
    except (ValueError, InvalidIdError):
        raise PeerTokenError(
            "malformed", "the probe token names an invalid id"
        ) from None
    if claims.canonical_bytes() != payload:
        raise PeerTokenError("malformed", "the probe token payload is not canonical")
    return claims


def verify_probe_token(
    public_key: Ed25519PublicKey,
    token: str,
    *,
    holder: str,
    now: float,
) -> ProbeTokenClaims:
    """Size, shape, signature (first), kind, holder and expiry of a probe
    token; its claims. Refusals as :func:`verify_peer_token`."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
        raise PeerTokenError("oversized", "the probe token is too long")
    payload_b64, sep, sig_b64 = token.partition(".")
    if not sep or len(sig_b64) != _SIG_B64_CHARS:
        raise PeerTokenError("malformed", "the probe token is not <payload>.<sig>")
    if len(payload_b64) > (MAX_PAYLOAD_BYTES * 4 + 2) // 3:
        raise PeerTokenError("oversized", "the probe token payload is too long")
    payload = _b64d(payload_b64, "payload")
    signature = _b64d(sig_b64, "signature")
    if len(signature) != _SIGNATURE_BYTES:
        raise PeerTokenError("malformed", "the probe token signature has a bad size")
    try:
        public_key.verify(signature, payload)
    except InvalidSignature:
        raise PeerTokenError(
            "bad_signature", "the probe token signature is not valid"
        ) from None
    claims = _parse_probe_claims(payload)
    if claims.holder != holder:
        raise PeerTokenError("wrong_holder", "the probe token is for another holder")
    if now > claims.exp + SKEW_S:
        raise PeerTokenError("expired", "the probe token has expired")
    if claims.exp > now + PROBE_MAX_TTL_S + SKEW_S:
        raise PeerTokenError("too_long", "the probe token lives longer than allowed")
    return claims
