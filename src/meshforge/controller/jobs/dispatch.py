"""Dispatch: a queued job -> an attempt on its node, and every command that
carries the attempt's credentials (V0.4 G4 D65, V0.5 D95; split out of
:mod:`.dispatcher` in V0.6 G1, D119).

:class:`DispatchSteps` is the part of the :class:`~.dispatcher.Dispatcher`
that places a job and talks to the agent about one attempt: the dispatch pass
(``FOR UPDATE SKIP LOCKED``, attempt row + next fencing token + the
planner's decision row committed **before** ``StartAttempt`` is sent; the
placement itself is :class:`~.planning.DispatchPlanner`, V0.6 G2),
``StartAttempt`` with a fresh download-only fetch token and an upload grant,
``RefreshFetchToken`` after a controller restart (D95), ``RefreshPeerTokens``
while an attempt stages (V0.7 G3, D147: peer tokens live <= 60 s) and
``CancelAttempt``.
It holds no state of its own: every attribute is the dispatcher's (set in
:meth:`~.dispatcher.Dispatcher.__init__`), and it runs only inside the
dispatcher's one task (checklist item 5).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import SecretStr

from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.manifest import DEFAULT_CHUNK_OUTPUT_BYTES
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.secret_redaction import SecretMatcher
from meshforge.common.split_chunks import PartsIndex
from meshforge.common.states import AttemptRole, AttemptState
from meshforge.controller.blobs import MAX_FETCH_TOKEN_TTL_S, FetchTokens
from meshforge.controller.calibration_store import CalibrationSource
from meshforge.controller.jobs.execution_store import (
    ExecutionStore,
    LiveAttempt,
    StatusKind,
    StatusOutcome,
    StatusReport,
)
from meshforge.controller.jobs.logs import LogHub
from meshforge.controller.jobs.peers import PeerSources
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import NetworkReads, is_benchmark_key
from meshforge.controller.jobs.retry import RetryReason
from meshforge.controller.jobs.spec import execution_spec
from meshforge.controller.jobs.store import AttemptRecord, JobRecord
from meshforge.controller.jobs.transitions import AttemptEvent
from meshforge.controller.ml.predictor import ModelSource
from meshforge.controller.named_inputs import NamedInputStore, ResolvedInput
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.secret_box import (
    SecretIntegrityError,
    SecretKeyError,
    SecretKeyMismatchError,
)
from meshforge.controller.secret_store import (
    SecretsMissingError,
    SecretsTooLargeError,
    SecretStore,
    secret_names,
)
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.settings import (
    JobApiSettings,
    NetworkSettings,
    PlannerSettings,
    SplitSettings,
)
from meshforge.controller.upload_grants import UploadGrants

if TYPE_CHECKING:
    from meshforge.controller.split.service import SplitService

__all__ = [
    "CombineScope",
    "DispatchSteps",
    "DispatcherStats",
    "chunk_cap",
    "fetch_scope",
]

log = get_logger(__name__)

_FETCHING = frozenset({AttemptState.ASSIGNED, AttemptState.STAGING})
"""Attempt states in which the agent may still download blobs (D147)."""


@dataclass(slots=True)
class DispatcherStats:
    dispatched: int = 0
    start_sent: int = 0
    start_resent: int = 0
    fetch_tokens_refreshed: int = 0
    peer_tokens_reminted: int = 0
    """V0.7 G3 (D147): RefreshPeerTokens sent to staging attempts."""
    upload_grants_refreshed: int = 0
    statuses_applied: int = 0
    statuses_duplicate: int = 0
    statuses_fenced: int = 0
    statuses_wrong_node: int = 0
    """V0.7 G7 (CG-E2): reports about an attempt placed on another node."""
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
    retries_scheduled: int = 0
    """V0.6 G3 (D110): attempts whose end put their job back in the queue."""
    retries_exhausted: int = 0
    """Jobs failed because their last allowed attempt failed retryably."""
    execution_overlaps: int = 0
    """V0.6 G3 (D117): superseded attempts found still held by their agent
    after the job was retried - a possible overlapping execution."""


def fetch_scope(
    job: JobRecord,
    inputs: Sequence[str] = (),
    *,
    lane: bool = False,
    combine: CombineScope | None = None,
) -> list[Digest]:
    """What an attempt's controller fetch token may read: the tree (its manifest
    plus every member, :class:`~meshforge.controller.blobs.FetchTokens`) for a
    tree job, plus every named input tree of the job (V0.5 G5, D92); the
    bundle for a V0.4 bundle job. The same for StartAttempt and every
    RefreshFetchToken (D95). A split job's **lane** (V0.10 G4) may also read
    the job's item list blob (``LaneSpec.items_digest``). A **combine**
    (V0.10 G5) reads its parts index and the parts as plain blobs
    (:attr:`CombineScope.blobs`); here only the trees: none for a built-in
    (it reads nothing else), the project and inputs for a script."""
    if combine is not None and not combine.script:
        return []
    if job.tree_id:
        extra = [Digest.parse(t) for t in dict.fromkeys(inputs) if t != job.tree_id]
        if lane and job.split is not None:
            extra.append(Digest.parse(job.split.items_digest))
        return [Digest.parse(job.tree_id), *extra]
    if not job.bundle_digest:
        raise ValueError(f"job {job.id} has neither a tree nor a bundle")
    return [Digest.parse(job.bundle_digest)]


async def _matcher(values: Mapping[str, bytes]) -> SecretMatcher | None:
    """The attempt's shared log-redaction matcher (None without secrets),
    compiled in a worker thread: encoding the largest allowed job's values
    takes tens of milliseconds, too long for the loop every session shares."""
    if not values:
        return None
    return await asyncio.to_thread(SecretMatcher, values)


@dataclass(frozen=True, slots=True)
class CombineScope:
    """V0.10 G5: what a combine attempt reads besides the trees."""

    parts_digest: str
    parts: tuple[str, ...]
    """Every chunk's result digest, in chunk order."""
    script: bool
    """A user combine script (stages the project) - else a built-in."""

    @property
    def blobs(self) -> list[Digest]:
        return [Digest.parse(self.parts_digest), *(Digest.parse(p) for p in self.parts)]


