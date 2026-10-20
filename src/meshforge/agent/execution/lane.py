"""A Mode 3 lane on the agent (V0.10 G4, D259/D261/D263/D264; ADR-0019).

A ``StartAttempt`` with ``lane`` set is one node's share of a split job. It is
an ordinary attempt for everything around it - owner policy, secrets, the
sandboxed runtime, staging with the fetch token, the upload grant, cancel,
agent stop, Reconcile and the terminal report - and :class:`LaneSteps` (a part
of :class:`~meshforge.agent.execution.executor.Executor`, like the
controller's dispatcher steps) adds what differs:

1. **Before staging** the job's item list is downloaded with the lane's fetch
   token and verified against ``LaneSpec.items_digest``. For ``glob`` items the
   lane then stages the project (and named inputs) **without the item files**
   (D264): each chunk fetches and places only its own items. An item that is a
   symlink in its tree turns this off for that tree (everything is staged, as
   for a whole job) - never a dangling link.
2. **Pull loop** (D263): ``ChunkRequest`` (unbuffered, re-sent if no answer
   comes) -> ``ChunkGrant`` / ``NoWork``. ``wait`` sleeps ``retry_after_s``;
   ``drained`` ends the lane ``DONE``; ``stop`` stops it (``cancelled``).
   A grant whose slice of the item list does not hash to its ``items_digest``
   is refused (``integrity_error``) - the two sides never disagree silently.
3. **One chunk** = a fresh agent-owned control folder **outside** the
   workdir (the items file and an empty ``out/``, :mod:`.chunk_folder`; fix r2
   B1), the chunk's item files placed
   read-only (``glob``), **one fresh process** of the entrypoint with the
   placeholders / ``MESHFORGE_CHUNK_*`` filled in, in the lane's prepared
   runtime and env (the env is cached by hash: the first chunk builds it, the
   rest reuse it). Its output streams go to the lane's logs between chunk
   marker lines. A clean exit -> ``out/`` packed (the deterministic bundle of
   the V0.4 outputs; at most ``max_chunk_output_bytes``), uploaded under the
   lane's grant, ``ChunkResult(done, digest)``; any other end ->
   ``ChunkResult(failed, failure_type)``. The chunk's folder and item files
   are removed after it. Each chunk's workdir writes are capped like a whole
   job's (``max_written_bytes``, D143): past it the chunk fails
   ``output_error``.
4. ``ChunkResult`` is a **report** (buffered, replayed after a reconnect,
   re-sent until acked); ``LeaseRenew`` goes out every third of the lease
   timeout for the leases the lane holds; ``CancelChunk`` kills that chunk's
   process and drops its result (a chunk cancelled before it started here -
   a speculative copy or original that lost while its grant was on the way,
   V0.10 G6 - is skipped: no process, no result).

Code: :mod:`.lane_base` (state, constants, the executor surface the steps
use), :mod:`.lane_staging` (a chunk's inputs), :mod:`.lane_chunk` (one chunk
run, pack, upload, result) and this module (commands, pull loop).

A stop of the lane (cancel, agent stop, owner pre-emption) kills the running
chunk and ends the lane like any attempt; its leases are released by the
controller when it records the lane's end (never counted against the chunk).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from meshforge.agent.execution.chunk_inputs import split_view_path
from meshforge.agent.execution.failure_evidence import OutputTail
from meshforge.agent.execution.fetch import (
    FetchError,
    blob_base_url,
    fetch_bundle,
)
from meshforge.agent.execution.lane_base import (
    ASK_TIMEOUT_S,
    MAX_EARLY_CANCELS,
    RESULT_ACK_WAIT_S,
    LaneState,
    LaneStopError,
)
from meshforge.agent.execution.lane_chunk import ChunkRunSteps
from meshforge.agent.execution.runtime import Stream
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.split_chunks import (
    MAX_ITEM_LIST_BYTES,
    ItemListError,
    parse_item_list,
)
from meshforge.common.states import AttemptState
from meshforge.common.tree import Tree

if TYPE_CHECKING:
    from meshforge.agent.execution.executor import _Attempt, _Observer
    from meshforge.agent.execution.logs import AttemptLogs
    from meshforge.agent.execution.plan import AttemptPlan

__all__ = ["LaneState", "LaneSteps"]

log = get_logger(__name__)


class LaneSteps(ChunkRunSteps):
    """The lane part of the executor (see module docstring)."""

    # ------------------------------------------------------------ commands

    def _lane_command(self, msg: agent_pb2.ControllerMessage) -> None:
        """``chunk_grant`` / ``no_work`` / ``cancel_chunk`` for a held lane."""
        which = msg.WhichOneof("payload")
        inner = getattr(msg, which or "")
        attempt = self._attempts.get(getattr(inner, "attempt_id", ""))
        lane = None if attempt is None else attempt.lane
        if (
            attempt is None
            or lane is None
            or attempt.token != inner.fencing_token
            or attempt.ended
        ):
            log.info(
                "agent.lane.command_ignored",
                node_id=self._node_id,
                payload=which,
                attempt_id=str(getattr(inner, "attempt_id", ""))[:40] or None,
                known=attempt is not None,
            )
            return
        if which == "cancel_chunk":
            key = (int(inner.item_id), int(inner.lease_seq))
            if lane.current == key:
                lane.cancelled_chunk = key
                if lane.run_task is not None and not lane.run_task.done():
                    lane.run_task.cancel()
            elif key not in lane.finished:
                # V0.10 G6: not started here yet (a grant still in the inbox)
                lane.cancelled_early[key] = None
                while len(lane.cancelled_early) > MAX_EARLY_CANCELS:
                    lane.cancelled_early.popitem(last=False)
            log.info(
                "agent.lane.chunk_cancel",
                node_id=self._node_id,
                job_id=attempt.job_id,
                attempt_id=attempt.attempt_id,
                item_id=key[0],
                lease_seq=key[1],
                running=lane.current == key,
                reason=inner.reason[:64] or None,
            )
            return
        lane.inbox.put_nowait(msg)

    def _lane_result_acked(self, message_id: str) -> bool:
        for attempt in self._attempts.values():
            lane = attempt.lane
            if lane is not None and lane.pending.pop(message_id, None) is not None:
                lane.acked.set()
                return True
        return False

    # ------------------------------------------------------------ before staging

    async def _lane_prepare(
        self, attempt: _Attempt, plan: AttemptPlan, spec: agent_pb2.ExecutionSpec
    ) -> None:
        """Download and verify the item list; the item paths per tree."""
        lane = attempt.lane
        assert lane is not None  # noqa: S101 - the caller checked
        try:
            digest = Digest.parse(lane.spec.items_digest)
        except ValueError:
            raise FetchError(
                "LaneSpec.items_digest is not sha256:<64 hex>",
                failure_type=FailureType.INVALID_INPUT,
            ) from None
        dest = self.work_root / f"{attempt.attempt_id}.items"
        base = blob_base_url(
            plan.blob_url if attempt.blob_url is None else attempt.blob_url,
            self._controller_host(),
            plan.blob_port if attempt.blob_port is None else attempt.blob_port,
            scheme=self._scheme,
        )
        try:
            await self._step(
                attempt,
                fetch_bundle(
                    base,
                    digest,
                    attempt.fetch_token or plan.fetch_token,
                    dest,
                    max_bytes=MAX_ITEM_LIST_BYTES,
                    **self._transport(),
                ),
            )
            data = await asyncio.to_thread(dest.read_bytes)
        finally:
            with contextlib.suppress(OSError):
                dest.unlink()
        try:
            items = await asyncio.to_thread(parse_item_list, data)
        except ItemListError as exc:
            raise FetchError(
                f"the job's item list is malformed: {exc}",
                failure_type=FailureType.INTEGRITY_ERROR,
            ) from None
        if items.count != lane.spec.item_count:
            raise FetchError(
                "the job's item list does not have LaneSpec.item_count items",
                failure_type=FailureType.INTEGRITY_ERROR,
            )
        lane.items = items
        if items.over == "glob":
            mounts = [i.mount for i in spec.inputs]
            by_tree: dict[str | None, set[str]] = {}
            for item in items.items:
                mount, rel = split_view_path(str(item), mounts)
                by_tree.setdefault(mount, set()).add(rel)
            lane.by_tree = {k: frozenset(v) for k, v in by_tree.items()}
        log.info(
            "agent.lane.items",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            over=items.over,
            items=items.count,
        )

    @staticmethod
    def _lane_skip(
        attempt: _Attempt, mount: str | None
    ) -> Callable[[Tree], frozenset[str]] | None:
        """``stage_tree(skip=...)`` for one tree of a lane (D264): its item
        files - unless one of them is not a plain file there (then none)."""
        lane = attempt.lane
        if lane is None or not lane.by_tree.get(mount):
            return None
        wanted = lane.by_tree[mount]

        def skip(tree: Tree) -> frozenset[str]:
            kinds = {e.path: e.kind for e in tree.entries}
            if any(kinds.get(p) != "file" for p in wanted):
                return frozenset()
            return wanted

        return skip

    # ------------------------------------------------------------ the lane

    async def _run_lane(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        logs: AttemptLogs,
        runtime: Any,
        observer: _Observer,
        on_output: Callable[[Stream, bytes], Awaitable[None]],
        stderr_tail: OutputTail | None = None,
        drain: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """The body of a lane's run (``Executor._execute``'s ``body``)."""

        lane = attempt.lane
        assert lane is not None  # noqa: S101
        assert lane.items is not None  # noqa: S101
        del observer  # one fresh observer per chunk
        await self._announce_run(attempt)
        started = self._clock()
        renewer = asyncio.create_task(
            self._renew_loop(attempt), name=f"lane-renew:{attempt.attempt_id}"
        )
        stopped = False
        try:
            await self._pull(attempt, plan, logs, runtime, on_output, stderr_tail)
        except LaneStopError:
            stopped = True
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            if drain is not None:
                await drain()
        attempt.phases["run_s"] = self._clock() - started
        attempt.phases["env_s"] = lane.env_s
        if stopped:
            if attempt.stop_reason is None:
                attempt.stop_reason = "cancelled"
            raise self._stopped_error()
        await self._await_results(attempt)
        final = await self._final_logs(attempt, plan, logs)
        attempt.phases["collect_s"] = 0.0
        await self._send(attempt, AttemptState.DONE, exit_code=0, logs=final)

    async def _pull(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        logs: AttemptLogs,
        runtime: Any,
        on_output: Callable[[Stream, bytes], Awaitable[None]],
        stderr_tail: OutputTail | None,
    ) -> None:
        lane = attempt.lane
        assert lane is not None  # noqa: S101
        while True:
            reply = await self._ask(attempt)
            if reply.WhichOneof("payload") == "no_work":
                reason = reply.no_work.reason
                if reason == agent_pb2.NoWork.REASON_DRAINED:
                    log.info(
                        "agent.lane.drained",
                        node_id=self._node_id,
                        job_id=attempt.job_id,
                        attempt_id=attempt.attempt_id,
                        chunks=lane.chunks_run,
                    )
                    return
                if reason == agent_pb2.NoWork.REASON_WAIT:
                    wait = min(max(reply.no_work.retry_after_s, 0.05), 60.0)
                    await self._step(attempt, asyncio.sleep(wait))
                    continue
                raise LaneStopError
            for chunk in reply.chunk_grant.chunks:
                key = (int(chunk.item_id), int(chunk.lease_seq))
                if key in lane.finished or key in lane.held:
                    continue  # a late duplicate of a grant this lane had
                lane.held[key] = int(chunk.chunk_index)
                try:
                    await self._chunk(
                        attempt, plan, logs, runtime, on_output, stderr_tail, chunk
                    )
                finally:
                    lane.held.pop(key, None)
                    lane.finished.add(key)

    async def _ask(self, attempt: _Attempt) -> agent_pb2.ControllerMessage:
        """One ChunkRequest and its answer (re-sent when none comes)."""
        lane = attempt.lane
        assert lane is not None  # noqa: S101
        request = agent_pb2.ChunkRequest(
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.token,
            want=1,
            held=[
                agent_pb2.ChunkLeaseRef(item_id=i, lease_seq=s) for i, s in lane.held
            ],
        )
        while True:
            if self._send_now is not None:
                await self._send_now("chunk_request", request)
            try:
                return await self._step(
                    attempt, asyncio.wait_for(lane.inbox.get(), ASK_TIMEOUT_S)
                )
            except TimeoutError:
                log.info(
                    "agent.lane.ask_again",
                    node_id=self._node_id,
                    attempt_id=attempt.attempt_id,
                )

    async def _renew_loop(self, attempt: _Attempt) -> None:
        lane = attempt.lane
        assert lane is not None  # noqa: S101
        every = max(1.0, float(lane.spec.lease_timeout_s or 60.0) / 3)
        while True:
            await asyncio.sleep(every)
            if not lane.held or self._send_now is None:
                continue
            await self._send_now(
                "lease_renew",
                agent_pb2.LeaseRenew(
                    job_id=attempt.job_id,
                    attempt_id=attempt.attempt_id,
                    fencing_token=attempt.token,
                    leases=[
                        agent_pb2.ChunkLeaseRef(item_id=i, lease_seq=s)
                        for i, s in lane.held
                    ],
                ),
            )

    # ------------------------------------------------------------ one chunk

    async def _await_results(self, attempt: _Attempt) -> None:
        """Before ``DONE``: the lane's last results acked (re-sent while not),
        bounded by :data:`RESULT_ACK_WAIT_S` - they stay in the replay ring
        either way."""
        lane = attempt.lane
        assert lane is not None  # noqa: S101
        deadline = self._clock() + RESULT_ACK_WAIT_S
        while lane.pending and self._clock() < deadline:
            lane.acked.clear()
            with contextlib.suppress(TimeoutError):
                await self._step(
                    attempt,
                    asyncio.wait_for(lane.acked.wait(), self._cfg.terminal_resend_s),
                )
            for message_id, result in list(lane.pending.items()):
                if lane.pending.get(message_id) is None:
                    continue
                if self._resend is None or not await self._resend(message_id):
                    lane.pending.pop(message_id, None)
                    new_id = await self._report("chunk_result", result)
                    lane.pending[new_id] = result
