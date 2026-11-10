"""The dispatcher's side of the chunk protocol (V0.10 G4, D261/D263/D267).

The session layer hands a lane's chunk messages to the hooks registered here
(on the session's reader task: validate, then queue - never a database call
there). The dispatcher's one task takes them off its queue in arrival order and
calls :meth:`SplitService.handle`, which runs :class:`~.store.ChunkStore` and
then talks back to the lane:

* ``ChunkRequest`` -> ``ChunkGrant`` (the lane's upload grant grows by
  ``split.max_chunk_output_bytes`` per new chunk, D261) or ``NoWork``; an
  idle lane at the tail may get a **speculative copy** of a straggling chunk
  (V0.10 G6, D266, :mod:`.speculation`);
* ``ChunkResult`` (a report) -> a ``done`` digest counts only if the lane's own
  upload grant stored and verified that blob (the V0.4 D71/D73 rule) -
  otherwise the lease is released uncounted (the chunk runs again; nothing
  unverified is ever committed); then it is applied and **acked only once
  stored**. A lease that lost to the commit (the original or the speculative
  copy of a straggling chunk, V0.10 G6) gets ``CancelChunk``; a job that
  failed (a chunk failed for good) gets ``CancelAttempt`` for every live lane;
* ``LeaseRenew`` -> the lane's leases extended. A lease renewed with
  ``stalled_s`` > 0 (V1.0 G10, D451: the agent saw its chunk make no progress)
  is **marked**: the next lane on another node that asks gets a copy of it
  first (an idle lane at once, a busy one when its chunk ends; first result
  wins, D266). With no lane on another node it keeps being renewed - a reclaim
  would only run it again on the same node - and the controller says so
  (``controller.split.chunk_stalled`` with ``action=alone``). The longest
  ``stalled_s`` of each lease goes into its chunk record.

**Env failures from an older agent (V1.0 G10, D448).** Since D443 an agent
whose env cannot be built ends its lane ``FAILED(env_error)`` itself. An older
agent reports it as a *chunk* failure and pulls the next chunk - building the
env again for every chunk. The controller now treats a chunk result failed
``env_error`` as the end of that lane: the lease is **released** uncounted (the
chunk did nothing wrong), then ``end_lane`` (the dispatcher) ends the lane
``FAILED(env_error)``, records ``(node, env)`` known-bad and cancels the lane on
the agent.

Every message that fails its lane / lease checks is counted
(:attr:`SplitStats.fenced`) and logged ``controller.split.fenced`` with its
cause, and never applied (checklist 34). The periodic step :meth:`expire`
returns overdue leases to ``pending`` (visibility timeout, D267b).
"""

from __future__ import annotations

import datetime as dt
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.split_chunks import PartsIndex
from meshforge.common.states import JobState
from meshforge.controller.session.live import LiveSession
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.settings import SplitSettings
from meshforge.controller.split.lanes import LiveLane
from meshforge.controller.split.messages import (
    ChunkRequestIn,
    ChunkResultIn,
    LeaseRenewIn,
    parse_chunk_request,
    parse_chunk_result,
    parse_lease_renew,
)
from meshforge.controller.split.records import ChunkReport
from meshforge.controller.split.store import (
    ChunkGrantOut,
    ChunkStore,
    FenceCause,
    NoWorkOut,
    ResultKind,
    ResultOutcome,
)
from meshforge.controller.upload_grants import UploadGrants

__all__ = ["ChunkWork", "SplitService", "SplitStats"]

log = get_logger(__name__)

_MAX_TRACKED: Final = 100_000
"""Bound on the (lane, chunk) unverified counters kept (live lanes only; a
new pair with no room is counted as a failure - fail safe, never a reset)."""
_MAX_STALLS: Final = 10_000
"""D451: stalled marks / peaks kept (bounded; oldest dropped)."""
_NO_WORK: Final = {
    "drained": agent_pb2.NoWork.REASON_DRAINED,
    "wait": agent_pb2.NoWork.REASON_WAIT,
    "stop": agent_pb2.NoWork.REASON_STOP,
}


