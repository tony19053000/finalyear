"""V0.6 G4 (D111): probe tokens - a second token kind signed with the peer key.

A probe token opens only ``/v1/probe``; a blob token is refused there and a
probe token is refused for blobs; lifetime <= 60 s; holder bound; tampering
and malformed payloads refused before parsing (signature first)."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.common.hashing import Digest
from meshforge.common.peer_token import (
    PROBE_MAX_TTL_S,
    SKEW_S,
    PeerTokenError,
    sign_peer_token,
    sign_probe_token,
    verify_peer_token,
    verify_probe_token,
)

A = "node_01J00000000000000000000000"
B = "node_01J00000000000000000000001"
ATT = "att_01J00000000000000000000000"
NOW = 1_800_000_000.0
KEY = Ed25519PrivateKey.generate()
PUB = KEY.public_key()


def _reason(exc: pytest.ExceptionInfo[PeerTokenError]) -> str:
    return exc.value.reason


def test_round_trip_names_requester_and_holder() -> None:
    token = sign_probe_token(KEY, requester=A, holder=B, now=NOW)
    claims = verify_probe_token(PUB, token, holder=B, now=NOW + 1)
    assert (claims.requester, claims.holder) == (A, B)
    assert claims.exp == int(NOW) + PROBE_MAX_TTL_S


@pytest.mark.parametrize("ttl", [0, 61, 600])
def test_ttl_is_bounded_to_60_s(ttl: int) -> None:
    with pytest.raises(ValueError, match="ttl"):
        sign_probe_token(KEY, requester=A, holder=B, now=NOW, ttl_s=ttl)


def test_expiry_with_skew() -> None:
    token = sign_probe_token(KEY, requester=A, holder=B, now=NOW, ttl_s=10)
    verify_probe_token(PUB, token, holder=B, now=NOW + 10 + SKEW_S)
    with pytest.raises(PeerTokenError) as exc:
        verify_probe_token(PUB, token, holder=B, now=NOW + 10 + SKEW_S + 1)
    assert _reason(exc) == "expired"


def test_wrong_holder_refused() -> None:
    token = sign_probe_token(KEY, requester=A, holder=B, now=NOW)
    with pytest.raises(PeerTokenError) as exc:
        verify_probe_token(PUB, token, holder=A, now=NOW)
    assert _reason(exc) == "wrong_holder"


def test_a_blob_token_is_not_a_probe_token_and_vice_versa() -> None:
    blob = sign_peer_token(
        KEY,
        tree=Digest("ab" * 32),
        requester=A,
        holder=B,
        attempt_id=ATT,
        now=NOW,
    )
    with pytest.raises(PeerTokenError) as exc:
        verify_probe_token(PUB, blob, holder=B, now=NOW)
    assert _reason(exc) == "malformed"
    probe = sign_probe_token(KEY, requester=A, holder=B, now=NOW)
    with pytest.raises(PeerTokenError) as exc2:
        verify_peer_token(PUB, probe, holder=B, now=NOW)
    assert _reason(exc2) == "malformed"


def test_another_key_or_a_tampered_payload_is_refused() -> None:
    token = sign_probe_token(KEY, requester=A, holder=B, now=NOW)
    other = Ed25519PrivateKey.generate().public_key()
    with pytest.raises(PeerTokenError) as exc:
        verify_probe_token(other, token, holder=B, now=NOW)
    assert _reason(exc) == "bad_signature"
    payload, sig = token.split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=="))
    body["exp"] += 3600
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(PeerTokenError) as exc2:
        verify_probe_token(PUB, f"{forged}.{sig}", holder=B, now=NOW)
    assert _reason(exc2) == "bad_signature"


def test_a_signed_payload_of_another_kind_is_refused_by_kind() -> None:
    body = {"exp": int(NOW) + 30, "hold": B, "kind": "blob", "req": A, "v": 1}
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    token = f"{b64(payload)}.{b64(KEY.sign(payload))}"
    with pytest.raises(PeerTokenError) as exc:
        verify_probe_token(PUB, token, holder=B, now=NOW)
    assert _reason(exc) == "wrong_kind"


def test_oversized_and_garbage_tokens() -> None:
    for bad in ("", "x" * 600, "abc", "a.b"):
        with pytest.raises(PeerTokenError):
            verify_probe_token(PUB, bad, holder=B, now=NOW)
