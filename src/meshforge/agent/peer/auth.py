"""Who may read what from this node's peer blob server (V0.5 D98).

:class:`PeerAuthorizer` checks a ``Bearer`` peer token with the controller's
public key from the agent's **current** session (``HelloAck.peer_token_key``;
until the first accepted Hello there is no key and every request is refused
``503``), that the token names this node as holder, that it has not expired, and
that the requested digest is the token's tree **or a member of it**. The tree's
manifest is read from this node's own CAS and parsed (hostile input - it is
verified against its id) in a worker thread; the member set is built in that
thread too and kept in a bounded LRU (by entries), so the event loop never
builds a big set and a repeated check is a dictionary lookup. No manifest in the
CAS means ``404``: this node cannot vouch for membership.

:meth:`PeerAuthorizer.authorize_probe` checks a **probe** token (V0.6 G4,
D111) for the ``/v1/probe`` route instead: same key, same holder rule, a
different token kind (a blob token is refused there and vice versa).
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from meshforge.agent.cas.store import AgentCas
from meshforge.common.hashing import Digest
from meshforge.common.peer_token import (
    PeerTokenClaims,
    PeerTokenError,
    ProbeTokenClaims,
    load_public_key,
    verify_peer_token,
    verify_probe_token,
)
from meshforge.common.tree import TreeError, TreeLimits, parse_tree

__all__ = ["AuthResult", "PeerAuthorizer"]

_NOT_A_TREE: Final = frozenset[str]()


@dataclass(frozen=True, slots=True)
class AuthResult:
    """``status`` 0 = allowed (``claims`` set); else the HTTP status to answer."""

    status: int
    reason: str
    claims: PeerTokenClaims | None = None
    probe: ProbeTokenClaims | None = None
    """Set instead of ``claims`` for an allowed probe (D111)."""


def _members(data: bytes, tree_id: Digest, limits: TreeLimits) -> frozenset[str]:
    """Parse + build the member set in the calling (worker) thread."""
    tree = parse_tree(data, expected=tree_id, limits=limits)
    return frozenset(e.sha256 for e in tree.files())


class PeerAuthorizer:
    """See module docstring. One per agent process (event-loop owned)."""

    def __init__(
        self,
        cas: AgentCas,
        *,
        node_id: str,
        cache_entries: int = 200_000,
        wall_clock: Callable[[], float] = time.time,
        tree_limits: TreeLimits | None = None,
    ) -> None:
        self._cas = cas
        self._node_id = node_id
        self._cache_entries = cache_entries
        self._wall = wall_clock
        self._limits = tree_limits or TreeLimits()
        self._key: Ed25519PublicKey | None = None
        self._trees: OrderedDict[str, frozenset[str]] = OrderedDict()
        self._weight = 0
        self.trees_parsed = 0

    # ------------------------------------------------------------------ key

    def set_key(self, raw: bytes) -> bool:
        """Install the key of a newly accepted session; False = absent/invalid
        (the server then refuses everything until a session sends a good one)."""
        if not raw:
            self._key = None
            return False
        try:
            self._key = load_public_key(raw)
        except PeerTokenError:
            self._key = None
            return False
        return True

    @property
    def ready(self) -> bool:
        return self._key is not None

    # ---------------------------------------------------------------- check

    async def authorize(self, authorization: str | None, digest: Digest) -> AuthResult:
        key = self._key
        if key is None:
            return AuthResult(503, "no_key")
        if not authorization:
            return AuthResult(401, "missing")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return AuthResult(401, "missing")
        try:
            claims = verify_peer_token(
                key, token.strip(), holder=self._node_id, now=self._wall()
            )
        except PeerTokenError as exc:
            return AuthResult(403, exc.reason)
        if digest == claims.tree:
            return AuthResult(0, "tree", claims)
        members = await self._tree_members(claims.tree)
        if members is None:
            return AuthResult(404, "no_tree")
        if digest.hex not in members:
            return AuthResult(403, "not_in_tree")
        return AuthResult(0, "member", claims)

    def authorize_probe(self, authorization: str | None) -> AuthResult:
        """A ``/v1/probe`` request (D111): a valid probe token for this node."""
        key = self._key
        if key is None:
            return AuthResult(503, "no_key")
        if not authorization:
            return AuthResult(401, "missing")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return AuthResult(401, "missing")
        try:
            claims = verify_probe_token(
                key, token.strip(), holder=self._node_id, now=self._wall()
            )
        except PeerTokenError as exc:
            return AuthResult(403, exc.reason)
        return AuthResult(0, "probe", probe=claims)

    async def _tree_members(self, tree_id: Digest) -> frozenset[str] | None:
        """Members of ``tree_id`` (None = its manifest is not in this CAS)."""
        hit = self._trees.get(tree_id.hex)
        if hit is not None:
            self._trees.move_to_end(tree_id.hex)
            return hit
        store = self._cas.store
        async with self._cas.holding(tree_id):
            hit = self._trees.get(tree_id.hex)
            if hit is not None:
                return hit
            size = store.size(tree_id)
            if size is None:
                return None
            if size > self._limits.max_manifest_bytes:
                members = _NOT_A_TREE
            else:
                try:
                    data = await asyncio.to_thread(store.path_for(tree_id).read_bytes)
                    members = await asyncio.to_thread(
                        _members, data, tree_id, self._limits
                    )
                    self.trees_parsed += 1
                except FileNotFoundError:
                    return None
                except (TreeError, OSError):
                    members = _NOT_A_TREE
            self._remember(tree_id.hex, members)
            return members

    def _remember(self, key: str, members: frozenset[str]) -> None:
        old = self._trees.pop(key, None)
        if old is not None:
            self._weight -= 1 + len(old)
        self._trees[key] = members
        self._weight += 1 + len(members)
        while self._weight > self._cache_entries and len(self._trees) > 1:
            _key, evicted = self._trees.popitem(last=False)
            self._weight -= 1 + len(evicted)