@dataclass(slots=True)
class SplitStats:
    requests: int = 0
    grants: int = 0
    """Chunks granted (new leases)."""
    regrants: int = 0
    """Leases granted again because the lane did not list them (lost grant)."""
    no_work: int = 0
    committed: int = 0
    late_commits: int = 0
    already_done: int = 0
    retried: int = 0
    chunks_failed: int = 0
    released: int = 0
    not_uploaded: int = 0
    """Done results whose digest the lane's grant did not store."""
    unverified_late: int = 0
    """Unverified results of a lease that had already ended (nothing done)."""
    unverified_failures: int = 0
    """Of those, the ones past ``max_unverified_results``: counted failures."""
    renewed: int = 0
    renew_refused: int = 0
    expired: int = 0
    rejected: int = 0
    """Malformed chunk messages (never looked up)."""
    fenced: dict[str, int] = field(default_factory=dict)
    """Cause -> count of chunk messages not applied (checklist 34)."""
    jobs_done: int = 0
    """Split jobs whose every chunk is committed (they then owe a combine)."""
    nondeterministic: int = 0
    """V0.10 G5 (D269): second results of a committed chunk with another
    digest (the job's ``nondeterministic_chunk`` warning)."""
    speculated: int = 0
    """V0.10 G6 (D266): speculative copies granted to idle lanes."""
    copies_won: int = 0
    """Commits where the speculative copy beat the original lease."""
    originals_won: int = 0
    """Commits where the original beat its live speculative copy."""
    stalls: int = 0
    """V1.0 G10 (D451): leases newly reported stalled."""
    stalls_alone: int = 0
    """Of those, with no lane on another node to copy them."""


EndLane = Callable[[LiveLane, str, FailureType, str], Awaitable[None]]
"""D448: ``(lane, job_id, failure_type, detail)`` - the dispatcher ends the
lane ``FAILED`` (not retryable), records known-bad envs, cancels it."""


@dataclass(slots=True)
class _Stall:
    """D451: one lease its lane reported stalled."""

    job_id: str
    attempt_id: str
    node_id: str
    seq: int
    stalled_s: float


@dataclass(frozen=True, slots=True)
class ChunkWork:
    """One chunk message on the dispatcher's queue."""

    live: LiveSession
    message_id: str
    body: ChunkRequestIn | ChunkResultIn | LeaseRenewIn


