"""V0.7 G3 fix round 1 (D155 T-2): :class:`PeerAuthorizer` binds a token to an
expected requester - fail closed on an empty identity, unbound only when the
server passes ``None`` (plain HTTP, ``insecure-dev``)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from meshforge.agent.cas import AgentCas
from meshforge.agent.peer import PeerAuthorizer
from meshforge.agent.peer.auth import WRONG_REQUESTER
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.common.peer_token import sign_peer_token, sign_probe_token

pytestmark = [pytest.mark.unit, pytest.mark.asyncio, pytest.mark.security]

TREE = Digest("ab" * 32)


def _setup(tmp: Path) -> tuple[PeerAuthorizer, Ed25519PrivateKey, str]:
    holder = str(new_node_id())
    key = Ed25519PrivateKey.generate()
    auth = PeerAuthorizer(AgentCas.at(tmp / "cas"), node_id=holder)
    assert auth.set_key(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    return auth, key, holder


def _blob(key: Ed25519PrivateKey, holder: str, requester: str) -> str:
    return "Bearer " + sign_peer_token(
        key,
        tree=TREE,
        requester=requester,  # type: ignore[arg-type]
        holder=holder,  # type: ignore[arg-type]
        attempt_id=new_attempt_id(),
        now=time.time(),
    )


def _probe(key: Ed25519PrivateKey, holder: str, requester: str) -> str:
    return "Bearer " + sign_probe_token(
        key, requester=requester, holder=holder, now=time.time()
    )


@pytest.mark.parametrize(
    ("expected", "allowed"),
    [
        ("self", True),  # the token's own requester
        ("other", False),  # another enrolled node
        ("", False),  # TLS without a client identity: fail closed
        (None, True),  # unbound (plain HTTP, insecure-dev only)
    ],
)
async def test_blob_and_probe_tokens_are_bound_to_the_expected_requester(
    tmp_path: Path, expected: str | None, allowed: bool
) -> None:
    auth, key, holder = _setup(tmp_path)
    requester = str(new_node_id())
    want = {"self": requester, "other": str(new_node_id())}.get(
        expected or "", expected
    )
    blob = await auth.authorize(
        _blob(key, holder, requester), TREE, expected_requester=want
    )
    probe = auth.authorize_probe(
        _probe(key, holder, requester), expected_requester=want
    )
    for result in (blob, probe):
        if allowed:
            assert result.status == 0
        else:
            assert (result.status, result.reason) == (403, WRONG_REQUESTER)
            assert result.claims is None and result.probe is None


async def test_a_bad_token_is_refused_for_its_own_reason_before_the_binding(
    tmp_path: Path,
) -> None:
    """The binding never masks a token error (forged / wrong holder)."""
    auth, key, _holder = _setup(tmp_path)
    requester = str(new_node_id())
    wrong_holder = _blob(key, str(new_node_id()), requester)
    result = await auth.authorize(wrong_holder, TREE, expected_requester=requester)
    assert (result.status, result.reason) == (403, "wrong_holder")
