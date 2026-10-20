"""A split job's combine on the agent (V0.10 G5, D268/D283; ADR-0019).

A ``StartAttempt`` with ``combine`` set is the last step of a split job. It is
an ordinary attempt for everything around it - owner policy, secrets, staging
with the fetch token and its retries (D95), the upload grant, cancel, agent
stop, Reconcile, the terminal report - and :class:`CombineSteps` (a part of
:class:`~meshforge.agent.execution.executor.Executor`, like the lane steps)
adds what differs:

1. **Staging.** A combine *script* first stages the project like a whole job
   (its env is built by the runtime, as for any run); a *built-in* stages no
   project (it reads only the parts; its workdir is an empty folder for its
   logs). Then the **parts index** is downloaded and verified (digest, then
   :func:`~meshforge.common.split_chunks.parse_parts_index`; exactly
   ``CombineSpec.chunk_count`` chunks) and every chunk's result - a plain blob,
   verified against its digest - is unpacked hostile-input-safe into the
   agent-owned :class:`~.combine_folder.CombineFolder` as ``parts/NNNNN`` in
   chunk order, then made read-only. A result that is not a safe bundle is the
   job's data, not a transfer problem: ``combine_error`` (``unsafe_part``).
   A staging retry (controller restart, a refused token) resumes after the
   parts already unpacked.
2. **Run.** A built-in (``concat`` / ``merge-csv`` / ``merge-json``) runs as
   ``python -m meshforge.agent.execution.combine`` with **the agent's
   interpreter** in its own process (trusted MeshForge code: no user code, no
   Docker image; the subprocess runtime's process tree, time limit = the job's
   ``limits.timeout``, cancel and kill), writing ``out/``; its refusal is
   ``combine_error`` with the built-in's reason (``header_mismatch``,
   ``duplicate_key``, ...). A script runs in the job's own runtime (the Docker
   sandbox when there is one) with ``{parts}`` / ``{out}`` filled in and
   ``MESHFORGE_COMBINE_PARTS`` / ``MESHFORGE_COMBINE_OUT`` (Docker:
   ``/meshforge/parts`` read-only, ``/meshforge/out`` read-write); a non-zero
   exit is ``combine_error`` (``script_failed``); its workdir is capped like a
   job's (D143).
3. **Output.** ``out/`` is packed (the deterministic bundle of V0.4 outputs;
   the folder checked unswapped before and after; at most the job's
   ``max_output_bytes``, else ``combine_error`` ``too_large``), uploaded under
   the attempt's grant and reported ``DONE`` with its digest - the job's output.

The control folder is removed when the attempt ends (every path).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import meshforge
from meshforge.agent.execution.combine import EXIT_COMBINE_ERROR
from meshforge.agent.execution.combine_folder import (
    CombineFolder,
    combine_argv,
    combine_env,
)
from meshforge.agent.execution.failure_evidence import OutputTail, refine_result
from meshforge.agent.execution.fetch import FetchError, blob_base_url
from meshforge.agent.execution.lane import LaneSteps
from meshforge.agent.execution.outputs import OutputError
from meshforge.agent.execution.plan import runtime_argv, workload_env
from meshforge.agent.execution.runtime import RunLimits, RunResult, RunSpec, Stream
from meshforge.agent.execution.workdir_cap import stop_watch
from meshforge.common.bundle import BundleError, BundleLimits, unpack
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.logging import get_logger
from meshforge.common.manifest import DEFAULT_CHUNK_OUTPUT_BYTES
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.split_chunks import (
    MAX_PARTS_INDEX_BYTES,
    ItemListError,
    PartsIndex,
    parse_parts_index,
)
from meshforge.common.states import AttemptState

if TYPE_CHECKING:
    from meshforge.agent.execution.executor import _Attempt, _Observer
    from meshforge.agent.execution.logs import AttemptLogs
    from meshforge.agent.execution.plan import AttemptPlan
    from meshforge.agent.execution.workdir_cap import WorkdirCap

__all__ = ["CombineState", "CombineSteps"]

log = get_logger(__name__)

_BUILTIN_MODULE: Final = "meshforge.agent.execution.combine"
_RESULT_MAX_BYTES: Final = 64 * 1024
_BUILTIN_MEMORY_BYTES: Final = 512 * 1024 * 1024
"""The built-in's planned memory (not enforced: it is bounded by design)."""