class SplitService:
    """See module docstring. Lives inside the dispatcher's one task."""

    def __init__(
        self,
        *,
        store: ChunkStore,
        manager: SessionManager,
        grants: UploadGrants | None,
        settings: SplitSettings,
        wall_clock: Callable[[], dt.datetime],
        put: Callable[[ChunkWork], bool],
        clock: Callable[[], float] | None = None,
        wake: Callable[[], None] | None = None,
        end_lane: EndLane | None = None,
    ) -> None:
        self._store = store
        self._wake = wake
        self._end_lane = end_lane
        """D448: end a lane controller-side (old agent's chunk env_error)."""
        """V0.10 G5: run a dispatch pass soon (a job now owes its combine)."""
        self._manager = manager
        self._grants = grants
        self._settings = settings
        self._wall = wall_clock
        self._put = put
        self.stats = SplitStats()
        self._clock = clock or time.monotonic
        self._unverified: OrderedDict[tuple[str, int], int] = OrderedDict()
        """(lane attempt, chunk item id) -> unverified ``done`` results tolerated
        so far. In memory: a controller restart resets it (the 2-node and
        ``retry.max_attempts`` rules still bound the job)."""
        self._cooldown: OrderedDict[tuple[str, int], float] = OrderedDict()
        self._tracked_lanes: set[str] = set()
        self._stalls: OrderedDict[int, _Stall] = OrderedDict()
        """D451: chunk item id -> its stalled lease (copied first)."""
        self._stall_peaks: OrderedDict[tuple[int, int], float] = OrderedDict()
        """D451: (item id, lease seq) -> longest ``stalled_s`` (chunk record)."""
        store.stall_peaks = self._stall_peaks
        """(lane, chunk) -> monotonic time before which that lane does not get
        that chunk again (after an unverified result)."""
        manager.dispatch.register("chunk_request", self._on_request)
        manager.dispatch.register("chunk_result", self._on_result)
        manager.dispatch.register("lease_renew", self._on_renew)
        manager.defer_ack("chunk_result")

    @property
    def lease_timeout_s(self) -> float:
        return self._settings.visibility_timeout_s

    # ------------------------------------------------------------ hooks

    def _reject(self, live: LiveSession, payload: str, reason: str) -> None:
        self.stats.rejected += 1
        log.warning(
            "controller.split.message_rejected",
            node_id=live.node_id,
            session_id=live.session_id,
            payload=payload,
            reason=reason,
        )

    async def _on_request(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        body = parse_chunk_request(msg.chunk_request)
        if isinstance(body, str):
            self._reject(live, "chunk_request", body)
            return
        self._put(ChunkWork(live, msg.message_id, body))

    async def _on_renew(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        body = parse_lease_renew(msg.lease_renew)
        if isinstance(body, str):
            self._reject(live, "lease_renew", body)
            return
        self._put(ChunkWork(live, msg.message_id, body))

    async def _on_result(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        body = parse_chunk_result(msg.chunk_result)
        if isinstance(body, str):
            self._reject(live, "chunk_result", body)
            self._manager.ack_message(live, msg.message_id)  # never replay it
            return
        if not self._put(ChunkWork(live, msg.message_id, body)):
            # Not acked: the lane re-sends it (it keeps unacked results).
            log.warning(
                "controller.split.result_dropped",
                node_id=live.node_id,
                attempt_id=body.lane.attempt_id,
            )

    # ------------------------------------------------------------ work

    async def handle(self, work: ChunkWork) -> None:
        body = work.body
        if isinstance(body, ChunkRequestIn):
            await self._request(work.live, body)
        elif isinstance(body, ChunkResultIn):
            await self._result(work.live, work.message_id, body)
        else:
            await self._renew(work.live, body)

    def _fenced(
        self, live: LiveSession, kind: str, cause: str, attempt_id: str, **extra: Any
    ) -> None:
        self.stats.fenced[cause] = self.stats.fenced.get(cause, 0) + 1
        log.warning(
            "controller.split.fenced",
            node_id=live.node_id,
            session_id=live.session_id,
            attempt_id=attempt_id,
            message=kind,
            cause=cause,
            **extra,
        )

    async def _request(self, live: LiveSession, body: ChunkRequestIn) -> None:
        self.stats.requests += 1
        lane = body.lane
        got = await self._store.request(
            node_id=live.node_id,
            attempt_id=lane.attempt_id,
            token=lane.fencing_token,
            want=body.want,
            held=body.held,
            now=self._wall(),
            avoid=self._avoid(lane.attempt_id),
            stalled=self._stalled_for(lane.job_id, live.node_id),
        )
        if isinstance(got, FenceCause):
            self._fenced(live, "chunk_request", got.value, lane.attempt_id)
            # The lane is not ours to feed: tell it to stop pulling.
            got = NoWorkOut("stop")
        if isinstance(got, NoWorkOut):
            self.stats.no_work += 1
            self._manager.send_command(
                live.node_id,
                "no_work",
                agent_pb2.NoWork(
                    job_id=lane.job_id,
                    attempt_id=lane.attempt_id,
                    fencing_token=lane.fencing_token,
                    reason=_NO_WORK[got.reason],
                    retry_after_s=self._settings.wait_retry_s
                    if got.reason == "wait"
                    else 0.0,
                ),
                attempt_id=lane.attempt_id,
                fencing_token=lane.fencing_token,
            )
            return
        self._grant(live, body, got)

    def _grant(
        self, live: LiveSession, body: ChunkRequestIn, got: ChunkGrantOut
    ) -> None:
        lane = body.lane
        fresh = len(got.chunks) - got.regranted
        self.stats.grants += fresh
        self.stats.regrants += got.regranted
        self.stats.speculated += int(got.speculated is not None)
        if self._grants is not None and fresh:
            # D261: room for the new chunks' outputs under the lane's grant
            self._grants.raise_quota(
                lane.attempt_id, add_bytes=fresh * got.chunk_cap, add_blobs=fresh
            )
        msg = agent_pb2.ChunkGrant(
            job_id=lane.job_id,
            attempt_id=lane.attempt_id,
            fencing_token=lane.fencing_token,
            lease_timeout_s=self.lease_timeout_s,
            chunks=[
                agent_pb2.GrantedChunk(
                    chunk_index=c.chunk_index,
                    item_id=c.item_id,
                    lease_seq=c.lease_seq,
                    first_item=c.first_item,
                    item_count=c.item_count,
                    items_digest=c.items_digest,
                    speculative=c.speculative,
                )
                for c in got.chunks
            ],
        )
        sent = self._manager.send_command(
            live.node_id,
            "chunk_grant",
            msg,
            attempt_id=lane.attempt_id,
            fencing_token=lane.fencing_token,
        )
        log.info(
            "controller.split.granted",
            job_id=lane.job_id,
            attempt_id=lane.attempt_id,
            node_id=live.node_id,
            chunks=[c.chunk_index for c in got.chunks],
            regranted=got.regranted,
            speculative=got.speculated is not None,
            sent=sent is not None,
        )

    async def _result(
        self, live: LiveSession, message_id: str, body: ChunkResultIn
    ) -> None:
        lane = body.lane
        outcome, digest, size = body.outcome, body.result_digest, None
        failure = body.failure
        key = (lane.attempt_id, body.item_id)
        unverified = False
        env_failed = (
            outcome == "failed"
            and failure is not None
            and failure[0] is FailureType.ENV_ERROR
            and self._end_lane is not None
        )
        if env_failed:
            outcome = "released"  # D448: the node's env, not the chunk's fault
        if outcome == "done" and digest is not None:
            size = (
                None
                if self._grants is None
                else self._grants.uploaded_size(lane.attempt_id, digest)
            )
            if size is None:
                # D261: a digest merely named proves nothing. A few are
                # tolerated uncounted (a controller restart forgets what a
                # grant stored); past the limit the chunk fails, counted.
                unverified = True
                self.stats.not_uploaded += 1
                seen = self._unverified.get(key, 0)
                log.warning(
                    "controller.split.result_not_uploaded",
                    job_id=lane.job_id,
                    attempt_id=lane.attempt_id,
                    node_id=live.node_id,
                    chunk=body.chunk_index,
                    times=seen + 1,
                )
                digest = None
                full = key not in self._unverified and (
                    len(self._unverified) >= _MAX_TRACKED
                )
                if full or seen >= self._settings.max_unverified_results:
                    # past the limit - or no room to count it (only live
                    # lanes' counters are kept): fail safe, counted

                    self._unverified.pop(key, None)
                    self.stats.unverified_failures += 1
                    outcome = "failed"
                    failure = (
                        FailureType.OUTPUT_ERROR,
                        f"chunk_unverified: chunk {body.chunk_index}: the lane "
                        f"reported {seen + 1} results its upload grant never "
                        "stored",
                        True,
                    )
                else:
                    outcome = "released"
        got = await self._store.result(
            node_id=live.node_id,
            attempt_id=lane.attempt_id,
            token=lane.fencing_token,
            item_id=body.item_id,
            seq=body.lease_seq,
            outcome=outcome,
            digest=digest,
            failure=failure,
            size=size,
            now=self._wall(),
            report=ChunkReport(
                phases=body.phases,
                exit_code=body.exit_code,
                input_bytes=body.input_bytes,
                stderr_tail=body.stderr_tail,
            ),
        )
        if got.kind is not ResultKind.FENCED:
            # D451: that lease's try is over (a committed chunk: every lease)
            self._clear_stall(
                body.item_id,
                None if got.kind is ResultKind.COMMITTED else body.lease_seq,
            )
        if unverified and got.kind is ResultKind.RELEASED:
            self._note_unverified(key)
        if env_failed and got.kind is ResultKind.RELEASED:
            assert failure is not None  # noqa: S101 - env_failed checked it
            assert self._end_lane is not None  # noqa: S101
            log.warning(
                "controller.split.lane_env_failed",
                job_id=lane.job_id,
                attempt_id=lane.attempt_id,
                node_id=live.node_id,
                chunk=body.chunk_index,
            )
            await self._end_lane(
                LiveLane(lane.attempt_id, live.node_id, lane.fencing_token),
                lane.job_id,
                FailureType.ENV_ERROR,
                failure[1],
            )
        self._manager.ack_message(live, message_id)  # stored (or never will be)
        if (
            unverified
            and got.kind is ResultKind.FENCED
            and got.fence == "lease_not_live"
        ):
            # a late unverified result: the lease had already ended (expired,
            # released, lane lost) - nothing to put back, nothing committed
            self.stats.unverified_late += 1
            log.warning(
                "controller.split.result_unverified_late",
                job_id=lane.job_id,
                attempt_id=lane.attempt_id,
                node_id=live.node_id,
                chunk=body.chunk_index,
                lease_seq=body.lease_seq,
            )
        else:
            self._count(live, body, got)
        for holder, item_id, seq in got.cancel_chunks:
            self._cancel_chunk(holder, lane.job_id, item_id, seq, "superseded")
        if got.job_ended is JobState.FAILED:
            for other in got.cancel_lanes:
                self._cancel_lane(other, "chunk_failed")
        elif got.combine_owed:
            # Every chunk is committed: tell the other lanes now (a NoWork
            # waits in their inbox and answers their next ask), so they end
            # DONE at once instead of after their wait - and their
            # credentials close with them. The job now owes its combine
            # (V0.10 G5): the dispatcher places it on the tick this wakes.
            for other in got.cancel_lanes:
                if other.attempt_id != lane.attempt_id:
                    self._drained(other, lane.job_id)
            if self._wake is not None:
                self._wake()

    def _count(
        self, live: LiveSession, body: ChunkResultIn, got: ResultOutcome
    ) -> None:
        lane = body.lane
        kind = got.kind
        if kind is ResultKind.FENCED:
            self._fenced(
                live,
                "chunk_result",
                str(got.fence or "fenced"),
                lane.attempt_id,
                chunk=body.chunk_index,
                lease_seq=body.lease_seq,
            )
            return
        if kind is ResultKind.COMMITTED:
            self.stats.committed += 1
            self.stats.late_commits += int(got.late)
            if got.copy_won is not None:
                self.stats.copies_won += int(got.copy_won)
                self.stats.originals_won += int(not got.copy_won)
                log.info(
                    "controller.split.speculation_resolved",
                    job_id=lane.job_id,
                    attempt_id=lane.attempt_id,
                    node_id=live.node_id,
                    chunk=body.chunk_index,
                    lease_seq=body.lease_seq,
                    winner="copy" if got.copy_won else "original",
                    losers=len(got.cancel_chunks),
                )
            if got.combine_owed:
                self.stats.jobs_done += 1
        elif kind is ResultKind.ALREADY_DONE:
            self.stats.already_done += 1
            self.stats.nondeterministic += int(got.nondeterministic)
        elif kind is ResultKind.RETRIED:
            self.stats.retried += 1
        elif kind is ResultKind.CHUNK_FAILED:
            self.stats.chunks_failed += 1
        else:
            self.stats.released += 1
        log.info(
            "controller.split.chunk_result",
            job_id=lane.job_id,
            attempt_id=lane.attempt_id,
            node_id=live.node_id,
            chunk=body.chunk_index,
            lease_seq=body.lease_seq,
            reported=body.outcome,
            applied=kind.value,
            late=got.late,
            job_state=None if got.job_ended is None else got.job_ended.value,
        )

    def _cancel_chunk(
        self, lane: LiveLane, job_id: str, item_id: int, seq: int, reason: str
    ) -> None:
        self._manager.send_command(
            lane.node_id,
            "cancel_chunk",
            agent_pb2.CancelChunk(
                job_id=job_id,
                attempt_id=lane.attempt_id,
                fencing_token=lane.fencing_token,
                item_id=item_id,
                lease_seq=seq,
                reason=reason,
            ),
            attempt_id=lane.attempt_id,
            fencing_token=lane.fencing_token,
        )

    def _cancel_lane(self, lane: LiveLane, reason: str) -> None:
        self._manager.send_command(
            lane.node_id,
            "cancel_attempt",
            agent_pb2.CancelAttempt(
                attempt_id=lane.attempt_id,
                fencing_token=lane.fencing_token,
                reason=reason,
            ),
            attempt_id=lane.attempt_id,
            fencing_token=lane.fencing_token,
        )

    def _drained(self, lane: LiveLane, job_id: str) -> None:
        self._manager.send_command(
            lane.node_id,
            "no_work",
            agent_pb2.NoWork(
                job_id=job_id,
                attempt_id=lane.attempt_id,
                fencing_token=lane.fencing_token,
                reason=agent_pb2.NoWork.REASON_DRAINED,
            ),
            attempt_id=lane.attempt_id,
            fencing_token=lane.fencing_token,
        )

    def _note_unverified(self, key: tuple[str, int]) -> None:
        """Count one tolerated unverified result of (lane, chunk) and keep that
        chunk away from that lane for ``wait_retry_s`` (no hot loop). Never
        evicted while the lane lives (:meth:`forget_lane` drops a lane's
        entries when it ends); with no room the caller fails safe."""
        self._unverified[key] = self._unverified.get(key, 0) + 1
        self._tracked_lanes.add(key[0])
        self._cooldown[key] = self._clock() + self._settings.wait_retry_s

    def forget_lane(self, attempt_id: str) -> None:
        """The lane ended: its unverified counters, cool-downs and stalled
        marks go."""
        for item_id in [
            k for k, v in self._stalls.items() if v.attempt_id == attempt_id
        ]:
            del self._stalls[item_id]
        if attempt_id not in self._tracked_lanes:
            return
        self._tracked_lanes.discard(attempt_id)
        for key in [k for k in self._unverified if k[0] == attempt_id]:
            del self._unverified[key]
        for key in [k for k in self._cooldown if k[0] == attempt_id]:
            del self._cooldown[key]

    def _avoid(self, attempt_id: str) -> frozenset[int]:
        """Chunks (item ids) this lane must not get again yet."""
        now = self._clock()
        for key in [k for k, until in self._cooldown.items() if until <= now]:
            del self._cooldown[key]
        return frozenset(i for (a, i) in self._cooldown if a == attempt_id)

    # ------------------------------------------------------------ D451 stalls

    def _stalled_for(
        self, job_id: str, node_id: str
    ) -> dict[int, tuple[int, float]] | None:
        """The job's stalled leases a lane on ``node_id`` may copy (another
        node's), ``item id -> (seq, stalled_s)``; None = none."""
        found = {
            item: (m.seq, m.stalled_s)
            for item, m in self._stalls.items()
            if m.job_id == job_id and m.node_id != node_id
        }
        return found or None

    async def _note_stalls(self, live: LiveSession, body: LeaseRenewIn) -> None:
        lane = body.lane
        for item_id, seq in body.leases:
            stalled_s = body.stalled.get((item_id, seq))
            mark = self._stalls.get(item_id)
            if stalled_s is None:
                if mark is not None and mark.seq == seq:
                    del self._stalls[item_id]
                    log.info(
                        "controller.split.chunk_resumed",
                        job_id=lane.job_id,
                        attempt_id=lane.attempt_id,
                        node_id=live.node_id,
                        item_id=item_id,
                        lease_seq=seq,
                    )
                continue
            key = (item_id, seq)
            self._stall_peaks[key] = max(self._stall_peaks.get(key, 0.0), stalled_s)
            self._stall_peaks.move_to_end(key)
            while len(self._stall_peaks) > _MAX_STALLS:
                self._stall_peaks.popitem(last=False)
            if mark is not None and mark.seq == seq:
                mark.stalled_s = stalled_s
                continue
            others = await self._store.other_lanes(
                lane.job_id, lane.attempt_id, live.node_id
            )
            self._stalls[item_id] = _Stall(
                lane.job_id, lane.attempt_id, live.node_id, seq, stalled_s
            )
            while len(self._stalls) > _MAX_STALLS:
                self._stalls.popitem(last=False)
            self.stats.stalls += 1
            self.stats.stalls_alone += int(others == 0)
            log.warning(
                "controller.split.chunk_stalled",
                job_id=lane.job_id,
                attempt_id=lane.attempt_id,
                node_id=live.node_id,
                item_id=item_id,
                lease_seq=seq,
                stalled_s=round(stalled_s, 1),
                other_lanes=others,
                action="alone" if others == 0 else "copy_first",
                hint="no other node's lane can take a copy; the lease is kept "
                "(a reclaim would run it again here) - check the node"
                if others == 0
                else "the next lane on another node that asks gets a copy first",
            )

    def _clear_stall(self, item_id: int, seq: int | None) -> None:
        mark = self._stalls.get(item_id)
        if mark is not None and (seq is None or mark.seq == seq):
            del self._stalls[item_id]

    async def _renew(self, live: LiveSession, body: LeaseRenewIn) -> None:
        lane = body.lane
        got = await self._store.renew(
            node_id=live.node_id,
            attempt_id=lane.attempt_id,
            token=lane.fencing_token,
            leases=body.leases,
            timeout_s=None,
            now=self._wall(),
        )
        if isinstance(got, FenceCause):
            self._fenced(live, "lease_renew", got.value, lane.attempt_id)
            return
        await self._note_stalls(live, body)
        self.stats.renewed += got[0]
        self.stats.renew_refused += got[1]
        if got[1]:
            log.info(
                "controller.split.renew_refused",
                job_id=lane.job_id,
                attempt_id=lane.attempt_id,
                node_id=live.node_id,
                refused=got[1],
            )

    async def parts_index(self, digest: str) -> PartsIndex:
        """V0.10 G5: a job's parts index (the combine's input and fetch scope)."""
        return await self._store.parts_index(digest)

    async def hold_lane(self, attempt_id: str, seconds: float) -> int:
        """V1.0 G10 (D445): keep a lane's leases through its node's grace
        (``seconds`` from now, plus the normal visibility timeout)."""
        return await self._store.hold_lane(
            attempt_id,
            timeout_s=min(86_400.0, seconds + self._settings.visibility_timeout_s),
            now=self._wall(),
        )

    async def held_leases(self, attempt_id: str) -> int:
        """Live leases of one lane (its upload grant's refresh quota)."""
        return await self._store.held_leases(attempt_id)

    # ------------------------------------------------------------ periodic

    async def expire(self) -> int:
        """Visibility timeout (D267b): overdue leases back to pending."""
        expired = await self._store.expire(now=self._wall())
        self.stats.expired += expired
        return expired
