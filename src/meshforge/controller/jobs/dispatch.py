"""Dispatch: a queued job -> an attempt on its node, and every command that
carries the attempt's credentials (V0.4 G4 D65, V0.5 D95; split out of
:mod:`.dispatcher` in V0.6 G1, D119).

:class:`DispatchSteps` is the part of the :class:`~.dispatcher.Dispatcher`
that places a job and talks to the agent about one attempt: the dispatch pass
(``FOR UPDATE SKIP LOCKED``, attempt row + next fencing token committed
**before** ``StartAttempt`` is sent), the placement decision for a pinned job
(:meth:`DispatchSteps._decide`), ``StartAttempt`` with a fresh download-only
fetch token and an upload grant, ``RefreshFetchToken`` after a controller
restart (D95), and ``CancelAttempt``. It holds no state of its own: every
attribute is the dispatcher's (set in :meth:`~.dispatcher.Dispatcher.__init__`),
and it runs only inside the dispatcher's one task (checklist item 5).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import SecretStr

from meshforge.common.capabilities import EXEC_CAPABILITY, EXEC_GPU_CAPABILITY
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id
from meshforge.common.logging import get_logger
from meshforge.common.manifest import Manifest
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.blobs import MAX_FETCH_TOKEN_TTL_S, FetchTokens
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchDecision,
    DispatchFailure,
    ExecutionStore,
    NodeView,
)
from meshforge.controller.jobs.logs import LogHub
from meshforge.controller.jobs.peers import PeerSources
from meshforge.controller.jobs.spec import choose_gpus, execution_spec, start_snapshot
from meshforge.controller.jobs.store import AttemptRecord, JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.named_inputs import NamedInputStore, ResolvedInput
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.settings import JobApiSettings
from meshforge.controller.upload_grants import UploadGrants

__all__ = ["DispatchSteps", "DispatcherStats", "fetch_scope"]

log = get_logger(__name__)


@dataclass(slots=True)
class DispatcherStats:
    dispatched: int = 0
    start_sent: int = 0
    start_resent: int = 0
    fetch_tokens_refreshed: int = 0
    upload_grants_refreshed: int = 0
    statuses_applied: int = 0
    statuses_duplicate: int = 0
    statuses_fenced: int = 0
    statuses_rejected: int = 0
    statuses_dropped: int = 0
    attempts_lost: int = 0
    backstops: int = 0
    cancels_sent: int = 0
    releases_sent: int = 0
    statuses_poisoned: int = 0
    db_retries: int = 0
    upload_deadlines: int = 0
    log_files_deleted: int = 0


def fetch_scope(job: JobRecord, inputs: Sequence[str] = ()) -> list[Digest]:
    """What an attempt's controller fetch token may read: the tree (its manifest
    plus every member, :class:`~meshforge.controller.blobs.FetchTokens`) for a
    tree job, plus every named input tree of the job (V0.5 G5, D92); the
    bundle for a V0.4 bundle job. The same for StartAttempt and every
    RefreshFetchToken (D95)."""
    if job.tree_id:
        extra = [Digest.parse(t) for t in dict.fromkeys(inputs) if t != job.tree_id]
        return [Digest.parse(job.tree_id), *extra]
    if not job.bundle_digest:
        raise ValueError(f"job {job.id} has neither a tree nor a bundle")
    return [Digest.parse(job.bundle_digest)]


class DispatchSteps:
    """See module docstring. Attributes are the dispatcher's (declared here
    for the type checker; :class:`~.dispatcher.Dispatcher` sets them)."""

    _store: ExecutionStore
    _inputs: NamedInputStore | None
    _peers: PeerSources | None
    _manager: SessionManager
    _tokens: FetchTokens
    _grants: UploadGrants | None
    _logs: LogHub | None
    _settings: JobApiSettings
    _clock: Callable[[], float]
    _blob_port: Callable[[], int]
    _blob_url: str
    _resources: ResourceTracker | None
    _wall: Callable[[], dt.datetime]
    _started_at: float
    _grant_refresh_unacked: dict[str, str]
    stats: DispatcherStats

    # ------------------------------------------------------------ dispatch

    async def _dispatch(self) -> None:
        results = await self._store.dispatch_queued(
            self._decide,
            limit=self._settings.dispatch_batch,
            max_per_node=self._settings.max_attempts_per_node,
        )
        for result in results:
            if result.attempt is not None:
                self.stats.dispatched += 1
                await self._send_start(result.job, result.attempt, resend=False)

    def _decide(self, job: JobRecord, node: NodeView | None) -> DispatchDecision:
        node_id = str(job.placement.get("node_id", ""))
        live = self._manager.get(node_id)
        if live is None:
            if self._manager.get_lingering(node_id) is not None:
                return None  # SUSPECT: a blip must not fail the job (D19)
            if self._clock() - self._started_at < self._settings.reconnect_grace_s:
                return None  # just restarted: agents are still reconnecting
            return DispatchFailure(FailureType.NODE_LOST, "placed_node_offline")
        if node is None:
            return DispatchFailure(FailureType.NODE_LOST, "placed_node_deleted")
        if live.state is not NodeState.ONLINE:
            return None  # SUSPECT / DRAINING: wait
        caps = node.hardware.get("capabilities")
        tags = (
            {c for c in caps if isinstance(c, str)} if isinstance(caps, list) else set()
        )
        if EXEC_CAPABILITY not in tags:
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN, "node_no_longer_accepts_jobs"
            )
        manifest = Manifest.model_validate(dict(job.manifest))
        if manifest.resources.gpu == "required" and EXEC_GPU_CAPABILITY not in tags:
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN, "node_can_no_longer_provide_a_gpu"
            )
        if node.active_attempts >= self._settings.max_attempts_per_node:
            return None
        gpus = choose_gpus(manifest, node.hardware)
        snapshot = start_snapshot(
            hardware=node.hardware,
            hardware_scores=node.hardware_scores,
            node_state=live.state.value,
            rtt_ms=live.rtt_ewma_ms,
            sample=None if self._resources is None else self._resources.latest(node_id),
            monotonic_now=self._clock(),
            wall_now=self._wall(),
            gpu_indices=gpus,
        )
        return Dispatch(attempt_id=str(new_attempt_id()), start_snapshot=snapshot)

    # ------------------------------------------------------------ sources

    async def _peer_sources(
        self, job: JobRecord, attempt: AttemptRecord, tree_id: str | None = None
    ) -> list[agent_pb2.BlobSource]:
        if self._peers is None:
            return []
        if tree_id is None:
            return await self._peers.for_attempt(job, attempt)
        return await self._peers.for_tree(tree_id, job, attempt)

    async def _job_inputs(self, job: JobRecord) -> list[ResolvedInput]:
        if self._inputs is None or not job.tree_id:
            return []
        return await self._inputs.job_inputs(job.id)

    async def _input_trees(
        self,
        job: JobRecord,
        attempt: AttemptRecord,
        inputs: Sequence[ResolvedInput],
        secret: str,
    ) -> list[agent_pb2.InputTree]:
        """ExecutionSpec.inputs: per input the controller (with the attempt's
        fetch token, which covers every input tree) then peers holding it."""
        out: list[agent_pb2.InputTree] = []
        for item in inputs:
            peers = await self._peer_sources(job, attempt, item.tree_id)
            out.append(
                agent_pb2.InputTree(
                    tree_id=item.tree_id,
                    mount=item.mount,
                    ref=item.ref,
                    sources=[
                        agent_pb2.BlobSource(
                            url=self._blob_url, token=secret, node_id=""
                        ),
                        *peers,
                    ],
                )
            )
        return out

    # ------------------------------------------------------------ StartAttempt

    async def _send_start(
        self, job: JobRecord, attempt: AttemptRecord, *, resend: bool
    ) -> None:
        peers = await self._peer_sources(job, attempt)
        inputs = await self._job_inputs(job)
        token, ttl = self._mint_fetch_token(job, attempt, inputs)
        spec = execution_spec(
            job,
            attempt,
            fetch_token=token.get_secret_value(),
            blob_url=self._blob_url,
            blob_port=self._blob_port(),
        )
        spec.sources.extend(peers)
        spec.inputs.extend(
            await self._input_trees(job, attempt, inputs, token.get_secret_value())
        )
        if self._grants is not None:
            # D73: the only credential that may upload for this attempt; a
            # re-send keeps what the previous grant already stored.
            grant = self._grants.mint(
                attempt.id,
                ttl_s=ttl,
                max_blobs=self._settings.max_output_blobs,
                max_bytes=self._settings.max_output_bytes,
            )
            spec.upload_token = grant.get_secret_value()
            spec.upload_max_bytes = self._settings.max_output_bytes
            spec.upload_max_blobs = self._settings.max_output_blobs
        message_id = self._manager.send_command(
            attempt.node_id,
            "start_attempt",
            agent_pb2.StartAttempt(
                job_id=job.id,
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
                spec=spec,
            ),
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        if message_id is None:
            # Committed, not sent: the next Reconcile of this node re-sends it,
            # or its OFFLINE / the orphan sweep ends it (D65 commit-before-send).
            log.warning(
                "controller.jobs.start_not_sent",
                job_id=job.id,
                attempt_id=attempt.id,
                node_id=attempt.node_id,
            )
            return
        if self._logs is not None:
            self._logs.open(
                attempt.id,
                job_id=job.id,
                node_id=attempt.node_id,
                token=attempt.fencing_token,
            )
        if resend:
            self.stats.start_resent += 1
        else:
            self.stats.start_sent += 1

    def _mint_fetch_token(
        self,
        job: JobRecord,
        attempt: AttemptRecord,
        inputs: Sequence[ResolvedInput] = (),
    ) -> tuple[SecretStr, float]:
        """A new download-only token for ``attempt`` (D73) and its TTL; every
        older token of the attempt is revoked. One token per send: a re-send or
        a refresh after a controller restart must work although the old token
        died with the old process (tokens live in memory, never persisted).
        Scope (:func:`fetch_scope`) = the tree for a tree job (which grants
        exactly that tree's blobs), the bundle for a V0.4 bundle job."""
        self._tokens.revoke(attempt.id)
        ttl = self._token_ttl(job)
        token = self._tokens.mint(
            attempt.id, fetch_scope(job, [i.tree_id for i in inputs]), ttl_s=ttl
        )
        return token, ttl

    async def _refresh_fetch_token(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> None:
        """D95: a held attempt that may still be fetching has no live token in
        this process (the controller restarted): mint one, send it - with the
        whole source list re-minted (D98: fresh peer tokens; the controller
        first, carrying the new fetch token)."""
        peers = await self._peer_sources(job, attempt)
        inputs = await self._job_inputs(job)
        token, _ttl = self._mint_fetch_token(job, attempt, inputs)
        secret = token.get_secret_value()
        sources = (
            [agent_pb2.BlobSource(url=self._blob_url, token=secret, node_id="")]
            if job.tree_id
            else []
        )
        sent = self._manager.send_command(
            attempt.node_id,
            "refresh_fetch_token",
            agent_pb2.RefreshFetchToken(
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
                fetch_token=secret,
                blob_url=self._blob_url,
                blob_port=self._blob_port(),
                sources=[*sources, *peers],
                inputs=await self._input_trees(job, attempt, inputs, secret),
            ),
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        if sent is None:
            # No live session: nobody holds this token; drop it so the next
            # Reconcile sees "no live token" and refreshes again.
            self._tokens.revoke(attempt.id)
            return
        self.stats.fetch_tokens_refreshed += 1
        log.info(
            "controller.jobs.fetch_token_refreshed",
            job_id=job.id,
            attempt_id=attempt.id,
            node_id=attempt.node_id,
        )

    async def _refresh_upload_grant(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> None:
        """D114: a held attempt has no upload grant in this process (the
        controller restarted): mint one with the StartAttempt's quota and send
        it with where this controller serves blobs now."""
        if self._grants is None:
            return
        grant = self._grants.mint(
            attempt.id,
            ttl_s=self._token_ttl(job),
            max_blobs=self._settings.max_output_blobs,
            max_bytes=self._settings.max_output_bytes,
        )
        sent = self._manager.send_command(
            attempt.node_id,
            "refresh_upload_grant",
            agent_pb2.RefreshUploadGrant(
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
                upload_token=grant.get_secret_value(),
                blob_url=self._blob_url,
                blob_port=self._blob_port(),
            ),
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        if sent is None:
            # Nobody holds it: drop it so the next Reconcile refreshes again.
            self._grants.revoke(attempt.id)
            return
        # Until the agent acks it, a stream that breaks may have taken it along:
        # the next Reconcile then mints and sends another (one live grant).
        self._grant_refresh_unacked[attempt.id] = sent
        self.stats.upload_grants_refreshed += 1
        log.info(
            "controller.jobs.upload_grant_refreshed",
            job_id=job.id,
            attempt_id=attempt.id,
            node_id=attempt.node_id,
        )

    def _token_ttl(self, job: JobRecord) -> float:
        """Lifetime of an attempt's fetch token and upload grant."""
        return min(
            float(job.manifest.get("limits", {}).get("timeout", 0))
            + self._settings.fetch_token_grace_s,
            float(MAX_FETCH_TOKEN_TTL_S),
        )

    def _end_tokens(self, attempt_id: str) -> None:
        """The attempt ended: its fetch token lives ``fetch_token_end_grace_s``
        more, its upload grant takes no new upload (D60/D73)."""
        grace = self._settings.fetch_token_end_grace_s
        self._grant_refresh_unacked.pop(attempt_id, None)
        self._tokens.expire_after(attempt_id, grace)
        if self._grants is not None:
            self._grants.close(attempt_id, grace)

    # ------------------------------------------------------------ cancel

    def _send_cancel(self, attempt: AttemptRecord, reason: str) -> None:
        self._cancel_on(attempt.node_id, attempt.id, attempt.fencing_token, reason)

    def _cancel_on(
        self,
        node_id: str,
        attempt_id: str,
        fencing_token: int,
        reason: str,
        *,
        release: bool = False,
    ) -> None:
        sent = self._manager.send_command(
            node_id,
            "cancel_attempt",
            agent_pb2.CancelAttempt(
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                reason=reason,
                release=release,
            ),
            attempt_id=attempt_id,
            fencing_token=fencing_token,
        )
        if sent is not None:
            self.stats.cancels_sent += 1
            self.stats.releases_sent += int(release)