class CombineRefusedError(FetchError):
    """The combine's input is the job's data and cannot be combined
    (``combine_error``; staging never retries it)."""

    default_failure_type = FailureType.COMBINE_ERROR


@dataclass(slots=True)
class CombineState:
    """One combine on the agent (``_Attempt.combine``)."""

    spec: agent_pb2.CombineSpec
    index: PartsIndex | None = None
    folder: CombineFolder | None = None
    script: tuple[str, ...] | None = None
    """The user's combine script argv; None = a built-in."""
    builtin: str = "concat"
    stage_s: float = 0.0


def _meshforge_root() -> str:
    """The folder holding the ``meshforge`` package the agent runs from (the
    built-in's ``PYTHONPATH``: a source checkout works like an install)."""
    return str(Path(meshforge.__file__).resolve().parent.parent)


class CombineSteps(LaneSteps):
    """The combine part of the executor (see module docstring)."""

    if TYPE_CHECKING:
        _settings: Any
        _setup: Any
        _combine_runtime: Any
        _fetch: Callable[..., Awaitable[int]]

        async def _stage(
            self,
            attempt: _Attempt,
            plan: AttemptPlan,
            workdir: Path,
            spec: agent_pb2.ExecutionSpec,
            *,
            once: Callable[[str], Awaitable[None]] | None = None,
        ) -> None: ...
        async def _stopped(
            self, attempt: _Attempt, *, exit_code: int | None, signal: int | None
        ) -> None: ...
        async def _output_cap(self, attempt: _Attempt) -> WorkdirCap | None: ...
        def _start_output_cap(
            self, attempt: _Attempt, cap: WorkdirCap | None
        ) -> asyncio.Task[None] | None: ...
        def _send_log(self, attempt: _Attempt, chunk: Any) -> None: ...

    # ------------------------------------------------------------ staging

    async def _combine_stage(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        workdir: Path,
        spec: agent_pb2.ExecutionSpec,
    ) -> None:
        """STAGING of a combine (module docstring, step 1)."""
        combine = attempt.combine
        assert combine is not None  # noqa: S101 - the caller checked
        split = plan.manifest.split
        if split is None:
            raise FetchError(
                "a combine was sent for a job that is not split",
                failure_type=FailureType.INVALID_INPUT,
            )
        combine.script = split.combine.script
        combine.builtin = split.combine.builtin or "script"
        if combine.script is not None:
            await self._stage(attempt, plan, workdir, spec)
        else:
            await asyncio.to_thread(_empty_workdir, workdir)
        part_cap = int(split.max_chunk_output_bytes or DEFAULT_CHUNK_OUTPUT_BYTES)

        async def once(token: str) -> None:
            await self._combine_parts_once(attempt, plan, token, part_cap)

        await self._stage(attempt, plan, workdir, spec, once=once)
        folder = combine.folder
        assert folder is not None  # noqa: S101 - made by the first try
        await asyncio.to_thread(folder.seal)
        log.info(
            "agent.combine.staged",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            parts=folder.chunks,
            combine=combine.builtin,
        )

    def _combine_base(self, attempt: _Attempt, plan: AttemptPlan) -> str:
        return blob_base_url(
            plan.blob_url if attempt.blob_url is None else attempt.blob_url,
            self._controller_host(),
            plan.blob_port if attempt.blob_port is None else attempt.blob_port,
            scheme=self._scheme,
        )

    async def _combine_parts_once(
        self, attempt: _Attempt, plan: AttemptPlan, token: str, part_cap: int
    ) -> None:
        """One try: the parts index (once), then every part not unpacked yet."""
        combine = attempt.combine
        assert combine is not None  # noqa: S101
        base = self._combine_base(attempt, plan)
        if combine.index is None:
            combine.index = await self._parts_index(attempt, base, token)
            combine.folder = CombineFolder(
                self.work_root,
                attempt.attempt_id,
                chunks=len(combine.index.chunks),
                docker=combine.script is not None and plan.runtime == "docker",
            )
            await asyncio.to_thread(combine.folder.create)
        folder = combine.folder
        assert folder is not None  # noqa: S101
        bundle = self.work_root / f"{attempt.attempt_id}.part"
        limits = BundleLimits(max_total_bytes=part_cap)
        for index, (_first, _count, digest) in enumerate(combine.index.chunks):
            if await asyncio.to_thread(folder.has_part, index):
                continue
            try:
                await self._step(
                    attempt,
                    self._fetch(
                        base,
                        Digest.parse(digest),
                        token,
                        bundle,
                        max_bytes=part_cap,
                        **self._transport(),
                    ),
                )
                await asyncio.to_thread(folder.drop_part, index)
                try:
                    # not interruptible (bounded by its caps); a stop asked
                    # meanwhile is honoured right after
                    await asyncio.to_thread(
                        unpack, bundle, folder.part(index), limits=limits
                    )
                except BundleError as exc:
                    raise CombineRefusedError(
                        f"unsafe_part: chunk {index}'s result cannot be unpacked "
                        f"safely ({exc.message})"
                    ) from None
                await asyncio.to_thread(folder.seal_part, index)
            finally:
                with contextlib.suppress(OSError):
                    bundle.unlink()
            if attempt.stop_reason is not None:
                raise self._stopped_error()

    async def _parts_index(
        self, attempt: _Attempt, base: str, token: str
    ) -> PartsIndex:
        combine = attempt.combine
        assert combine is not None  # noqa: S101
        try:
            digest = Digest.parse(combine.spec.parts_digest)
        except ValueError:
            raise FetchError(
                "CombineSpec.parts_digest is not sha256:<64 hex>",
                failure_type=FailureType.INVALID_INPUT,
            ) from None
        dest = self.work_root / f"{attempt.attempt_id}.parts-index"
        try:
            await self._step(
                attempt,
                self._fetch(
                    base,
                    digest,
                    token,
                    dest,
                    max_bytes=MAX_PARTS_INDEX_BYTES,
                    **self._transport(),
                ),
            )
            data = await asyncio.to_thread(dest.read_bytes)
        finally:
            with contextlib.suppress(OSError):
                dest.unlink()
        try:
            index = await asyncio.to_thread(parse_parts_index, data)
        except ItemListError as exc:
            raise FetchError(
                f"the job's parts index is malformed: {exc}",
                failure_type=FailureType.INTEGRITY_ERROR,
            ) from None
        if len(index.chunks) != int(combine.spec.chunk_count):
            raise FetchError(
                "the job's parts index does not have CombineSpec.chunk_count chunks",
                failure_type=FailureType.INTEGRITY_ERROR,
            )
        return index

    # ------------------------------------------------------------ run

    def _builtin_spec(self, attempt: _Attempt, plan: AttemptPlan) -> RunSpec:
        combine = attempt.combine
        assert combine is not None  # noqa: S101
        folder = combine.folder
        assert folder is not None  # noqa: S101
        env = workload_env(
            runtime="subprocess",
            workdir=folder.root,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
        )
        env["PYTHONPATH"] = _meshforge_root()
        limits = plan.spec.limits
        return RunSpec(
            attempt_id=attempt.attempt_id,
            argv=(
                sys.executable,
                "-m",
                _BUILTIN_MODULE,
                combine.builtin,
                str(folder.parts),
                str(folder.out),
                str(folder.result),
                "--max-output-bytes",
                str(int(self._cfg.max_written_bytes)),
                "--json-max-bytes",
                str(int(self._settings.agent.split.combine_json_max_bytes)),
            ),
            workdir=folder.root,
            env=env,
            limits=RunLimits(
                memory_bytes=_BUILTIN_MEMORY_BYTES,
                pids=limits.pids,
                timeout_s=limits.timeout_s,
            ),
            grace_s=plan.spec.grace_s,
        )

    def _script_spec(self, attempt: _Attempt, plan: AttemptPlan) -> RunSpec:
        combine = attempt.combine
        assert combine is not None  # noqa: S101
        assert combine.script is not None  # noqa: S101
        folder = combine.folder
        assert folder is not None  # noqa: S101
        argv = combine_argv(
            runtime_argv(combine.script, plan.runtime),
            parts=folder.seen_parts,
            out=folder.seen_out,
        )
        return dataclasses.replace(
            plan.spec,
            argv=argv,
            env=combine_env(
                plan.spec.env, parts=folder.seen_parts, out=folder.seen_out
            ),
            extra_mounts=folder.mounts,
        )

    async def _run_combine(
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
        """The body of a combine's run (``Executor._execute``'s ``body``)."""
        combine = attempt.combine
        assert combine is not None  # noqa: S101
        script = combine.script is not None
        if script:
            spec = self._script_spec(attempt, plan)
            cap_watch = await self._output_cap(attempt)
        else:
            spec, cap_watch = self._builtin_spec(attempt, plan), None
            runtime = self._combine_runtime
        run: asyncio.Task[RunResult] = asyncio.ensure_future(
            runtime.run(spec, on_output, observer=observer)
        )
        attempt.step = run
        cap = self._start_output_cap(attempt, cap_watch)
        try:
            started = asyncio.ensure_future(observer.started.wait())
            try:
                await asyncio.wait({run, started}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                started.cancel()
            if observer.started.is_set():
                await self._announce_run(attempt)
            try:
                result = await run
            except asyncio.CancelledError:
                if not (run.cancelled() and attempt.stop_reason is not None):
                    raise
                result = observer.cancelled_result()
            finally:
                attempt.step = None
        finally:
            await stop_watch(cap)
            if drain is not None:
                await drain()
        if stderr_tail is not None and script:
            result = refine_result(result, stderr_tail.data)  # D116
        attempt.env_hash = result.env_hash or attempt.env_hash
        exited = self._clock()
        for chunk in logs.batcher.flush():
            self._send_log(attempt, chunk)
        if observer.run_started is not None:
            await self._announce_run(attempt)
        if observer.env_started is not None:
            attempt.phases["env_s"] = observer.env_s
        if observer.run_started is not None:
            attempt.phases["run_s"] = observer.run_s
        if result.cancelled:
            await self._stopped(
                attempt, exit_code=result.exit_code, signal=result.signal
            )
            return
        failure = None if result.ok else await self._combine_failure(attempt, result)
        if failure is None:
            await self._send(attempt, AttemptState.UPLOADING)
        final_logs = await self._final_logs(attempt, plan, logs)
        if failure is None:
            try:
                digest = await self._combine_output(attempt, plan)
            except (OutputError, FetchError) as exc:
                attempt.phases["collect_s"] = self._clock() - exited
                await self._send(
                    attempt,
                    AttemptState.FAILED,
                    failure=_output_failure(exc),
                    exit_code=result.exit_code,
                    logs=final_logs,
                )
                return
            attempt.phases["collect_s"] = self._clock() - exited
            await self._send(
                attempt,
                AttemptState.DONE,
                exit_code=result.exit_code,
                output_digest=str(digest),
                logs=final_logs,
            )
            return
        attempt.phases["collect_s"] = self._clock() - exited
        await self._send(
            attempt,
            AttemptState.FAILED,
            failure=failure,
            exit_code=result.exit_code,
            signal=result.signal,
            logs=final_logs,
        )

    async def _combine_failure(
        self, attempt: _Attempt, result: RunResult
    ) -> tuple[FailureType, str, bool]:
        """How a combine run that did not succeed ends (module docstring)."""
        combine = attempt.combine
        assert combine is not None  # noqa: S101
        detail = f"{result.reason}: {result.detail}" if result.reason else result.detail
        if combine.script is not None:
            if result.failure_type is FailureType.USER_ERROR:
                return (
                    FailureType.COMBINE_ERROR,
                    f"script_failed: the combine script exited {result.exit_code}"
                    + (f" ({detail})" if detail else ""),
                    False,
                )
            kind = result.failure_type or FailureType.INTERNAL_ERROR
            return kind, detail or kind.value, kind in _RETRYABLE
        if result.exit_code == EXIT_COMBINE_ERROR and not result.timed_out:
            refused = await asyncio.to_thread(_read_result, combine)
            if refused is not None:
                return FailureType.COMBINE_ERROR, refused, False
        if result.failure_type in (
            FailureType.DEADLINE_EXCEEDED,
            FailureType.OOM,
            FailureType.DISK_FULL,
        ):
            assert result.failure_type is not None  # noqa: S101
            return result.failure_type, detail or "the built-in combine stopped", False
        # our own code crashed (or could not start) on this node: another
        # node may do better (bounded by retry.max_attempts)
        log.error(
            "agent.combine.builtin_failed",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            exit_code=result.exit_code,
            signal=result.signal,
        )
        return (
            FailureType.INTERNAL_ERROR,
            f"the built-in combine failed on this node (exit {result.exit_code})",
            True,
        )

    async def _combine_output(self, attempt: _Attempt, plan: AttemptPlan) -> Digest:
        """``out/`` packed (checked unswapped) and uploaded under the grant."""
        combine = attempt.combine
        assert combine is not None  # noqa: S101
        folder = combine.folder
        assert folder is not None  # noqa: S101
        if not plan.upload_token:
            raise OutputError(
                "the controller gave this attempt no upload grant", retryable=False
            )
        cap = plan.upload_max_bytes or 2**62
        bundle = self.work_root / f"{attempt.attempt_id}.combine.bundle"
        packing = asyncio.ensure_future(asyncio.to_thread(folder.pack_out, bundle, cap))
        try:
            digest, size = await self._step(attempt, asyncio.shield(packing))
            started = self._clock()
            await self._upload_retrying(attempt, plan, digest, bundle)
            log.info(
                "agent.combine.output_uploaded",
                node_id=self._node_id,
                job_id=attempt.job_id,
                attempt_id=attempt.attempt_id,
                bytes=size,
                upload_s=round(self._clock() - started, 3),
            )
            return digest
        finally:
            if packing.done():
                with contextlib.suppress(OSError):
                    bundle.unlink()
            else:
                packing.add_done_callback(lambda _f: _unlink(bundle))

    async def _combine_cleanup(self, attempt: _Attempt) -> None:
        """The control folder goes with the attempt (every path)."""
        combine = attempt.combine
        if combine is None or combine.folder is None:
            return
        await asyncio.to_thread(combine.folder.remove)


_RETRYABLE: Final = frozenset(
    {
        FailureType.NODE_LOST,
        FailureType.TRANSFER_ERROR,
        FailureType.INTEGRITY_ERROR,
        FailureType.TIMEOUT,
        FailureType.NETWORK_ERROR,
        FailureType.RESOURCE_BUSY,
    }
)


def _output_failure(exc: OutputError | FetchError) -> tuple[FailureType, str, bool]:
    """Packing / upload failures; a packed output over the job's limit is the
    job's data (``combine_error`` ``too_large``), never retried."""
    if exc.message.startswith("output_limit"):
        return (
            FailureType.COMBINE_ERROR,
            "too_large: " + exc.message.removeprefix("output_limit: "),
            False,
        )
    return exc.failure_type, exc.message, bool(getattr(exc, "retryable", False))


def _read_result(combine: CombineState) -> str | None:
    """The built-in's refusal (``<reason>: <message>``) from its result file;
    None when there is none (then it counts as a crash). **Built-ins only**:
    a combine script's result is its exit code (``script_failed``) - a file
    a script left there is never read (it must not be able to word its own
    ``combine_error``). The file is opened without following a link."""
    folder = combine.folder
    if folder is None or combine.script is not None:
        return None
    try:
        fd = os.open(folder.result, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            data = fh.read(_RESULT_MAX_BYTES + 1)
        doc = json.loads(data[:_RESULT_MAX_BYTES])
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("ok") is not False:
        return None
    reason, message = doc.get("code"), doc.get("message")
    if not isinstance(reason, str) or not isinstance(message, str):
        return None
    text = f"{reason}: {message}"
    return "".join(c if c.isprintable() else "?" for c in text)[:1000]


def _empty_workdir(workdir: Path) -> None:
    """A built-in's workdir: an empty owner-only folder (its logs live there)."""
    workdir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        workdir.chmod(0o700)


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
