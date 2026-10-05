"""V0.6 G1 / D114, agent side: upload retries and ``RefreshUploadGrant``.

The executor's uploads (final logs, then the outputs) retry a refused grant by
waiting for ``RefreshUploadGrant`` and an unreachable controller with backoff,
for at most ``upload_retry_s`` counted from the attempt's first failed upload;
then ``output_error`` (retryable). A refresh applies only to the same
``(attempt_id, fencing_token)`` while the attempt has not ended, with a valid
grant and blob location. Unit level: the upload function is injected.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_executor import (
    Reports,
    WritingRuntime,
    _executor,
    _settle,
    _start,
    _with_outputs,
)

from meshforge.agent.execution.fetch import UploadError
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _down() -> UploadError:
    return UploadError("the upload failed: ConnectError", retryable=True)


def _refused() -> UploadError:
    return UploadError(
        "the controller refused the upload grant (HTTP 401)",
        retryable=True,
        details={"refused": True},
    )


class FlakyUploads:
    """Fails the OUTPUT upload with the queued errors, then succeeds; logs
    always succeed. Records (base, token) of every output try."""

    def __init__(self, errors: list[UploadError], on_log: Any = None) -> None:
        self.errors = errors
        self.tries: list[tuple[str, str]] = []
        self.logs = 0
        self.on_log = on_log

    async def __call__(self, base: str, _digest: Any, token: str, path: Path) -> int:
        if path.suffix == ".log":
            self.logs += 1
            if self.on_log is not None:
                self.on_log()
            return 0
        self.tries.append((base, token))
        if self.errors:
            raise self.errors.pop(0)
        return 1


def _refresh(
    msg: agent_pb2.ControllerMessage,
    token: str,
    *,
    fencing: int | None = None,
    port: int = 47999,
    url: str = "",
    attempt_id: str | None = None,
) -> agent_pb2.ControllerMessage:
    start = msg.start_attempt
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        refresh_upload_grant=agent_pb2.RefreshUploadGrant(
            attempt_id=start.attempt_id if attempt_id is None else attempt_id,
            fencing_token=start.fencing_token if fencing is None else fencing,
            upload_token=token,
            blob_url=url,
            blob_port=port,
        ),
    )


async def _until(predicate: Any, timeout_s: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout_s
    while not predicate():
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.01)


def _final(reports: Reports) -> agent_pb2.AttemptStatus:
    return reports.sent[-1][1]


async def test_a_refused_grant_waits_for_the_refresh_and_uses_it(
    tmp_path: Path,
) -> None:
    uploads, reports = FlakyUploads([_refused()]), Reports()
    executor, digest = _executor(
        tmp_path,
        WritingRuntime(),
        reports,
        upload=uploads,
        execution={"upload_retry_s": 30},
    )
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _until(lambda: uploads.tries)
    await asyncio.sleep(0.3)
    assert uploads.tries == [("http://127.0.0.1:47110", "grant")]  # no retry yet
    other = _start(digest)
    executor.handle(_refresh(msg, "grant-x", fencing=99))  # wrong fence
    executor.handle(_refresh(msg, "bad token"))  # malformed
    executor.handle(_refresh(msg, "grant-y", port=0))  # no blob port
    executor.handle(_refresh(msg, "grant-z", url="ftp://x"))  # bad blob URL
    executor.handle(_refresh(other, "grant-w"))  # an attempt it does not hold
    await asyncio.sleep(0.1)
    assert len(uploads.tries) == 1
    executor.handle(_refresh(msg, "grant2"))  # the restarted controller's port
    await _settle(executor)
    assert uploads.tries[-1] == ("http://127.0.0.1:47999", "grant2")
    assert len(uploads.tries) == 2
    assert attempt_state_from_proto(_final(reports).state).value == "done"
    await executor.close()


async def test_an_unreachable_controller_is_retried_with_backoff(
    tmp_path: Path,
) -> None:
    uploads, reports = FlakyUploads([_down(), _down()]), Reports()
    executor, digest = _executor(
        tmp_path,
        WritingRuntime(),
        reports,
        upload=uploads,
        execution={"upload_retry_s": 30},
    )
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _settle(executor)
    assert [t for _, t in uploads.tries] == ["grant"] * 3  # same grant, no wait
    assert attempt_state_from_proto(_final(reports).state).value == "done"
    await executor.close()


async def test_past_upload_retry_s_is_a_retryable_output_error(
    tmp_path: Path,
) -> None:
    uploads, reports = FlakyUploads([_refused()] * 50), Reports()
    executor, digest = _executor(
        tmp_path,
        WritingRuntime(),
        reports,
        upload=uploads,
        execution={"upload_retry_s": 0.3},
    )
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _settle(executor)
    final = _final(reports)
    assert attempt_state_from_proto(final.state).value == "failed"
    assert failure_type_from_proto(final.failure.failure_type).value == "output_error"
    assert final.failure.retryable
    assert "did not succeed within 0.3 s" in final.failure.message
    assert len(uploads.tries) == 1  # a refusal waits; it is not hammered
    await executor.close()


async def test_the_budget_starts_at_the_first_failure_not_at_the_first_upload(
    tmp_path: Path,
) -> None:
    """A slow but healthy log upload must not use up the retry budget."""
    now = [0.0]

    def slow_log() -> None:
        now[0] += 100.0  # far past upload_retry_s (5 s)

    uploads, reports = FlakyUploads([_down()], on_log=slow_log), Reports()
    executor, digest = _executor(
        tmp_path,
        WritingRuntime(),
        reports,
        upload=uploads,
        execution={"upload_retry_s": 5},
        clock=lambda: now[0],
    )
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _settle(executor)
    assert uploads.logs == 1 and len(uploads.tries) == 2
    assert attempt_state_from_proto(_final(reports).state).value == "done"
    await executor.close()


async def test_a_refresh_after_the_budget_does_not_buy_a_retry(
    tmp_path: Path,
) -> None:
    now = [0.0]
    uploads, reports = FlakyUploads([_refused()]), Reports()
    executor, digest = _executor(
        tmp_path,
        WritingRuntime(),
        reports,
        upload=uploads,
        execution={"upload_retry_s": 5},
        clock=lambda: now[0],
    )
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _until(lambda: uploads.tries)
    await asyncio.sleep(0.05)  # now waiting for the refresh
    now[0] = 10.0
    executor.handle(_refresh(msg, "grant2"))
    await _settle(executor)
    final = _final(reports)
    assert failure_type_from_proto(final.failure.failure_type).value == "output_error"
    assert len(uploads.tries) == 1
    await executor.close()


@pytest.mark.parametrize("retry_s", [0, 30])
async def test_a_final_error_or_no_budget_fails_at_once(
    tmp_path: Path, retry_s: float
) -> None:
    final_error = UploadError("the attempt already ended (HTTP 403)", retryable=False)
    errors = [final_error] if retry_s else [_refused()]
    uploads, reports = FlakyUploads(errors), Reports()
    executor, digest = _executor(
        tmp_path,
        WritingRuntime(),
        reports,
        upload=uploads,
        execution={"upload_retry_s": retry_s},
    )
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _settle(executor)
    final = _final(reports)
    assert failure_type_from_proto(final.failure.failure_type).value == "output_error"
    assert final.failure.retryable is (not retry_s)  # 403 final; 401 w/o budget not
    assert len(uploads.tries) == 1
    await executor.close()


async def test_a_refresh_before_the_upload_is_used_by_the_first_try(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()

    class SlowRuntime(WritingRuntime):
        async def run(self, spec: Any, on_output: Any, *, observer: Any = None) -> Any:
            await gate.wait()
            return await super().run(spec, on_output, observer=observer)

    uploads, reports = FlakyUploads([]), Reports()
    executor, digest = _executor(tmp_path, SlowRuntime(), reports, upload=uploads)
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await asyncio.sleep(0.1)  # running (well, staged and waiting)
    executor.handle(_refresh(msg, "grant2", url="http://10.1.2.3:9000"))
    gate.set()
    await _settle(executor)
    assert uploads.tries == [("http://10.1.2.3:9000", "grant2")]
    await executor.close()


async def test_a_refresh_for_an_ended_attempt_is_ignored(tmp_path: Path) -> None:
    uploads, reports = FlakyUploads([]), Reports()
    executor, digest = _executor(tmp_path, WritingRuntime(), reports, upload=uploads)
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _settle(executor)
    attempt = executor._attempts[msg.start_attempt.attempt_id]
    assert attempt.ended
    executor.handle(_refresh(msg, "grant2"))
    assert attempt.upload_token == "grant"
    await executor.close()


async def test_a_cancel_while_waiting_for_a_grant_is_prompt(tmp_path: Path) -> None:
    uploads, reports = FlakyUploads([_refused()]), Reports()
    executor, digest = _executor(
        tmp_path,
        WritingRuntime(),
        reports,
        upload=uploads,
        execution={"upload_retry_s": 600},
    )
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await _until(lambda: uploads.tries)
    start = msg.start_attempt
    executor.handle(
        agent_pb2.ControllerMessage(
            message_id=str(new_command_id()),
            cancel_attempt=agent_pb2.CancelAttempt(
                attempt_id=start.attempt_id, fencing_token=start.fencing_token
            ),
        )
    )
    await asyncio.wait_for(_settle(executor), 5)
    final = _final(reports)
    assert failure_type_from_proto(final.failure.failure_type).value == "cancelled"
    await executor.close()


async def test_a_resent_start_of_a_held_attempt_replaces_its_credentials(
    tmp_path: Path,
) -> None:
    """The controller re-minted both credentials for a re-sent StartAttempt and
    revoked the old ones: the agent runs it once but adopts the new ones; a
    copy with another fencing token, or after the attempt ended, changes
    nothing."""
    gate = asyncio.Event()

    class SlowRuntime(WritingRuntime):
        async def run(self, spec: Any, on_output: Any, *, observer: Any = None) -> Any:
            await gate.wait()
            return await super().run(spec, on_output, observer=observer)

    runtime, uploads, reports = SlowRuntime(), FlakyUploads([]), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, upload=uploads)
    msg = _start(digest)
    _with_outputs(msg)
    executor.handle(msg)
    await asyncio.sleep(0.1)
    attempt = executor._attempts[msg.start_attempt.attempt_id]

    def copy(grant: str, fetch: str, fencing: int = 1) -> agent_pb2.ControllerMessage:
        again = agent_pb2.ControllerMessage()
        again.CopyFrom(msg)
        again.message_id = str(new_command_id())
        again.start_attempt.fencing_token = fencing
        again.start_attempt.spec.upload_token = grant
        again.start_attempt.spec.fetch_token = fetch
        return again

    executor.handle(copy("grant-zombie", "tok-zombie", fencing=2))
    assert attempt.upload_token in ("", "grant") and attempt.fetch_token == "tok"
    executor.handle(copy("grant2", "tok2"))
    assert attempt.upload_token == "grant2" and attempt.fetch_token == "tok2"
    gate.set()
    await _settle(executor)
    assert runtime.calls == 1
    assert uploads.tries == [("http://127.0.0.1:47110", "grant2")]
    executor.handle(copy("grant3", "tok3"))  # ended: nothing changes
    assert attempt.upload_token == "grant2"
    await executor.close()
