"""V0.7 G3 tester round 2 (D155 T-2): the requester binding is exact.

Derived from the criterion "a token captured by another enrolled node is
useless to it" - so no near-miss spelling of the right node id may pass, an
empty/absent identity fails closed on both routes, and a refusal for the wrong
requester never reveals whether this node holds the tree.
"""

from __future__ import annotations

import base64
import json
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

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

TREE = Digest("ab" * 32)


def _rig(tmp: Path) -> tuple[PeerAuthorizer, Ed25519PrivateKey, str]:
    holder = str(new_node_id())
    key = Ed25519PrivateKey.generate()
    auth = PeerAuthorizer(AgentCas.at(tmp / "cas"), node_id=holder)
    assert auth.set_key(
        key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    )
    return auth, key, holder


def _blob(key: Ed25519PrivateKey, holder: str, req: str, *, age: float = 0) -> str:
    return "Bearer " + sign_peer_token(
        key,
        tree=TREE,
        requester=req,  # type: ignore[arg-type]
        holder=holder,  # type: ignore[arg-type]
        attempt_id=new_attempt_id(),
        now=time.time() - age,
    )


def _probe(key: Ed25519PrivateKey, holder: str, req: str) -> str:
    return "Bearer " + sign_probe_token(
        key, requester=req, holder=holder, now=time.time()
    )


def _near_misses(node: str) -> dict[str, str]:
    return {
        "lowercase": node.lower(),
        "uppercase": node.upper(),
        "trailing_space": node + " ",
        "leading_space": " " + node,
        "trailing_newline": node + "\n",
        "trailing_nul": node + "\x00",
        "truncated": node[:-1],
        "extended": node + "0",
        "last_char_changed": node[:-1] + ("0" if node[-1] != "0" else "1"),
        "unicode_homoglyph": node.replace("0", "\u041e", 1)
        if "0" in node
        else node + "\u200b",
        "fullwidth": "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in node),
        "other_node": str(new_node_id()),
        "prefix_only": node.split("_")[0] + "_",
    }


@pytest.mark.parametrize(
    "variant", sorted(_near_misses("node_01J0HARNESS000000000000000"))
)
async def test_no_near_miss_spelling_of_the_requester_passes(
    tmp_path: Path, variant: str
) -> None:
    auth, key, holder = _rig(tmp_path)
    requester = str(new_node_id())
    spelling = _near_misses(requester)[variant]
    if spelling == requester:  # e.g. ids that are already uppercase
        pytest.skip("spelling equals the id for this ULID")
    blob = await auth.authorize(
        _blob(key, holder, requester), TREE, expected_requester=spelling
    )
    probe = auth.authorize_probe(
        _probe(key, holder, requester), expected_requester=spelling
    )
    assert (blob.status, blob.reason) == (403, WRONG_REQUESTER)
    assert (probe.status, probe.reason) == (403, WRONG_REQUESTER)


async def test_the_exact_requester_passes_on_both_routes(tmp_path: Path) -> None:
    auth, key, holder = _rig(tmp_path)
    requester = str(new_node_id())
    blob = await auth.authorize(
        _blob(key, holder, requester), TREE, expected_requester=requester
    )
    probe = auth.authorize_probe(
        _probe(key, holder, requester), expected_requester=requester
    )
    assert blob.status == 0 and blob.reason == "tree"
    assert probe.status == 0


async def test_empty_identity_fails_closed_on_both_routes(tmp_path: Path) -> None:
    auth, key, holder = _rig(tmp_path)
    requester = str(new_node_id())
    blob = await auth.authorize(
        _blob(key, holder, requester), TREE, expected_requester=""
    )
    probe = auth.authorize_probe(_probe(key, holder, requester), expected_requester="")
    assert (blob.status, blob.reason) == (403, WRONG_REQUESTER)
    assert (probe.status, probe.reason) == (403, WRONG_REQUESTER)


async def test_binding_does_not_turn_other_refusals_into_wrong_requester(
    tmp_path: Path,
) -> None:
    """Order: signature/holder/expiry first (a stale or foreign token says
    why), then the requester, then the CAS. Missing header stays 401."""
    auth, key, holder = _rig(tmp_path)
    requester, other = str(new_node_id()), str(new_node_id())
    expired = await auth.authorize(
        _blob(key, holder, requester, age=3600), TREE, expected_requester=other
    )
    assert (expired.status, expired.reason) == (403, "expired")
    none = await auth.authorize(None, TREE, expected_requester=other)
    assert none.status == 401
    foreign = Ed25519PrivateKey.generate()
    forged = await auth.authorize(
        _blob(foreign, holder, requester), TREE, expected_requester=requester
    )
    assert (forged.status, forged.reason) == (403, "bad_signature")
    other_holder = await auth.authorize(
        _blob(key, str(new_node_id()), requester), TREE, expected_requester=requester
    )
    assert (other_holder.status, other_holder.reason) == (403, "wrong_holder")


async def test_wrong_requester_is_the_same_answer_for_held_and_unheld_trees(
    tmp_path: Path,
) -> None:
    """No oracle: for the thief, a tree this node holds and one it does not give
    the identical refusal (and a member digest of either)."""
    auth, key, holder = _rig(tmp_path)
    requester, thief = str(new_node_id()), str(new_node_id())
    answers = set()
    for tree in (TREE, Digest("cd" * 32)):
        token = "Bearer " + sign_peer_token(
            key,
            tree=tree,
            requester=requester,  # type: ignore[arg-type]
            holder=holder,  # type: ignore[arg-type]
            attempt_id=new_attempt_id(),
            now=time.time(),
        )
        for digest in (tree, Digest("ef" * 32)):
            r = await auth.authorize(token, digest, expected_requester=thief)
            answers.add((r.status, r.reason))
    assert answers == {(403, WRONG_REQUESTER)}


def _hand_signed(key: Ed25519PrivateKey, body: dict[str, object]) -> str:
    payload = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    enc = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()  # noqa: E731
    return "Bearer " + enc(payload) + "." + enc(key.sign(payload))


@pytest.mark.parametrize("req", [None, "", 7, "node_x", ["a"]])
async def test_a_validly_signed_token_with_a_missing_or_invalid_req_is_refused(
    tmp_path: Path, req: object
) -> None:
    """Even with the controller's key, a claim set without a usable ``req`` is
    never served - not even to an unbound (insecure-dev) server."""
    auth, key, holder = _rig(tmp_path)
    body: dict[str, object] = {
        "att": str(new_attempt_id()),
        "exp": int(time.time()) + 30,
        "hold": holder,
        "tree": str(TREE),
        "v": 1,
    }
    if req is not None:
        body["req"] = req
    header = _hand_signed(key, body)
    for expected in (None, "", str(new_node_id())):
        result = await auth.authorize(header, TREE, expected_requester=expected)
        assert result.status == 403, (req, expected, result)
        assert result.reason != "tree"
