"""One chunk of a lane (V0.10 G4, D259/D261; split out of :mod:`.lane` in fix
round 2): its control folder, one fresh process with the placeholders filled
in, the per-chunk write caps, then ``out/`` packed (refusing a swapped
folder), uploaded under the lane's grant and reported as a ``ChunkResult``."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meshforge.agent.cas.materialise import remove_subset
from meshforge.agent.execution.chunk_folder import ChunkFolder
from meshforge.agent.execution.chunk_inputs import chunk_env, substitute
from meshforge.agent.execution.failure_evidence import OutputTail, refine_result
from meshforge.agent.execution.fetch import FetchError, UploadError
from meshforge.agent.execution.lane_base import (
    CHUNK_FAILURE_NOT_RETRYABLE,
    LaneState,
)
from meshforge.agent.execution.lane_staging import ChunkStagingSteps
from meshforge.agent.execution.outputs import OutputError
from meshforge.agent.execution.runtime import RunResult, Stream
from meshforge.agent.execution.workdir_cap import WorkdirCap, stop_watch
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import failure_type_to_proto
from meshforge.common.split_chunks import ChunkSpan, chunk_items_digest

if TYPE_CHECKING:
    from meshforge.agent.execution.executor import _Attempt
    from meshforge.agent.execution.logs import AttemptLogs
    from meshforge.agent.execution.plan import AttemptPlan

__all__ = ["ChunkRunSteps"]

log = get_logger(__name__)


class ChunkRunSteps(ChunkStagingSteps):
    """See module docstring."""

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
        key = (int(chunk.item_id), int(chunk.lease_seq))
        if key in lane.cancelled_early:
            # V0.10 G6: it lost (CancelChunk) before its turn here - the
            # controller already superseded the lease; nothing to run or send
            del lane.cancelled_early[key]
            log.info(
                "agent.lane.chunk_skipped",
                node_id=self._node_id,
                job_id=attempt.job_id,
                attempt_id=attempt.attempt_id,
                chunk=int(chunk.chunk_index),
                lease_seq=key[1],
                reason="cancelled",
            )
            return
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
        folder = ChunkFolder(
            self.work_root,
            attempt.attempt_id,
            lines=items.over == "lines",
            docker=plan.runtime == "docker",
        )
        placed: list[Path] = []
        lane.current = (int(chunk.item_id), int(chunk.lease_seq))
        lane.cancelled_chunk = None
        bundle = self.work_root / f"{attempt.attempt_id}.chunk.bundle"
        try:
            try:
                placed = await self._chunk_inputs(attempt, plan, folder, span, mine)
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
                attempt, plan, runtime, on_output, stderr_tail, folder, chunk
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
                digest, size = await self._pack_upload(attempt, plan, folder, bundle)
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
            await asyncio.to_thread(_cleanup, folder, placed, workdir, bundle)

    async def _chunk_run(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        runtime: Any,
        on_output: Callable[[Stream, bytes], Awaitable[None]],
        stderr_tail: OutputTail | None,
        folder: ChunkFolder,
        chunk: agent_pb2.GrantedChunk,
    ) -> RunResult | None:
        """One fresh process for the chunk; None = cancelled by CancelChunk."""
        from meshforge.agent.execution.executor import _Observer

        lane = attempt.lane
        workdir = attempt.workdir
        assert lane is not None  # noqa: S101
        assert workdir is not None  # noqa: S101
        if lane.cancelled_chunk is not None and lane.cancelled_chunk == lane.current:
            return None  # cancelled while its inputs were staged: never started
        values = {
            "items_file": folder.seen_items,
            "chunk_index": str(int(chunk.chunk_index)),
            "out": folder.seen_out,
        }
        spec = dataclasses.replace(
            plan.spec,
            argv=substitute(plan.spec.argv, values),
            env=chunk_env(
                plan.spec.env,
                items_file=folder.seen_items,
                chunk_index=int(chunk.chunk_index),
                out=folder.seen_out,
            ),
            extra_mounts=folder.mounts,
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
        # the chunk's out/ lives outside the workdir: capped on its own, at
        # the chunk's output limit (plus the items file)
        out_cap = WorkdirCap(
            folder.root,
            int(lane.spec.max_chunk_output_bytes) or self._cfg.max_written_bytes,
            self._cfg.output_check_interval_s,
            clock=self._clock,
            free_floor_bytes=self.cas.store.free_disk_floor_bytes,
            max_free_read_failures=self._cfg.free_disk_read_failure_limit,
            free_read_timeout_s=self._cfg.free_disk_read_timeout_s,
        )
        out_cap.baseline = max(0, await self._step(attempt, out_cap.measure_once()))
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
        out_watch = asyncio.create_task(
            out_cap.run(over, lambda _free: over(-1), lambda _n: over(-2)),
            name=f"lane-out-cap:{attempt.attempt_id}",
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
            await stop_watch(out_watch)
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
        self, attempt: _Attempt, plan: AttemptPlan, folder: ChunkFolder, bundle: Path
    ) -> tuple[Digest, int]:
        lane = attempt.lane
        assert lane is not None  # noqa: S101
        cap = int(lane.spec.max_chunk_output_bytes) or 2**62
        digest, size = await self._step(
            attempt,
            asyncio.shield(asyncio.to_thread(folder.pack_out, bundle, cap)),
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


def _cleanup(
    folder: ChunkFolder, placed: list[Path], workdir: Path, bundle: Path
) -> None:
    """After a chunk: its control folder (never following a link out of the
    work root), its placed item files (never through a link the job made),
    the packed bundle."""
    folder.remove()
    remove_subset(placed, workdir)
    bundle.unlink(missing_ok=True)
