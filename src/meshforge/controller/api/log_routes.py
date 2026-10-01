"""``/v1/jobs/{id}/logs`` and ``/v1/jobs/{id}/logs/ws`` (V0.4 G5, D72).

Both need the fabric token (logs contain user output, D44) - the WebSocket in
the handshake's ``Authorization`` header, **never** a query parameter (a URL
ends up in logs and shell history).

**Plain** ``GET``: the persisted bytes of the job's latest attempt (or
``?attempt=``), ``?stream=stdout|stderr|both``. ``both`` is stdout followed by
stderr, and the ``X-MeshForge-Stdout-Bytes`` header says where stderr starts -
no separator bytes are invented. ``X-MeshForge-Attempt`` names the attempt,
``X-MeshForge-Log-Gaps`` (JSON, bounded) the live gaps when no final log
replaced them. A job without an attempt yet answers 200 with an empty body.

**WebSocket** ``/logs/ws?stream=...&stdout_offset=&stderr_offset=``: after the
handshake the server sends what is persisted from the offsets, then live data,
then - once the attempt is terminal **and** its final logs are in - one end frame
and a normal close. Frames are JSON text:

* ``{"stream": "stdout", "offset": N, "data": "<base64>"}`` (``offset`` = the
  byte position in the persisted file, so a client can resume with it);
* ``{"end": true, "state", "attempt_state", "exit_code", "failure_type",
  "gaps", "final"}``.

A client that reads too slowly is closed ``4008 slow_consumer`` - the log writer
never waits for it; a job over its follower limit gets ``4029``; an unknown job
``4404``; a bad id ``4400``. Waiting for the job's first attempt (while it is
queued) polls the store every ``_POLL_S``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path as FsPath
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Path, Query, WebSocket
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_attempt_id, validate_job_id
from meshforge.common.logging import get_logger
from meshforge.common.states import ATTEMPT_TERMINAL_STATES, JOB_TERMINAL_STATES
from meshforge.controller.api.auth import FabricTokenRequired, fabric_token_ok
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.job_routes import JOBS_STATE_KEY, JobApi, get_job_api
from meshforge.controller.jobs.logs import STREAMS, LogEvent, LogHub, Subscription
from meshforge.controller.jobs.store import AttemptRecord, JobRecord

__all__ = ["CLOSE_SLOW_CONSUMER", "log_router"]

log = get_logger(__name__)

log_router = APIRouter(prefix="/v1/jobs", tags=["logs"])

_JOB_ID_MAX: Final = 40
_FRAME_BYTES: Final = 64 * 1024
_POLL_S: Final = 0.25
_SEND_TIMEOUT_S: Final = 10.0
_CLOSE_WAIT_S: Final = 30.0
"""A close frame to a client that stopped reading is written once it reads
again; past this the connection is simply dropped."""
_MAX_GAPS_HEADER: Final = 64
CLOSE_SLOW_CONSUMER: Final = 4008
CLOSE_TOO_MANY: Final = 4029
CLOSE_NOT_FOUND: Final = 4404
CLOSE_INVALID: Final = 4400

StreamChoice = Literal["stdout", "stderr", "both"]
Jobs = Annotated[JobApi, Depends(get_job_api)]


def _streams(choice: str) -> tuple[str, ...]:
    return STREAMS if choice == "both" else (choice,)


async def _latest(jobs: JobApi, job_id: str) -> AttemptRecord | None:
    attempts = await jobs.store.list_attempts(job_id)
    return max(attempts, key=lambda a: a.fencing_token) if attempts else None


def _hub(jobs: JobApi) -> LogHub:
    if jobs.logs is None:
        raise ApiError(FailureType.CONFIG_ERROR, "this controller does not keep logs")
    return jobs.logs


# ------------------------------------------------------------------- plain


@log_router.get("/{job_id}/logs", summary="The job's persisted logs (plain bytes)")
async def get_logs(
    _auth: FabricTokenRequired,
    jobs: Jobs,
    job_id: Annotated[str, Path(max_length=_JOB_ID_MAX)],
    stream: Annotated[StreamChoice, Query()] = "both",
    attempt: Annotated[str | None, Query(max_length=_JOB_ID_MAX)] = None,
) -> StreamingResponse:
    hub = _hub(jobs)
    try:
        checked = str(validate_job_id(job_id))
        wanted = None if attempt is None else str(validate_attempt_id(attempt))
    except InvalidIdError:
        raise ApiError.invalid("job_id / attempt is not a canonical id") from None
    job = await jobs.store.get_job(checked)
    if job is None:
        raise ApiError.not_found("job")
    attempts = await jobs.store.list_attempts(checked)
    chosen: AttemptRecord | None
    if wanted is not None:
        chosen = next((a for a in attempts if a.id == wanted), None)
        if chosen is None:
            raise ApiError.not_found("attempt")
    else:
        chosen = max(attempts, key=lambda a: a.fencing_token) if attempts else None
    headers: dict[str, str] = {}
    files: list[tuple[FsPath, int]] = []
    if chosen is not None:
        headers["X-MeshForge-Attempt"] = chosen.id
        for name in _streams(stream):
            path = hub.path(chosen.id, name)
            files.append((path, await asyncio.to_thread(_size, path)))
        if stream == "both":
            headers["X-MeshForge-Stdout-Bytes"] = str(files[0][1])
        gaps = _gaps(hub, chosen.id)
        if gaps:
            headers["X-MeshForge-Log-Gaps"] = json.dumps(gaps, separators=(",", ":"))
    return StreamingResponse(
        _stream_files(files), media_type="text/plain", headers=headers
    )


def _size(path: FsPath) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _gaps(hub: LogHub, attempt_id: str) -> dict[str, list[list[int]]]:
    state = hub.live_state(attempt_id) or hub.ended_state(attempt_id)
    if state is None:
        return {}
    gaps = {s: g for s, g in state.gaps().items() if not state.final.get(s)}
    return {s: g[:_MAX_GAPS_HEADER] for s, g in gaps.items()}


async def _stream_files(files: list[tuple[FsPath, int]]) -> AsyncIterator[bytes]:
    for path, size in files:
        start = 0
        while start < size:
            data = await asyncio.to_thread(
                _read, path, start, min(size, start + _FRAME_BYTES)
            )
            if not data:
                break
            start += len(data)
            yield data


def _read(path: FsPath, start: int, end: int) -> bytes:
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            return fh.read(max(0, end - start))
    except OSError:
        return b""


# --------------------------------------------------------------- websocket


class _Follower:
    """One WebSocket follower (see module docstring)."""

    def __init__(
        self, ws: WebSocket, jobs: JobApi, hub: LogHub, streams: tuple[str, ...]
    ) -> None:
        self.ws = ws
        self.jobs = jobs
        self.hub = hub
        self.streams = streams
        self.pos: dict[str, int] = {}
        self.sub: Subscription | None = None

    async def send(self, payload: dict[str, Any]) -> None:
        await asyncio.wait_for(
            self.ws.send_text(json.dumps(payload, separators=(",", ":"))),
            _SEND_TIMEOUT_S,
        )

    async def catch_up(self, attempt_id: str, limit: dict[str, int] | None) -> None:
        """Send persisted bytes from ``pos`` to ``limit`` (None = file end)."""
        for stream in self.streams:
            path = self.hub.path(attempt_id, stream)
            end = (
                limit[stream]
                if limit is not None
                else await asyncio.to_thread(_size, path)
            )
            while self.pos[stream] < end:
                start = self.pos[stream]
                data = await asyncio.to_thread(
                    _read, path, start, min(end, start + _FRAME_BYTES)
                )
                if not data:
                    break
                await self._frame(stream, start, data)

    async def _frame(self, stream: str, offset: int, data: bytes) -> None:
        await self.send(
            {
                "stream": stream,
                "offset": offset,
                "data": base64.b64encode(data).decode("ascii"),
            }
        )
        self.pos[stream] = offset + len(data)

    async def live(self, event: LogEvent) -> None:
        if event.stream not in self.streams:
            return
        end = event.file_offset + len(event.data)
        if end <= self.pos[event.stream]:
            return
        skip = max(0, self.pos[event.stream] - event.file_offset)
        await self._frame(event.stream, event.file_offset + skip, event.data[skip:])

    async def end(self, job: JobRecord, attempt: AttemptRecord | None) -> None:
        state = None
        if attempt is not None:
            state = self.hub.ended_state(attempt.id) or self.hub.live_state(attempt.id)
        await self.send(
            {
                "end": True,
                "state": job.state.value,
                "attempt_state": None if attempt is None else attempt.state.value,
                "exit_code": None if attempt is None else attempt.exit_code,
                "failure_type": None
                if job.failure_type is None
                else job.failure_type.value,
                "gaps": {} if attempt is None else _gaps(self.hub, attempt.id),
                "final": {} if state is None else dict(state.final),
            }
        )


@log_router.websocket("/{job_id}/logs/ws")
async def follow_logs(
    websocket: WebSocket,
    job_id: str,
    stream: StreamChoice = "both",
    stdout_offset: Annotated[int, Query(ge=0, le=1 << 40)] = 0,
    stderr_offset: Annotated[int, Query(ge=0, le=1 << 40)] = 0,
) -> None:
    jobs = getattr(websocket.app.state, JOBS_STATE_KEY, None)
    if not fabric_token_ok(websocket) or not isinstance(jobs, JobApi):
        await websocket.close(code=1008)  # before accept: the client sees 403
        return
    await websocket.accept()
    follower: _Follower | None = None
    try:
        if jobs.logs is None:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="logs_not_kept")
            return
        try:
            checked = str(validate_job_id(job_id))
        except InvalidIdError:
            await websocket.close(code=CLOSE_INVALID, reason="invalid_job_id")
            return
        follower = _Follower(websocket, jobs, jobs.logs, _streams(stream))
        follower.pos = {"stdout": stdout_offset, "stderr": stderr_offset}
        await _follow(follower, checked)
    except WebSocketDisconnect:
        return
    except TimeoutError:
        # A send blocked for _SEND_TIMEOUT_S: the client is not reading.
        # Its queue has usually overflowed too while the send was blocked: one
        # slow follower is counted once (Subscription.mark_slow).
        log.info("controller.logs.slow_follower", job_id=job_id, cause="send_timeout")
        if follower is not None and follower.sub is not None:
            follower.sub.mark_slow()
        elif jobs.logs is not None:
            jobs.logs.stats.slow_subscribers += 1
        await _close_slow(websocket)
        return
    finally:
        if follower is not None and follower.sub is not None:
            follower.sub.close()
    if websocket.application_state is WebSocketState.CONNECTED:
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=1000)


async def _follow(f: _Follower, job_id: str) -> Subscription | None:
    """The follow loop; returns the subscription to close (or None)."""
    job = await f.jobs.store.get_job(job_id)
    if job is None:
        await f.ws.close(code=CLOSE_NOT_FOUND, reason="job_not_found")
        return None
    attempt = await _latest(f.jobs, job_id)
    while attempt is None:  # queued: wait for the first attempt (or the end)
        if job.state in JOB_TERMINAL_STATES:
            await f.end(job, None)
            return None
        await asyncio.sleep(_POLL_S)
        job = await f.jobs.store.get_job(job_id) or job
        attempt = await _latest(f.jobs, job_id)
    sub = f.hub.subscribe(attempt.id, job_id)
    if sub is None:
        await f.ws.close(code=CLOSE_TOO_MANY, reason="too_many_followers")
        return None
    f.sub = sub
    # Subscribed first, then the snapshot: nothing appended in between is lost
    # (the writer appends and fans out without an await in between).
    snapshot = {s: f.hub.file_len(attempt.id, s) for s in STREAMS}
    await f.catch_up(attempt.id, snapshot)
    done = f.hub.ended_state(attempt.id) is not None or await _ended_without_hub(
        f, attempt.id
    )
    while not done:
        if sub.overflowed:
            log.info(
                "controller.logs.slow_follower", job_id=job_id, attempt_id=attempt.id
            )
            await _close_slow(f.ws)
            return sub
        try:
            event = await asyncio.wait_for(sub.queue.get(), 1.0)
        except TimeoutError:
            if await _ended_without_hub(f, attempt.id):
                break
            continue
        if event.kind == "end":
            break
        await f.live(event)
    await f.catch_up(attempt.id, None)  # the final log may be longer than live
    job = await f.jobs.store.get_job(job_id) or job
    record = await f.jobs.store.get_attempt(attempt.id) or attempt
    await f.end(job, record)
    return sub


async def _close_slow(ws: WebSocket) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            ws.close(code=CLOSE_SLOW_CONSUMER, reason="slow_consumer"), _CLOSE_WAIT_S
        )


async def _ended_without_hub(f: _Follower, attempt_id: str) -> bool:
    """Terminal in the store while the hub no longer (or never) tracks it - e.g.
    after a controller restart. A hub-tracked attempt ends with its end event."""
    if f.hub.live_state(attempt_id) is not None:
        return False
    record = await f.jobs.store.get_attempt(attempt_id)
    return record is None or record.state in ATTEMPT_TERMINAL_STATES
