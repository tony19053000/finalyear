"""V0.4 G5 D72: the WebSocket follower's framing and end frame, without a network."""

from __future__ import annotations

import base64
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from mf_users import ADMIN

from meshforge.common.errors import FailureType
from meshforge.common.states import AttemptState, JobState
from meshforge.controller.api.log_routes import _Follower
from meshforge.controller.jobs.logs import LogEvent, LogHub
from meshforge.controller.jobs.store import AttemptRecord, JobRecord

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class FakeWs:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(json.loads(text))


def _follower(
    tmp_path: Path, streams: tuple[str, ...] = ("stdout", "stderr")
) -> tuple[_Follower, FakeWs]:
    ws = FakeWs()
    hub = LogHub(tmp_path, blobs=None, max_bytes=10**6)
    f = _Follower(ws, None, hub, streams, ADMIN)  # type: ignore[arg-type]
    f.pos = {"stdout": 0, "stderr": 0}
    return f, ws


async def test_live_events_are_sent_once_from_the_client_position(
    tmp_path: Path,
) -> None:
    f, ws = _follower(tmp_path)
    f.pos["stdout"] = 3  # the client already has 3 bytes
    await f.live(LogEvent("data", "stdout", 0, b"abcdef"))  # overlaps: tail only
    await f.live(LogEvent("data", "stdout", 0, b"abcdef"))  # duplicate: nothing
    await f.live(LogEvent("data", "stdout", 6, b"gh"))
    got = [(fr["offset"], base64.b64decode(fr["data"])) for fr in ws.frames]
    assert got == [(3, b"def"), (6, b"gh")]


async def test_a_stream_not_followed_is_not_sent(tmp_path: Path) -> None:
    f, ws = _follower(tmp_path, ("stderr",))
    await f.live(LogEvent("data", "stdout", 0, b"x"))
    assert ws.frames == []


async def test_catch_up_sends_persisted_bytes_from_the_offset(tmp_path: Path) -> None:
    f, ws = _follower(tmp_path, ("stdout",))
    f.hub.path("att_1", "stdout").write_bytes(b"0123456789")
    f.pos["stdout"] = 4
    await f.catch_up("att_1", {"stdout": 8, "stderr": 0})
    assert [base64.b64decode(fr["data"]) for fr in ws.frames] == [b"4567"]
    await f.catch_up("att_1", None)  # to the end of the file
    assert base64.b64decode(ws.frames[-1]["data"]) == b"89"


async def test_the_end_frame(tmp_path: Path) -> None:
    f, ws = _follower(tmp_path)
    now = dt.datetime.now(dt.UTC)
    job = JobRecord(
        id="job_1",
        name="n",
        state=JobState.FAILED,
        manifest={},
        bundle_digest="",
        placement={},
        idempotency_key=None,
        request_hash="",
        attempt_epoch=1,
        failure_type=FailureType.USER_ERROR,
        failure_detail=None,
        created_at=now,
        updated_at=now,
    )
    attempt = AttemptRecord(
        id="att_1",
        job_id="job_1",
        node_id="n",
        fencing_token=1,
        state=AttemptState.FAILED,
        failure_type=FailureType.USER_ERROR,
        detail=None,
        exit_code=3,
        output_digest=None,
        start_snapshot=None,
        started_at=None,
        ended_at=None,
        created_at=now,
    )
    await f.end(job, attempt)
    (frame,) = ws.frames
    assert frame == {
        "end": True,
        "state": "failed",
        "attempt_state": "failed",
        "exit_code": 3,
        "failure_type": "user_error",
        "gaps": {},
        "final": {},
    }
