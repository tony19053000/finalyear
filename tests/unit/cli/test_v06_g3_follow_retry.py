"""V0.6 G3: ``compute run -f`` / ``compute logs -f`` across a retry - the
retry notice frame goes to ``on_retry`` (printed on stderr, sanitised) and the
follow continues to the job's end frame."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from meshforge.cli import follow as follow_module
from meshforge.cli import job_commands


class FakeWs:
    """The asyncio client's connection (V0.7 G4 item 0: the CLI no longer
    uses ``websockets.sync`` over TLS, D156 e)."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = frames

    async def __aenter__(self) -> FakeWs:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def __aiter__(self) -> AsyncIterator[str]:
        for frame in self.frames:
            yield json.dumps(frame)


def _data(stream: str, offset: int, text: bytes) -> dict[str, Any]:
    return {"stream": stream, "offset": offset, "data": base64.b64encode(text).decode()}


RETRY = {
    "retry": True,
    "attempt_id": "att_2",
    "fencing_token": 2,
    "previous_attempt_id": "att_1",
    "previous_state": "lost",
    "previous_failure_type": "node_lost",
}


def test_the_follow_continues_past_a_retry_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = [
        _data("stdout", 0, b"first\n"),
        RETRY,
        _data("stdout", 0, b"second\n"),
        {"end": True, "state": "succeeded", "exit_code": 0},
    ]
    monkeypatch.setattr(
        "websockets.asyncio.client.connect", lambda *a, **k: FakeWs(frames)
    )
    out = io.BytesIO()
    seen: list[Any] = []
    end = follow_module.follow_logs(
        "ws://h/v1/jobs/j/logs/ws",
        auth_header=None,
        outputs={"stdout": out},
        open_timeout_s=1.0,
        on_retry=seen.append,
    )
    assert end["state"] == "succeeded"
    assert out.getvalue() == b"first\nsecond\n"
    assert seen == [RETRY]


def test_the_retry_notice_is_printed_sanitised(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_commands._print_retry(RETRY)
    assert "attempt ended (node_lost)" in capsys.readouterr().err
    job_commands._print_retry(
        {**RETRY, "previous_failure_type": "\x1b[2Jevil", "fencing_token": True}
    )
    err = capsys.readouterr().err
    assert "\x1b" not in err and "(?)" in err and "following attempt ?" in err
