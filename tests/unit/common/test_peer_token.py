"""V0.5 G4 D98: peer blob tokens - sign, verify and every refusal reason."""

from __future__ import annotations

import base64
import json
import pickle

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.common.peer_token import (
    MAX_PAYLOAD_BYTES,
    MAX_TOKEN_CHARS,
    MAX_TTL_S,
    SKEW_S,
    PeerTokenClaims,
    PeerTokenError,
    load_public_key,
    sign_peer_token,
    verify_peer_token,
)

pytestmark = pytest.mark.unit

NOW = 1_800_000_000.0
TREE: Digest = sha256_bytes(b"tree")


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _issue(key: Ed25519PrivateKey, holder: str, **kw: object) -> str:
    args: dict[str, object] = {
        "tree": TREE,
        "requester": new_node_id(),
        "holder": holder,
        "attempt_id": new_attempt_id(),
        "now": NOW,
    }
    args.update(kw)
    return sign_peer_token(key, **args)  # type: ignore[arg-type]


def _reason(key: Ed25519PrivateKey, token: str, holder: str, now: float = NOW) -> str:
    with pytest.raises(PeerTokenError) as info:
        verify_peer_token(key.public_key(), token, holder=holder, now=now)
    assert info.value.failure_type is FailureType.UNAUTHORIZED
    assert len(token) < 4 or token not in info.value.message
    return info.value.reason


def test_round_trip(key: Ed25519PrivateKey) -> None:
    holder = new_node_id()
    requester = new_node_id()
    attempt = new_attempt_id()
    token = sign_peer_token(
        key, tree=TREE, requester=requester, holder=holder, attempt_id=attempt, now=NOW
    )
    claims = verify_peer_token(key.public_key(), token, holder=holder, now=NOW)
    assert claims == PeerTokenClaims(
        tree=TREE,
        requester=requester,
        holder=holder,
        attempt_id=attempt,
        exp=int(NOW) + MAX_TTL_S,
    )
    assert len(token) <= MAX_TOKEN_CHARS


def test_public_key_from_raw_bytes(key: Ed25519PrivateKey) -> None:
    from cryptography.hazmat.primitives import serialization

    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    holder = new_node_id()
    token = _issue(key, holder)
    verify_peer_token(load_public_key(raw), token, holder=holder, now=NOW)
    for bad in (b"", raw[:31], raw + b"x"):
        with pytest.raises(PeerTokenError) as info:
            load_public_key(bad)
        assert info.value.reason == "bad_key"


def test_wrong_key(key: Ed25519PrivateKey) -> None:
    holder = new_node_id()
    token = _issue(Ed25519PrivateKey.generate(), holder)
    assert _reason(key, token, holder) == "bad_signature"


def test_wrong_holder(key: Ed25519PrivateKey) -> None:
    token = _issue(key, new_node_id())
    assert _reason(key, token, new_node_id()) == "wrong_holder"


def test_expiry_edges(key: Ed25519PrivateKey) -> None:
    holder = new_node_id()
    token = _issue(key, holder, ttl_s=60)
    exp = int(NOW) + 60
    verify_peer_token(key.public_key(), token, holder=holder, now=exp + SKEW_S)
    assert _reason(key, token, holder, now=exp + SKEW_S + 1) == "expired"


def test_exp_too_far_in_the_future_is_refused(key: Ed25519PrivateKey) -> None:
    holder = new_node_id()
    token = _issue(key, holder)  # exp = NOW + 600
    # A holder whose clock is behind by more than the skew sees it as too long.
    verify_peer_token(key.public_key(), token, holder=holder, now=NOW - SKEW_S)
    assert _reason(key, token, holder, now=NOW - SKEW_S - 1) == "too_long"


def test_ttl_bounds(key: Ed25519PrivateKey) -> None:
    holder = new_node_id()
    _issue(key, holder, ttl_s=MAX_TTL_S)
    with pytest.raises(ValueError, match="ttl"):
        _issue(key, holder, ttl_s=MAX_TTL_S + 1)
    with pytest.raises(ValueError, match="ttl"):
        _issue(key, holder, ttl_s=0)