def _is_combine(attempt: AttemptRecord) -> bool:
    """V0.10 G5: a split job's combine attempt."""
    return attempt.role is AttemptRole.COMBINE


def _combine_is_script(job: JobRecord) -> bool:
    spec = None if job.split is None else job.split.spec.get("combine")
    return isinstance(spec, dict) and "script" in spec


def _is_lane(attempt: AttemptRecord) -> bool:
    """V0.10 G4: a Mode 3 lane attempt."""
    return attempt.role is AttemptRole.LANE


def chunk_cap(job: JobRecord) -> int:
    """A split job's ``split.max_chunk_output_bytes`` (V0.10 G4, D261)."""
    split = job.split
    value = None if split is None else split.spec.get("max_chunk_output_bytes")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_CHUNK_OUTPUT_BYTES


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
    _grant_unacked: dict[str, str]
    _planner: DispatchPlanner
    _planner_settings: PlannerSettings
    _network_settings: NetworkSettings
    _peer_minted: dict[str, float]
    _secrets: SecretStore | None
    _insecure_secrets: bool
    _bench_condition: Callable[[str | None], Awaitable[str | None]] | None
    _split_settings: SplitSettings
    _split: SplitService | None
    _calibration: CalibrationSource | None
    """V0.14 G2 (D293): where the tick's calibration comes from (None = none)."""
    _ml: ModelSource | None
    """V0.15 G4 (D336): where the tick's ML model comes from (None = none)."""
    stats: DispatcherStats

    if TYPE_CHECKING:  # the dispatcher's (never shadowed at run time)

        def _after(
            self, outcome: StatusOutcome, report: StatusReport | None = None
        ) -> None: ...

    # ------------------------------------------------------------ dispatch

    async def _dispatch(self) -> None:
        if self._calibration is not None:
            # V0.14 G2 (D293): one read per tick (by version id; factors are
            # loaded only when the active version changed); a failed read
            # keeps the version in use - never blocks the dispatch
            self._planner.calibration = await self._calibration.refresh()
        if self._ml is not None:
            # V0.15 G4 (D336): one read per tick; a failed read keeps the
            # model in use - never blocks the dispatch (D363)
            try:
                self._planner.ml = await self._ml.current()
            except Exception as exc:  # noqa: BLE001 - a model never stops dispatch
                log.warning(
                    "controller.dispatcher.ml_source_failed",
                    error_type=type(exc).__name__,
                )
        results = await self._store.dispatch_planned(
            self._planner.decide,
            limit=self._settings.dispatch_batch,
            max_per_node=self._settings.max_attempts_per_node,
            now=self._wall(),
            aging_s=self._planner_settings.priority_aging_s,
            history_runs=self._planner_settings.history_runs,
            network=NetworkReads(
                transfer_min_bytes=self._network_settings.transfer_min_bytes,
                transfer_history=self._network_settings.transfer_history,
            ),
            # V0.10 G5: split jobs that owe their combine are placed too
            decide_combine=None
            if self._split is None
            else self._planner.decide_combine,
            # V0.14 G3 (D296): the nodes' reliability for the risk term
            risk_window_s=self._planner_settings.risk_window_days * 86_400.0,
        )
        for result in results:
            for attempt in result.lanes or (
                () if result.attempt is None else (result.attempt,)
            ):
                self.stats.dispatched += 1
                await self._send_start(result.job, attempt, resend=False)

    # ------------------------------------------------------------ sources

    async def _peer_sources(
        self, job: JobRecord, attempt: AttemptRecord, tree_id: str | None = None
    ) -> list[agent_pb2.BlobSource]:
        if self._peers is None:
            return []
        if tree_id is None:
            # D147: the attempt's peer tokens are fresh from now on.
            self._peer_minted[attempt.id] = self._clock()
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

    async def _job_secrets(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> dict[str, bytes] | None:
        """V0.7 G6 (D144): the values of the secrets the job names, **of its
        owner only**, read at every (re)send. A secret that is missing,
        unreadable or a master key that is unusable ends the attempt before
        anything is sent - the job fails typed, never retried, never hangs
        (``secret_missing:<NAME>``). None = the attempt was ended."""
        names = secret_names(job.manifest)
        if not names:
            return {}
        failure: tuple[FailureType, str] | None = None
        live = self._manager.get(attempt.node_id)
        try:
            if self._secrets is None:
                failure = (FailureType.CONFIG_ERROR, "secrets are not configured")
            elif (
                live is not None
                and live.cert_serial is None
                and not self._insecure_secrets
            ):
                # D144: values travel only over mTLS (insecure-dev has no
                # client certificate); never on a plaintext channel.
                failure = (
                    FailureType.CONFIG_ERROR,
                    "secrets_need_mtls: the node's session is not mutually "
                    "authenticated (insecure-dev); job secrets are never sent "
                    "in plaintext ([security] allow_insecure_secrets)",
                )
            else:
                return await self._secrets.resolve(
                    job.owner_user_id,
                    names,
                    max_total_bytes=self._settings.max_job_secret_bytes,
                )
        except SecretsMissingError as exc:
            failure = (FailureType.INVALID_INPUT, exc.message)
        except SecretsTooLargeError as exc:
            # D162: the same cap as at submit (a value replaced by a larger
            # one while the job waited, or a lowered setting).
            failure = (FailureType.INVALID_INPUT, exc.message)
        except SecretKeyMismatchError as exc:
            failure = (FailureType.CONFIG_ERROR, f"secret_key_mismatch: {exc.message}")
        except SecretIntegrityError as exc:
            failure = (FailureType.INTEGRITY_ERROR, f"secret_unreadable: {exc.message}")
        except SecretKeyError:
            failure = (
                FailureType.CONFIG_ERROR,
                "secret_key_unusable: the controller's secrets master key cannot "
                "be used (see the controller log)",
            )
        outcome = await self._store.end_attempt(
            attempt.id,
            attempt.fencing_token,
            event=AttemptEvent.FAIL,
            failure_type=failure[0],
            detail=failure[1],
            retryable=False,
        )
        self._after(outcome)
        log.warning(
            "controller.jobs.secrets_refused",
            job_id=job.id,
            attempt_id=attempt.id,
            failure_type=failure[0].value,
            detail=failure[1],
        )
        return None

    async def _log_secrets(self, job: JobRecord) -> SecretMatcher | None:
        """V0.7 G6: the values the log store redacts for a live attempt found
        again after a controller restart (Reconcile), compiled off the event
        loop; best effort - the agent redacts first, a store problem only
        costs the second layer."""
        names = secret_names(job.manifest)
        if not names or self._secrets is None:
            return None
        try:
            values = await self._secrets.resolve(
                job.owner_user_id,
                names,
                max_total_bytes=self._settings.max_job_secret_bytes,
            )
            return await _matcher(values)
        except Exception as exc:  # noqa: BLE001 - logged; the agent still redacts
            log.warning(
                "controller.jobs.log_secrets_unavailable",
                job_id=job.id,
                cause=type(exc).__name__,
            )
            return None

    async def _send_start(
        self, job: JobRecord, attempt: AttemptRecord, *, resend: bool
    ) -> None:
        secrets = await self._job_secrets(job, attempt)
        if secrets is None:
            return  # ended before anything reached the node
        condition: str | None = None
        if self._bench_condition is not None:
            try:
                condition = await self._bench_condition(job.idempotency_key)
            except Exception as exc:  # noqa: BLE001 - never send it without
                # V0.9 G4: a run must not start without its condition (it
                # would be labelled wrong): not sent now - the next Reconcile
                # re-sends it, or the orphan sweep ends it (D65).
                log.warning(
                    "controller.jobs.condition_unknown",
                    job_id=job.id,
                    attempt_id=attempt.id,
                    cause=type(exc).__name__,
                )
                return
        # D162 (T-8): one matcher per attempt, compiled off the event loop.
        matcher = await _matcher(secrets)
        combine: CombineScope | None = None
        if _is_combine(attempt):
            combine = await self._combine_scope(job, attempt)
            if combine is None:
                return  # ended before anything reached the node
        peers = await self._peer_sources(job, attempt)
        inputs = await self._job_inputs(job)
        token, ttl = self._mint_fetch_token(job, attempt, inputs, combine=combine)
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
            # re-send keeps what the previous grant already stored. A lane's
            # grant grows by one chunk's output per chunk granted (D261).
            grant = self._grants.mint(
                attempt.id,
                ttl_s=ttl,
                max_blobs=self._settings.max_output_blobs,
                max_bytes=self._settings.max_output_bytes,
                node_id=attempt.node_id,
            )
            spec.upload_token = grant.get_secret_value()
            spec.upload_max_bytes = self._settings.max_output_bytes
            spec.upload_max_blobs = self._settings.max_output_blobs
        start = agent_pb2.StartAttempt(
            job_id=job.id,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
            spec=spec,
            secrets=[
                agent_pb2.JobSecret(name=name, value=value)
                for name, value in sorted(secrets.items())
            ],
        )
        if condition is not None:
            # V0.9 G4 (D217): the agent applies + verifies it before staging
            start.condition.spec = condition
        if _is_lane(attempt) and job.split is not None:
            # V0.10 G4 (D259): this attempt pulls chunks instead of running once
            start.lane.CopyFrom(
                agent_pb2.LaneSpec(
                    items_digest=job.split.items_digest,
                    item_count=job.split.item_count,
                    max_chunk_output_bytes=chunk_cap(job),
                    lease_timeout_s=self._split_settings.visibility_timeout_s,
                )
            )
        if combine is not None:
            # V0.10 G5 (D268): this attempt combines the job's chunk results
            start.combine.CopyFrom(
                agent_pb2.CombineSpec(
                    parts_digest=combine.parts_digest,
                    chunk_count=len(combine.parts),
                )
            )
        # V0.9 G5 (D246): the agent's owner guard stops only benchmark jobs
        start.benchmark = is_benchmark_key(getattr(job, "idempotency_key", None))
        message_id = self._manager.send_command(
            attempt.node_id,
            "start_attempt",
            start,
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        if self._grants is not None:
            # D114: until acked, the agent may not have this grant (the one it
            # holds, if any, was just replaced); "" = not even sent.
            self._grant_unacked[attempt.id] = message_id or ""
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
                secret_values=matcher,
            )
        if resend:
            self.stats.start_resent += 1
        else:
            self.stats.start_sent += 1

    async def _combine_scope(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> CombineScope | None:
        """V0.10 G5: a combine's parts (from the job's parts index). The index
        unreadable (a controller-side fault) ends the attempt - retried by the
        combine rules - rather than sending a combine that cannot read its
        input. None = it was ended."""
        digest = None if job.split is None else job.split.parts_digest
        index: PartsIndex | None = None
        cause = "no_parts_index"
        if digest is not None and self._split is not None:
            try:
                index = await self._split.parts_index(digest)
            except (LookupError, ValueError, OSError) as exc:
                cause = type(exc).__name__
        if digest is not None and index is not None:
            return CombineScope(digest, index.digests, _combine_is_script(job))
        outcome = await self._store.end_attempt(
            attempt.id,
            attempt.fencing_token,
            event=AttemptEvent.FAIL,
            failure_type=FailureType.INTERNAL_ERROR,
            detail=f"the job's parts index cannot be read ({cause})",
            retryable=True,
        )
        self._after(outcome)
        self._note_retry(outcome)
        log.error(
            "controller.split.parts_index_unreadable",
            job_id=job.id,
            attempt_id=attempt.id,
            cause=cause,
        )
        return None

    def _mint_fetch_token(
        self,
        job: JobRecord,
        attempt: AttemptRecord,
        inputs: Sequence[ResolvedInput] = (),
        *,
        combine: CombineScope | None = None,
    ) -> tuple[SecretStr, float]:
        """A new download-only token for ``attempt`` (D73) and its TTL; every
        older token of the attempt is revoked. One token per send: a re-send or
        a refresh after a controller restart must work although the old token
        died with the old process (tokens live in memory, never persisted).
        Scope (:func:`fetch_scope`) = the tree for a tree job (which grants
        exactly that tree's blobs), the bundle for a V0.4 bundle job."""
        self._tokens.revoke(attempt.id)
        ttl = self._token_ttl(job, attempt)
        token = self._tokens.mint(
            attempt.id,
            fetch_scope(
                job,
                [i.tree_id for i in inputs],
                lane=_is_lane(attempt),
                combine=combine,
            ),
            ttl_s=ttl,
            node_id=attempt.node_id,
            blobs=() if combine is None else combine.blobs,
        )
        return token, ttl

    async def _refresh_fetch_token(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> None:
        """D95: a held attempt that may still be fetching has no live token in
        this process (the controller restarted): mint one, send it - with the
        whole source list re-minted (D98: fresh peer tokens; the controller
        first, carrying the new fetch token)."""
        combine: CombineScope | None = None
        if _is_combine(attempt):
            combine = await self._combine_scope(job, attempt)
            if combine is None:
                return
        peers = await self._peer_sources(job, attempt)
        inputs = await self._job_inputs(job)
        token, _ttl = self._mint_fetch_token(job, attempt, inputs, combine=combine)
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

    async def _remint_peer_tokens(self, live: Sequence[LiveAttempt]) -> None:
        """D147: peer tokens live <= 60 s, so every attempt that may still be
        fetching (ASSIGNED/STAGING, a tree job) gets fresh ones every third of
        the token life - one ``RefreshPeerTokens`` with a token per (tree,
        current holder). The agent swaps them into the running transfer, so
        every range it asks carries a token younger than the token life."""
        if self._peers is None:
            return
        now = self._clock()
        interval = self._peers.remint_interval_s
        staging = {
            entry.attempt.id: entry
            for entry in live
            if (entry.attempt.state in _FETCHING or _is_lane(entry.attempt))
            and entry.job.tree_id
        }
        for attempt_id in [a for a in self._peer_minted if a not in staging]:
            del self._peer_minted[attempt_id]
        for entry in staging.values():
            job, attempt = entry.job, entry.attempt
            project = job.tree_id or ""
            if now - self._peer_minted.get(attempt.id, -interval) < interval:
                continue
            if self._manager.get(attempt.node_id) is None:
                continue  # no session: the next StartAttempt/refresh mints anew
            self._peer_minted[attempt.id] = now
            trees = [project] + [t.tree_id for t in await self._job_inputs(job)]
            grants: list[agent_pb2.PeerTokenGrant] = []
            for tree_id in dict.fromkeys(trees):
                grants.extend(await self._peers.remint(tree_id, job, attempt))
            if not grants:
                continue
            sent = self._manager.send_command(
                attempt.node_id,
                "refresh_peer_tokens",
                agent_pb2.RefreshPeerTokens(
                    attempt_id=attempt.id,
                    fencing_token=attempt.fencing_token,
                    tokens=grants,
                ),
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
            )
            if sent is not None:
                self.stats.peer_tokens_reminted += 1
                log.debug(
                    "controller.jobs.peer_tokens_reminted",
                    job_id=job.id,
                    attempt_id=attempt.id,
                    node_id=attempt.node_id,
                    tokens=len(grants),
                )

    async def _refresh_upload_grant(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> None:
        """D114: a held attempt has no upload grant in this process (the
        controller restarted): mint one with the StartAttempt's quota and send
        it with where this controller serves blobs now."""
        if self._grants is None:
            return
        held = 0
        if _is_lane(attempt) and self._split is not None:
            # V0.10 G4 (D261): room for the chunks the lane still holds (a
            # restarted controller forgot the grown quota)
            held = await self._split.held_leases(attempt.id)
        grant = self._grants.mint(
            attempt.id,
            ttl_s=self._token_ttl(job, attempt),
            max_blobs=self._settings.max_output_blobs + held,
            max_bytes=self._settings.max_output_bytes + held * chunk_cap(job),
            node_id=attempt.node_id,
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
        self._grant_unacked[attempt.id] = sent
        self.stats.upload_grants_refreshed += 1
        log.info(
            "controller.jobs.upload_grant_refreshed",
            job_id=job.id,
            attempt_id=attempt.id,
            node_id=attempt.node_id,
        )

    def _token_ttl(self, job: JobRecord, attempt: AttemptRecord | None = None) -> float:
        """Lifetime of an attempt's fetch token and upload grant. A lane runs
        chunk after chunk (each within ``limits.timeout``), so its credentials
        live as long as any may (V0.10 G4); they end with the lane."""
        if attempt is not None and _is_lane(attempt):
            return float(MAX_FETCH_TOKEN_TTL_S)
        return min(
            float(job.manifest.get("limits", {}).get("timeout", 0))
            + self._settings.fetch_token_grace_s,
            float(MAX_FETCH_TOKEN_TTL_S),
        )

    def _end_tokens(self, attempt_id: str) -> None:
        """The attempt ended: its fetch token lives ``fetch_token_end_grace_s``
        more, its upload grant takes no new upload (D60/D73)."""
        grace = self._settings.fetch_token_end_grace_s
        self._grant_unacked.pop(attempt_id, None)
        self._tokens.expire_after(attempt_id, grace)
        if self._grants is not None:
            self._grants.close(attempt_id, grace)
        if self._split is not None:
            self._split.forget_lane(attempt_id)  # V0.10 G4: its counters go

    # ------------------------------------------------------------ retries

    def _note_retry(self, outcome: StatusOutcome) -> None:
        """D110: count and log (after commit) what an attempt's end did to its
        job; wake the loop so a retry without backoff is placed at once."""
        verdict, attempt = outcome.retry, outcome.attempt
        if verdict is None or attempt is None or outcome.kind is not StatusKind.APPLIED:
            return
        if verdict.retry:
            self.stats.retries_scheduled += 1
            log.info(
                "controller.jobs.retry_scheduled",
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                node_id=attempt.node_id,
                fencing_token=attempt.fencing_token,
                failure_type=None
                if attempt.failure_type is None
                else attempt.failure_type.value,
                backoff_s=verdict.backoff_s,
            )
            self._wake_soon()
        elif verdict.reason is RetryReason.RETRIES_EXHAUSTED:
            self.stats.retries_exhausted += 1
            log.warning(
                "controller.jobs.retries_exhausted",
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                node_id=attempt.node_id,
                attempts=attempt.fencing_token,
            )

    def _wake_soon(self) -> None:
        """Run a dispatch pass soon (the dispatcher's ``wake``)."""

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
