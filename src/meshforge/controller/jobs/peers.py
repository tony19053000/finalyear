"""Which peers an attempt may fetch its tree from (V0.5 D89/D98/D100).

For a tree job the dispatcher lists the controller first, then up to
``[controller.jobs] max_peer_sources`` nodes that

* are ONLINE right now (a live session in the manager),
* advertised a peer endpoint that the controller accepted (D99),
* are not the attempt's own node, and
* hold ``tree <tree_id>`` in the cache location index (D100: reported only once
  the manifest and every member blob are in their CAS), **and** their index
  rows are live for the current session (``index_live``: the session's
  snapshot completed and no gap since - not a stale node, not rows left from
  before a controller restart; G4 reviewer carry-over 1),

ordered by the most recent ``last_seen`` of that index row. Each peer gets its
own signed token (D98: this tree, this requester, this holder, this attempt,
<= 60 s since V0.7 G3, D147 - :meth:`PeerSources.remint` re-mints them while
the attempt stages). The index read is one indexed query; if the database cannot
answer, the attempt simply fetches from the controller (logged) - peers are an
optimisation, never a dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from meshforge.common.hashing import Digest
from meshforge.common.ids import validate_attempt_id, validate_node_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState

if TYPE_CHECKING:
    from meshforge.controller.cache_index import CacheLocationStore
    from meshforge.controller.jobs.store import AttemptRecord, JobRecord
    from meshforge.controller.peer_signing import PeerTokenIssuer
    from meshforge.controller.session.manager import SessionManager

__all__ = ["PeerSources"]

log = get_logger(__name__)


class PeerSources:
    """See module docstring."""

    def __init__(
        self,
        manager: SessionManager,
        cache: CacheLocationStore,
        issuer: PeerTokenIssuer,
        *,
        max_sources: int,
        index_live: Callable[[str, str], bool],
        scheme: str = "https",
    ) -> None:
        """``scheme``: ``https`` (V0.7 G3: peers serve TLS with their node
        certificate) or ``http`` in ``insecure-dev``."""
        self._manager = manager
        self._scheme = scheme
        self._index_live = index_live
        self._cache = cache
        self._issuer = issuer
        self._max = max_sources
        self.listed = 0
        self.reminted = 0
        self.lookup_failures = 0

    @property
    def remint_interval_s(self) -> float:
        """D147: how often a staging attempt's peer tokens are re-minted - a
        third of their life, so a token is replaced long before it expires
        even when the holder's clock runs a little ahead."""
        return self._issuer.ttl_s / 3

    async def for_attempt(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> list[agent_pb2.BlobSource]:
        """The peer sources of ``attempt`` (empty for a bundle job)."""
        if not job.tree_id:
            return []
        return await self.for_tree(job.tree_id, job, attempt)

    async def for_tree(
        self, tree_id: str, job: JobRecord, attempt: AttemptRecord
    ) -> list[agent_pb2.BlobSource]:
        """Peers holding ``tree_id`` for ``attempt`` - its project tree or one
        of its named input trees (V0.5 G5); each token is scoped to that tree."""
        holders = await self._holders(tree_id, job, attempt)
        sources = [
            agent_pb2.BlobSource(
                url=f"{self._scheme}://{endpoint}",
                token=self._mint(tree_id, attempt, node_id),
                node_id=node_id,
            )
            for node_id, endpoint in holders
        ]
        self.listed += len(sources)
        if sources:
            log.info(
                "controller.jobs.peer_sources",
                job_id=job.id,
                attempt_id=attempt.id,
                node_id=attempt.node_id,
                peers=[s.node_id for s in sources],
            )
        return sources

    async def remint(
        self, tree_id: str, job: JobRecord, attempt: AttemptRecord
    ) -> list[agent_pb2.PeerTokenGrant]:
        """V0.7 G3 (D147): fresh tokens for the peers that hold ``tree_id``
        now (same choice as :meth:`for_tree`). Sent while the attempt stages
        (``RefreshPeerTokens``); the agent applies only those for peers it was
        listed - a peer that left simply gets none and its old token expires."""
        holders = await self._holders(tree_id, job, attempt)
        grants = [
            agent_pb2.PeerTokenGrant(
                tree_id=tree_id,
                node_id=node_id,
                token=self._mint(tree_id, attempt, node_id),
            )
            for node_id, _endpoint in holders
        ]
        self.reminted += len(grants)
        return grants

    def _mint(self, tree_id: str, attempt: AttemptRecord, holder: str) -> str:
        return self._issuer.issue(
            tree=Digest.parse(tree_id),
            requester=validate_node_id(attempt.node_id),
            holder=validate_node_id(holder),
            attempt_id=validate_attempt_id(attempt.id),
        )

    async def _holders(
        self, tree_id: str, job: JobRecord, attempt: AttemptRecord
    ) -> list[tuple[str, str]]:
        """``(node_id, peer endpoint)`` of up to ``max_sources`` peers that
        may serve ``tree_id`` to ``attempt`` (module docstring), freshest first."""
        if self._max <= 0:
            return []
        candidates = {
            live.node_id: live
            for live in self._manager.snapshot()
            if live.state is NodeState.ONLINE
            and not live.ended
            and live.peer_endpoint
            and live.node_id != attempt.node_id
            and self._index_live(live.node_id, live.session_id)
        }
        if not candidates:
            return []
        try:
            rows = await self._cache.holders("tree", tree_id, among=candidates)
        except Exception as exc:  # noqa: BLE001 - peers are optional (docstring)
            self.lookup_failures += 1
            log.warning(
                "controller.jobs.peer_lookup_failed",
                job_id=job.id,
                attempt_id=attempt.id,
                cause=type(exc).__name__,
            )
            return []
        rows.sort(key=lambda row: row[1], reverse=True)
        chosen: list[tuple[str, str]] = []
        for node_id, _seen in rows[: self._max]:
            live = candidates.get(node_id)
            if live is not None:
                chosen.append((node_id, live.peer_endpoint))
        return chosen