def test_tampered_payload(key: Ed25519PrivateKey) -> None:
    holder = new_node_id()
    token = _issue(key, holder)
    payload_b64, _, sig = token.partition(".")
    body = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    body["tree"] = str(sha256_bytes(b"other"))
    forged = _b64(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    assert _reason(key, forged + "." + sig, holder) == "bad_signature"


def test_signed_but_non_canonical_payload_is_refused(key: Ed25519PrivateKey) -> None:
    """Even the key holder cannot get a non-canonical form accepted."""
    holder = new_node_id()
    body = {
        "v": 1,
        "tree": str(TREE),
        "req": new_node_id(),
        "hold": holder,
        "att": new_attempt_id(),
        "exp": int(NOW) + 60,
    }
    payload = json.dumps(body).encode()  # spaces, unsorted
    token = _b64(payload) + "." + _b64(key.sign(payload))
    assert _reason(key, token, holder) == "malformed"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.pop("att"),
        lambda b: b.update(extra=1),
        lambda b: b.update(exp="1"),
        lambda b: b.update(exp=True),
        lambda b: b.update(tree="sha256:XYZ"),
        lambda b: b.update(req="job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3"),
    ],
)
def test_signed_but_invalid_fields(key: Ed25519PrivateKey, mutate: object) -> None:
    holder = new_node_id()
    body: dict[str, object] = {
        "att": new_attempt_id(),
        "exp": int(NOW) + 60,
        "hold": holder,
        "req": new_node_id(),
        "tree": str(TREE),
        "v": 1,
    }
    mutate(body)  # type: ignore[operator]
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    token = _b64(payload) + "." + _b64(key.sign(payload))
    assert _reason(key, token, holder) == "malformed"


def test_bad_version(key: Ed25519PrivateKey) -> None:
    holder = new_node_id()
    body = {
        "att": new_attempt_id(),
        "exp": int(NOW) + 60,
        "hold": holder,
        "req": new_node_id(),
        "tree": str(TREE),
        "v": 2,
    }
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    token = _b64(payload) + "." + _b64(key.sign(payload))
    assert _reason(key, token, holder) == "bad_version"


def test_token_size_bound_max_and_max_plus_one(key: Ed25519PrivateKey) -> None:
    holder = new_node_id()
    sig = _b64(b"\0" * 64)
    # Exactly MAX_TOKEN_CHARS: passes the size check (refused later for shape).
    at_max = "A" * (MAX_TOKEN_CHARS - len(sig) - 1) + "." + sig
    assert len(at_max) == MAX_TOKEN_CHARS
    assert _reason(key, at_max, holder) != "oversized" or (
        len(at_max.partition(".")[0]) > (MAX_PAYLOAD_BYTES * 4 + 2) // 3
    )
    over = "A" * (MAX_TOKEN_CHARS - len(sig)) + "." + sig
    assert len(over) == MAX_TOKEN_CHARS + 1
    assert _reason(key, over, holder) == "oversized"


def test_payload_size_bound_max_and_max_plus_one(key: Ed25519PrivateKey) -> None:
    holder = new_node_id()
    limit = (MAX_PAYLOAD_BYTES * 4 + 2) // 3
    sig = _b64(b"\0" * 64)
    assert _reason(key, "A" * limit + "." + sig, holder) != "oversized"
    assert _reason(key, "A" * (limit + 1) + "." + sig, holder) == "oversized"


@pytest.mark.parametrize(
    "token",
    [
        "",
        ".",
        "abc",
        "abc.def",
        "a$b." + "A" * 86,
        "A." + "A" * 85 + "=",
        "é." + "A" * 86,
    ],
)
def test_garbage_is_malformed(key: Ed25519PrivateKey, token: str) -> None:
    assert _reason(key, token, new_node_id()) in {"malformed", "bad_signature"}


def test_non_string_token(key: Ed25519PrivateKey) -> None:
    with pytest.raises(PeerTokenError):
        verify_peer_token(key.public_key(), None, holder=new_node_id(), now=NOW)  # type: ignore[arg-type]


def test_error_pickles_with_reason() -> None:
    err = PeerTokenError("expired", "the peer token has expired")
    again = pickle.loads(pickle.dumps(err))
    assert again.reason == "expired"
