"""Which peers an attempt may fetch its tree from (V0.5 D89/D98/D100).

For a tree job the dispatcher lists the controller first, then up to
``[controller.jobs] max_peer_sources`` nodes that

* are ONLINE right now (a live session in the manager),
* advertised a peer endpoint that the controller accepted (D99),
* are not the attempt's own node, and
* hold ``tree <tree_id>`` in the cache location index (D100: reported only once
  the manifest and every member blob are in their CAS),

ordered by the most recent ``last_seen`` of that index row. Each peer gets its
own signed token (D98: this tree, this requester, this holder, this attempt,
<= 10 minutes). The index read is one indexed query; if the database cannot
answer, the attempt simply fetches from the controller (logged) - peers are an
optimisation, never a dependency.
"""

from __future__ import annotations

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
    ) -> None:
        self._manager = manager
        self._cache = cache
        self._issuer = issuer
        self._max = max_sources
        self.listed = 0
        self.lookup_failures = 0

    async def for_attempt(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> list[agent_pb2.BlobSource]:
        """The peer sources of ``attempt`` (empty for a bundle job)."""
        if not job.tree_id or self._max <= 0:
            return []
        candidates = {
            live.node_id: live
            for live in self._manager.snapshot()
            if live.state is NodeState.ONLINE
            and not live.ended
            and live.peer_endpoint
            and live.node_id != attempt.node_id
        }
        if not candidates:
            return []
        try:
            rows = await self._cache.holders("tree", job.tree_id, among=candidates)
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
        tree = Digest.parse(job.tree_id)
        sources: list[agent_pb2.BlobSource] = []
        for node_id, _seen in rows[: self._max]:
            live = candidates.get(node_id)
            if live is None:
                continue
            token = self._issuer.issue(
                tree=tree,
                requester=validate_node_id(attempt.node_id),
                holder=validate_node_id(node_id),
                attempt_id=validate_attempt_id(attempt.id),
            )
            sources.append(
                agent_pb2.BlobSource(
                    url=f"http://{live.peer_endpoint}", token=token, node_id=node_id
                )
            )
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
