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
3. **One chunk** = a fresh ``<workdir>/.meshforge/chunk/`` (the items file and
   an empty ``out/``, :mod:`.chunk_inputs`), the chunk's item files placed
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
   process and drops its result.

A stop of the lane (cancel, agent stop, owner pre-emption) kills the running
chunk and ends the lane like any attempt; its leases are released by the
controller when it records the lane's end (never counted against the chunk).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import shutil
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from meshforge.agent.cas.materialise import remove_subset
from meshforge.agent.execution.chunk_inputs import (
    ChunkPaths,
    chunk_env,
    split_view_path,
    substitute,
    write_items_json,
    write_lines_chunk,
)
from meshforge.agent.execution.docker_runtime import CONTAINER_WORKDIR
from meshforge.agent.execution.failure_evidence import OutputTail, refine_result
from meshforge.agent.execution.fetch import (
    FetchError,
    StageReport,
    UploadError,
    blob_base_url,
    fetch_bundle,
    stage_entries,
    tree_sources,
)
from meshforge.agent.execution.outputs import OutputError, expand_outputs
from meshforge.agent.execution.runtime import RunResult, Stream
from meshforge.agent.execution.workdir_cap import WorkdirCap, stop_watch
from meshforge.common.bundle import BundleError, BundleLimits, pack_files
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import failure_type_to_proto
from meshforge.common.split_chunks import (
    MAX_ITEM_LIST_BYTES,
    ChunkSpan,
    ItemList,
    ItemListError,
    chunk_items_digest,
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

ASK_TIMEOUT_S: Final = 15.0
"""No answer to a ChunkRequest this long: ask again (the request is unbuffered)."""
RESULT_ACK_WAIT_S: Final = 30.0
"""How long a drained lane waits for its last results' Acks before it ends."""
_STAGE_RETRYABLE: Final = frozenset(
    {FailureType.TRANSFER_ERROR, FailureType.TIMEOUT, FailureType.UNAUTHORIZED}
)
CHUNK_FAILURE_NOT_RETRYABLE: Final = frozenset(
    {
        FailureType.INVALID_INPUT,
        FailureType.UNAUTHORIZED,
        FailureType.CONFIG_ERROR,
        FailureType.NO_FEASIBLE_PLAN,
    }
)
"""D267c: every other chunk failure may succeed on another node - the
controller's queue bounds the tries (``retry.max_attempts``, two nodes)."""


class _LaneStop(Exception):  # noqa: N818 - a control-flow signal
    """The controller said ``NoWork(stop)``: the lane ends ``cancelled``."""


@dataclass(slots=True)
class LaneState:
    """One lane's state on the agent (``_Attempt.lane``)."""

    spec: agent_pb2.LaneSpec
    items: ItemList | None = None
    by_tree: dict[str | None, frozenset[str]] = field(default_factory=dict)
    """``glob``: item paths per tree (``None`` = the project, else a mount)."""
    inbox: asyncio.Queue[agent_pb2.ControllerMessage] = field(
        default_factory=asyncio.Queue
    )
    held: dict[tuple[int, int], int] = field(default_factory=dict)
    """(item_id, lease_seq) -> chunk index of every lease the lane holds."""
    finished: set[tuple[int, int]] = field(default_factory=set)
    """Leases this lane already ran (a late duplicate grant is ignored)."""
    current: tuple[int, int] | None = None
    cancelled_chunk: tuple[int, int] | None = None
    """``CancelChunk`` asked to stop the running chunk."""
    run_task: asyncio.Future[Any] | None = None
    pending: dict[str, agent_pb2.ChunkResult] = field(default_factory=dict)
    """message_id -> ChunkResult not acked yet (re-sent until acked)."""
    acked: asyncio.Event = field(default_factory=asyncio.Event)
    chunks_run: int = 0
    env_s: float = 0.0
    capped: int | None = None
    """Bytes written when the per-chunk output cap stopped the chunk."""


class LaneSteps:
    """The lane part of the executor (see module docstring). Attributes and
    helpers are the executor's (declared for the type checker)."""

    _clock: Callable[[], float]
    _cfg: Any
    _fetch_cfg: Any
    _node_id: str
    _scheme: str
    _attempts: dict[str, _Attempt]
    _report: Callable[[str, Any], Awaitable[str]]
    _resend: Any
    _send_now: Callable[[str, Any], Awaitable[bool]] | None
    _controller_host: Callable[[], str | None]
    cas: Any
    work_root: Path

    if TYPE_CHECKING:

        async def _step[T](self, attempt: _Attempt, awaitable: Awaitable[T]) -> T: ...
        async def _send(
            self,
            attempt: _Attempt,
            state: AttemptState,
            *,
            failure: tuple[FailureType, str, bool] | None = None,
            exit_code: int | None = None,
            signal: int | None = None,
            output_digest: str = "",
            missing: Sequence[str] = (),
            logs: Mapping[Stream, agent_pb2.FinalLog] | None = None,
        ) -> None: ...
        async def _announce_run(self, attempt: _Attempt) -> None: ...
        async def _final_logs(
            self, attempt: _Attempt, plan: AttemptPlan, logs: AttemptLogs
        ) -> Mapping[Stream, agent_pb2.FinalLog]: ...
        async def _upload_retrying(
            self,
            attempt: _Attempt,
            plan: AttemptPlan,
            digest: Digest,
            path: Path,
            *,
            best_effort: bool = False,
        ) -> None: ...
        def _transport(self) -> dict[str, Any]: ...
        def _tuning(self) -> Any: ...
        def _token_lookup(self, attempt: _Attempt, tree_id: str) -> Any: ...
        def _stopped_error(self) -> Exception: ...
        def _current_spec(self, attempt: _Attempt) -> agent_pb2.ExecutionSpec: ...

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
        except _LaneStop:
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
                raise _LaneStop
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

    async def _chunk(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        logs: AttemptLogs,
        runtime: Any,
        on_output: Callable[[Stream, bytes], Awaitable[None]],
        stderr_tail: OutputTail | None,
        chunk: agent_pb2.GrantedChunk,
    ) -> None:
        lane = attempt.lane
        workdir = attempt.workdir
        assert lane is not None  # noqa: S101
        assert lane.items is not None  # noqa: S101
        assert workdir is not None  # noqa: S101
        result = agent_pb2.ChunkResult(
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.token,
            chunk_index=chunk.chunk_index,
            item_id=chunk.item_id,
            lease_seq=chunk.lease_seq,
        )
        started = self._clock()
        items = lane.items
        try:
            span = ChunkSpan(int(chunk.first_item), int(chunk.item_count))
            mine = items.chunk(span)
        except ValueError:
            mine = ()
        if not mine or str(chunk_items_digest(mine)) != chunk.items_digest:
            # the controller's list and ours disagree: never run other items
            await self._chunk_failed(
                attempt,
                result,
                FailureType.INTEGRITY_ERROR,
                "the granted chunk does not match this lane's item list",
                retryable=True,
            )
            return
        paths = ChunkPaths(
            workdir,
            lines=items.over == "lines",
            container_root=CONTAINER_WORKDIR if plan.runtime == "docker" else None,
        )
        placed: list[Path] = []
        lane.current = (int(chunk.item_id), int(chunk.lease_seq))
        lane.cancelled_chunk = None
        bundle = self.work_root / f"{attempt.attempt_id}.chunk.bundle"
        try:
            try:
                placed = await self._chunk_inputs(attempt, plan, paths, span, mine)
            except (FetchError, OSError, ValueError) as exc:
                if attempt.stop_reason is not None:
                    raise
                kind = getattr(exc, "failure_type", FailureType.TRANSFER_ERROR)
                await self._chunk_failed(
                    attempt,
                    result,
                    kind,
                    f"staging chunk {chunk.chunk_index}: {getattr(exc, 'message', exc)}",
                    retryable=kind not in CHUNK_FAILURE_NOT_RETRYABLE,
                )
                return
            stage_s = self._clock() - started
            await on_output(
                Stream.STDOUT,
                f"\n===== meshforge: chunk {chunk.chunk_index} "
                f"(items {span.first}..{span.stop - 1}) =====\n".encode(),
            )
            run = await self._chunk_run(
                attempt, plan, runtime, on_output, stderr_tail, paths, chunk
            )
            if run is None:
                return  # cancelled (CancelChunk): no result, the copy lost
            if not run.ok or lane.capped is not None:
                kind, retryable = _run_failure(run, lane.capped)
                await self._chunk_failed(
                    attempt,
                    result,
                    kind,
                    self._run_text(run, lane),
                    retryable=retryable,
                    exit_code=run.exit_code,
                    signal=run.signal,
                    phases={"stage_s": stage_s, "env_s": run.env_s, "run_s": run.run_s},
                )
                return
            uploaded = self._clock()
            try:
                digest, size = await self._pack_upload(attempt, plan, paths, bundle)
            except (OutputError, UploadError) as exc:
                await self._chunk_failed(
                    attempt,
                    result,
                    exc.failure_type,
                    f"chunk {chunk.chunk_index}: {exc.message}",
                    retryable=bool(getattr(exc, "retryable", False)),
                    exit_code=run.exit_code,
                )
                return
            result.outcome = agent_pb2.ChunkResult.OUTCOME_DONE
            result.result_digest = str(digest)
            result.result_bytes = size
            result.exit_code = run.exit_code if run.exit_code is not None else 0
            _phases(
                result,
                stage_s=stage_s,
                env_s=run.env_s,
                run_s=run.run_s,
                upload_s=self._clock() - uploaded,
            )
            await self._send_chunk_result(attempt, result)
        finally:
            lane.current = None
            lane.run_task = None
            lane.chunks_run += 1
            await asyncio.to_thread(_cleanup, paths.host_dir, placed, workdir, bundle)

    async def _chunk_inputs(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        paths: ChunkPaths,
        span: ChunkSpan,
        mine: tuple[str | int, ...],
    ) -> list[Path]:
        """The chunk's folder, its items file and (``glob``) its item files."""
        lane = attempt.lane
        workdir = attempt.workdir
        assert lane is not None  # noqa: S101
        assert lane.items is not None  # noqa: S101
        assert workdir is not None  # noqa: S101
        items = lane.items
        await asyncio.to_thread(_fresh_dir, paths.host_dir, paths.host_out)
        if items.over == "lines":
            source = items.source
            assert source is not None  # noqa: S101 - parse_item_list checked
            records = items.records[span.first : span.stop]

            def write_lines() -> int:
                with self.cas.store.open_read(Digest(source[1])) as fh:
                    return write_lines_chunk(
                        paths.host_items, fh, header=items.header, records=records
                    )

            await asyncio.to_thread(write_lines)
            return []
        await asyncio.to_thread(write_items_json, paths.host_items, mine)
        if items.over != "glob":
            return []
        return await self._place_items(attempt, plan, [str(i) for i in mine])

    async def _place_items(
        self, attempt: _Attempt, plan: AttemptPlan, wanted: list[str]
    ) -> list[Path]:
        """D264: the chunk's item files the lane did not stage, fetched (only
        those blobs) and placed read-only at their paths."""
        workdir = attempt.workdir
        assert workdir is not None  # noqa: S101
        mounts = [i.mount for i in self._current_spec(attempt).inputs]
        placed: list[Path] = []
        stages: list[StageReport] = list(attempt.stages)
        for index, report in enumerate(stages):
            mount = None if index == 0 else mounts[index - 1]
            rels = [
                rel
                for m, rel in (split_view_path(p, mounts) for p in wanted)
                if m == mount and rel in report.skipped
            ]
            if not rels:
                continue
            dest = workdir if mount is None else workdir.joinpath(*mount.split("/"))
            got = await self._stage_entries_retrying(
                attempt, report.tree, rels, dest, input_index=index - 1
            )
            placed.extend(got)
        return placed

    async def _stage_entries_retrying(
        self,
        attempt: _Attempt,
        tree: Tree,
        rels: list[str],
        dest: Path,
        *,
        input_index: int,
    ) -> list[Path]:
        """:func:`stage_entries` with the staging retry rule (D95): a refused
        token waits for ``RefreshFetchToken``, an unreachable controller is
        retried with backoff, both for at most ``staging_retry_s``."""
        deadline = self._clock() + self._cfg.staging_retry_s
        delay = 0.25
        while True:
            used = attempt.fetch_token
            attempt.token_changed.clear()
            spec = self._current_spec(attempt)
            sources = tree_sources(
                spec,
                self._controller_host(),
                sources=None if input_index < 0 else spec.inputs[input_index].sources,
                scheme=self._scheme,
            )
            try:
                got = await self._step(
                    attempt,
                    stage_entries(
                        self.cas,
                        tree=tree,
                        paths=rels,
                        sources=sources,
                        dest=dest,
                        timeout_s=self._fetch_cfg.request_timeout_s,
                        tuning=self._tuning(),
                        token_for=self._token_lookup(attempt, str(tree.tree_id)),
                        **self._transport(),
                    ),
                )
            except FetchError as exc:
                if (
                    exc.failure_type not in _STAGE_RETRYABLE
                    or self._clock() >= deadline
                ):
                    if exc.failure_type is FailureType.UNAUTHORIZED:
                        raise FetchError(
                            exc.message, failure_type=FailureType.TRANSFER_ERROR
                        ) from None
                    raise
                if attempt.fetch_token == used:
                    wait = min(delay, max(0.0, deadline - self._clock()))
                    with contextlib.suppress(TimeoutError):
                        await self._step(
                            attempt,
                            asyncio.wait_for(attempt.token_changed.wait(), wait),
                        )
                delay = min(delay * 2, 5.0)
                continue
            return list(got.placed)

    async def _chunk_run(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        runtime: Any,
        on_output: Callable[[Stream, bytes], Awaitable[None]],
        stderr_tail: OutputTail | None,
        paths: ChunkPaths,
        chunk: agent_pb2.GrantedChunk,
    ) -> RunResult | None:
        """One fresh process for the chunk; None = cancelled by CancelChunk."""
        from meshforge.agent.execution.executor import _Observer

        lane = attempt.lane
        workdir = attempt.workdir
        assert lane is not None  # noqa: S101
        assert workdir is not None  # noqa: S101
        values = {
            "items_file": paths.seen_items,
            "chunk_index": str(int(chunk.chunk_index)),
            "out": paths.seen_out,
        }
        spec = dataclasses.replace(
            plan.spec,
            argv=substitute(plan.spec.argv, values),
            env=chunk_env(
                plan.spec.env,
                items_file=paths.seen_items,
                chunk_index=int(chunk.chunk_index),
                out=paths.seen_out,
            ),
        )
        if stderr_tail is not None:
            stderr_tail.clear()
        observer = _Observer(self._clock)
        lane.capped = None
        cap = WorkdirCap(
            workdir,
            self._cfg.max_written_bytes,
            self._cfg.output_check_interval_s,
            clock=self._clock,
            free_floor_bytes=self.cas.store.free_disk_floor_bytes,
            max_free_read_failures=self._cfg.free_disk_read_failure_limit,
            free_read_timeout_s=self._cfg.free_disk_read_timeout_s,
        )
        cap.baseline = max(0, await self._step(attempt, cap.measure_once()))
        run: asyncio.Future[RunResult] = asyncio.ensure_future(
            runtime.run(spec, on_output, observer=observer)
        )
        lane.run_task = run
        attempt.step = run

        def over(written: int) -> None:
            lane.capped = written
            run.cancel()

        watch = asyncio.create_task(
            cap.run(over, lambda _free: over(-1), lambda _n: over(-2)),
            name=f"lane-cap:{attempt.attempt_id}",
        )
        try:
            result = await run
        except asyncio.CancelledError:
            if not run.cancelled():
                raise
            result = observer.cancelled_result()
        finally:
            attempt.step = None
            await stop_watch(watch)
        lane.env_s += result.env_s
        attempt.env_hash = result.env_hash or attempt.env_hash
        if attempt.stop_reason is not None:
            raise self._stopped_error()
        if lane.cancelled_chunk == lane.current:
            return None
        if stderr_tail is not None:
            result = refine_result(result, stderr_tail.data)  # D116
        return result

    def _run_text(self, run: RunResult, lane: LaneState) -> str:
        if lane.capped is not None and lane.capped < 0:
            return (
                "disk_floor: the disk of this node's workdirs ran low (or could not "
                "be read) while the chunk ran; stopped"
            )
        if lane.capped is not None:
            return (
                "output_limit: the chunk wrote more than "
                f"{self._cfg.max_written_bytes} bytes to the lane's workdir, or "
                "its disk ran low ([agent.execution] max_written_bytes); stopped"
            )
        return f"{run.reason}: {run.detail}" if run.reason else run.detail or "failed"

    async def _pack_upload(
        self, attempt: _Attempt, plan: AttemptPlan, paths: ChunkPaths, bundle: Path
    ) -> tuple[Digest, int]:
        lane = attempt.lane
        assert lane is not None  # noqa: S101
        cap = int(lane.spec.max_chunk_output_bytes) or 2**62
        digest, size = await self._step(
            attempt,
            asyncio.shield(asyncio.to_thread(_pack_out, paths.host_out, bundle, cap)),
        )
        if not plan.upload_token:
            raise OutputError(
                "the controller gave this lane no upload grant", retryable=False
            )
        attempt.upload_deadline = None  # each chunk's upload has its own budget
        await self._upload_retrying(attempt, plan, digest, bundle)
        return digest, size

    async def _chunk_failed(
        self,
        attempt: _Attempt,
        result: agent_pb2.ChunkResult,
        kind: FailureType,
        text: str,
        *,
        retryable: bool,
        exit_code: int | None = None,
        signal: int | None = None,
        phases: Mapping[str, float] | None = None,
    ) -> None:
        result.outcome = agent_pb2.ChunkResult.OUTCOME_FAILED
        result.failure.CopyFrom(
            common_pb2.Failure(
                failure_type=failure_type_to_proto(kind),
                message="".join(c if c.isprintable() else "?" for c in text)[:1024],
                retryable=retryable,
            )
        )
        if exit_code is not None:
            result.exit_code = exit_code
        if signal is not None and 1 <= signal <= 64:
            result.signal = signal
        if phases:
            _phases(result, **phases)
        await self._send_chunk_result(attempt, result)

    async def _send_chunk_result(
        self, attempt: _Attempt, result: agent_pb2.ChunkResult
    ) -> None:
        lane = attempt.lane
        assert lane is not None  # noqa: S101
        message_id = await self._report("chunk_result", result)
        lane.pending[message_id] = result
        log.info(
            "agent.lane.chunk_result",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            chunk=result.chunk_index,
            lease_seq=result.lease_seq,
            outcome=agent_pb2.ChunkResult.Outcome.Name(result.outcome),
            failure_type=None
            if not result.HasField("failure")
            else int(result.failure.failure_type),
        )

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


def _run_failure(run: RunResult, capped: int | None) -> tuple[FailureType, bool]:
    """A failed chunk's type and whether another node may succeed (D267c)."""
    if capped is not None:
        # past the output cap: as large anywhere; a full disk is this node's
        return (
            (FailureType.DISK_FULL, True)
            if capped < 0
            else (
                FailureType.OUTPUT_ERROR,
                False,
            )
        )
    kind = run.failure_type or FailureType.INTERNAL_ERROR
    return kind, kind not in CHUNK_FAILURE_NOT_RETRYABLE


def _phases(result: agent_pb2.ChunkResult, **values: float) -> None:
    for name, value in values.items():
        setattr(result.phases, name, max(0.0, float(value)))


def _fresh_dir(folder: Path, out: Path) -> None:
    if folder.exists() or folder.is_symlink():
        _remove(folder)
    out.mkdir(parents=True)


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    shutil.rmtree(path, ignore_errors=True)


def _cleanup(folder: Path, placed: list[Path], workdir: Path, bundle: Path) -> None:
    with contextlib.suppress(OSError):
        _remove(folder)
    remove_subset(placed, workdir)
    with contextlib.suppress(OSError):
        bundle.unlink(missing_ok=True)


def _pack_out(out: Path, dest: Path, max_bytes: int) -> tuple[Digest, int]:
    """Pack every file of the chunk's ``out/`` (the deterministic bundle of
    V0.4 outputs; an empty folder is an empty bundle)."""
    dest.unlink(missing_ok=True)
    files, _missing = expand_outputs(out, ("**",))
    try:
        with dest.open("xb") as fh:
            packed = pack_files(
                out, files, fh, limits=BundleLimits(max_total_bytes=max_bytes)
            )
    except BundleError as exc:
        dest.unlink(missing_ok=True)
        raise OutputError(
            f"cannot pack the chunk's output: {exc.message}", retryable=False
        ) from None
    if packed.compressed_bytes > max_bytes:
        dest.unlink(missing_ok=True)
        raise OutputError(
            f"output_limit: the chunk's packed output is {packed.compressed_bytes} "
            f"bytes, more than split.max_chunk_output_bytes ({max_bytes})",
            retryable=False,
        )
    return packed.digest, packed.compressed_bytes
