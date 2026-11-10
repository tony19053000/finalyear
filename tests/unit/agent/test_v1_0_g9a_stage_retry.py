"""V1.0 G9-A item 1 (D429): ``staging_retry_s`` = time staging may keep
failing **without progress**.

The retry window itself (:class:`StageRetryWindow`) with an injected clock,
then the executor's tree staging end to end against a fake blob surface
(``httpx.MockTransport``: real CAS, real resumable parts, real verification,
injected monotonic clock - no real waits beyond the 0.25 s first backoff).

Checked: an error 90 s into a long staging that had progress is retried (the
old rule failed it: the window had closed at 60 s) and the retry asks only
for the missing chunks; a try without progress for longer than the window
fails ``transfer_error`` naming the last cause; ``staging_retry_s = 0`` still
fails at the first error; a controller refusal after progress still waits
for ``RefreshFetchToken`` (never retried with the refused token);
``agent.execution.stage_retry`` says whether the try progressed.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from meshforge.agent.cas.progress import StagingProgress
from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.fetch import FetchError
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.runtime import (
    RunObserver,
    RunResult,
    RuntimeAvailability,
)
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.execution.stage_retry import (
    STAGE_RETRY_BASE_S,
    STAGE_RETRY_CAP_S,
    StageRetryWindow,
)
from meshforge.agent.settings import AgentSettings
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)
from meshforge.common.tree import BuiltTree, build_tree

pytestmark = pytest.mark.unit

CS = MIN_CHUNK_SIZE
CHUNKS = 8


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


# ------------------------------------------------------------ the window


def _down(text: str = "link went away") -> FetchError:
    return FetchError(text, failure_type=FailureType.TRANSFER_ERROR)


def test_a_try_with_progress_restarts_the_window_and_the_backoff() -> None:
    now = [0.0]
    progress = StagingProgress()
    window = StageRetryWindow(60.0, lambda: now[0], progress)
    assert window.deadline == 60.0
    assert window.backoff() == STAGE_RETRY_BASE_S
    assert window.backoff() == STAGE_RETRY_BASE_S * 2
    now[0] = 90.0  # a long try: past the first window, but it verified chunks
    progress.advance(5)
    assert window.failed() is True
    assert window.deadline == 150.0
    window.check(_down())  # not raised: the window restarted at the failure
    assert window.backoff() == STAGE_RETRY_BASE_S  # backoff reset too
    # a try without progress leaves the window where it is
    now[0] = 100.0
    assert window.failed() is False
    assert window.deadline == 150.0


def test_no_progress_past_the_window_is_transfer_error_naming_the_last_cause() -> None:
    now = [0.0]
    window = StageRetryWindow(60.0, lambda: now[0], StagingProgress())
    now[0] = 59.0
    assert window.failed() is False
    window.check(_down())
    now[0] = 60.0
    with pytest.raises(FetchError) as caught:
        window.check(_down("chunk fetch failed: the controller closed it"))
    assert caught.value.failure_type is FailureType.TRANSFER_ERROR
    assert "within 60 s without progress" in caught.value.message
    assert "the controller closed it" in caught.value.message


def test_the_backoff_doubles_to_its_cap_and_never_waits_past_the_window() -> None:
    now = [0.0]
    window = StageRetryWindow(600.0, lambda: now[0], StagingProgress())
    waits = [window.backoff() for _ in range(8)]
    assert waits[:5] == [0.25, 0.5, 1.0, 2.0, 4.0]
    assert waits[5:] == [STAGE_RETRY_CAP_S] * 3
    now[0] = 599.0
    assert window.backoff() == 1.0  # the rest of the window, not 5 s
    now[0] = 700.0
    assert window.backoff() == 0.0
    assert StageRetryWindow(0, lambda: 0.0, StagingProgress()).retries is False


# ------------------------------------------------------- executor + tree


class FakeRuntime:
    name = "subprocess"

    def __init__(self) -> None:
        self.calls = 0

    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("subprocess", True)

    async def run(
        self, spec: Any, on_output: Any, *, observer: RunObserver | None = None
    ) -> RunResult:
        assert observer is not None
        self.calls += 1
        observer.enter_run()
        observer.leave_run()
        return RunResult(exit_code=0)


class Reports:
    def __init__(self) -> None:
        self.sent: list[agent_pb2.AttemptStatus] = []

    async def __call__(self, _name: str, payload: agent_pb2.AttemptStatus) -> str:
        copy = agent_pb2.AttemptStatus()
        copy.CopyFrom(payload)
        self.sent.append(copy)
        return str(new_command_id())

    def final(self) -> agent_pb2.AttemptStatus:
        return self.sent[-1]


class BlobSurface:
    """The controller's blob API for one tree, with scripted failures.

    ``script`` maps the n-th chunk ``Range`` request (1-based) to an action:
    ``("cut", seconds)`` advances the injected clock by ``seconds`` and fails
    the request like a body cut off mid-stream; ``("refuse", seconds)``
    answers 401 (the controller refused the token)."""

    def __init__(
        self,
        built: BuiltTree,
        now: list[float],
        script: dict[int, tuple[str, float]],
        tokens: tuple[str, ...] = ("tok",),
    ) -> None:
        self.blobs = {h: p.read_bytes() for h, p in built.sources.items()}
        self.blobs[built.tree.tree_id.hex] = built.manifest
        self.now = now
        self.script = script
        self.tokens = set(tokens)
        self.range_requests = 0
        self.ranges: list[tuple[int, int]] = []
        """Every chunk range asked for (start, end incl.), in order."""
        self.cut_at: list[int] = []
        """Index into ``ranges`` of each scripted failure."""

    async def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth.removeprefix("Bearer ") not in self.tokens:
            return httpx.Response(401)
        hex_ = request.url.path.rsplit("/", 1)[-1]
        data = self.blobs.get(hex_)
        if data is None:
            return httpx.Response(404)
        rng = request.headers.get("range")
        if rng is None:
            return httpx.Response(200, stream=httpx.ByteStream(data))
        start, end = (int(x) for x in rng.removeprefix("bytes=").split("-"))
        self.range_requests += 1
        self.ranges.append((start, end))
        action = self.script.get(self.range_requests)
        if action is not None:
            kind, seconds = action
            self.now[0] += seconds
            self.cut_at.append(len(self.ranges) - 1)
            if kind == "refuse":
                return httpx.Response(401)
            raise httpx.RemoteProtocolError("peer closed connection mid-body")
        return httpx.Response(206, stream=httpx.ByteStream(data[start : end + 1]))


def _tree(tmp_path: Path) -> BuiltTree:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_text("print(1)\n")
    (root / "data.bin").write_bytes(os.urandom(CHUNKS * CS))
    return build_tree(root, chunk_size=CS)


def _executor(
    tmp_path: Path,
    surface: BlobSurface,
    clock: Callable[[], float],
    *,
    staging_retry_s: float,
) -> tuple[Executor, FakeRuntime, Reports]:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "execution": {
                "enabled": True,
                "allow_unsandboxed": True,
                "grace_s": 0.5,
                "staging_retry_s": staging_retry_s,
            },
            # one request at a time: "the n-th chunk request" is exact
            "fetch": {"per_source": 1},
        },
    )
    runtime, reports = FakeRuntime(), Reports()
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=None,
        subprocess=RuntimeAvailability("subprocess", True),
    )
    executor = Executor(
        settings,
        ExecutionSetup(offer=offer, runtimes={"subprocess": runtime}),  # type: ignore[dict-item]
        node_id="node_x",
        report=reports,
        controller_host=lambda: "127.0.0.1",
        clock=clock,
    )

    def transport() -> dict[str, Any]:
        return {"transport": httpx.MockTransport(surface.handler)}

    executor._transport = transport  # type: ignore[method-assign]
    return executor, runtime, reports


def _start(built: BuiltTree) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        start_attempt=agent_pb2.StartAttempt(
            job_id=str(new_job_id()),
            attempt_id=str(new_attempt_id()),
            fencing_token=1,
            spec=agent_pb2.ExecutionSpec(
                tree_id=str(built.tree.tree_id),
                blob_url="http://ctl:47110",
                blob_port=47110,
                fetch_token="tok",
                manifest_json='{"name":"x","entrypoint":["python","main.py"]}',
                timeout_s=60,
            ),
        ),
    )


async def _settle(executor: Executor, timeout_s: float = 10.0) -> None:
    async def done() -> None:
        while True:
            tasks = [a.task for a in executor._attempts.values() if a.task]
            if tasks and all(t.done() for t in tasks):
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(done(), timeout_s)


def _state(status: agent_pb2.AttemptStatus) -> str:
    return attempt_state_from_proto(status.state).value


def _failure(status: agent_pb2.AttemptStatus) -> str:
    return failure_type_from_proto(status.failure.failure_type).value


def _chunk_ranges() -> list[tuple[int, int]]:
    return [(i * CS, (i + 1) * CS - 1) for i in range(CHUNKS)]


@pytest.mark.asyncio
async def test_an_error_90_s_into_a_staging_with_progress_is_retried_and_resumes(
    tmp_path: Path,
) -> None:
    now = [0.0]
    built = _tree(tmp_path)
    # chunks 1-4 verified, the 5th request is cut off 90 s into the staging
    surface = BlobSurface(built, now, {5: ("cut", 90.0)})
    executor, runtime, reports = _executor(
        tmp_path, surface, lambda: now[0], staging_retry_s=60
    )
    with capture_logs() as logs:
        executor.handle(_start(built))
        await _settle(executor)
    assert _state(reports.final()) == "done", reports.final().failure.message
    assert runtime.calls == 1
    chunks = _chunk_ranges()
    assert surface.ranges[:4] == chunks[:4]
    # the retry asked only for the missing chunks: the cut one, then the rest
    assert surface.ranges[5:] == chunks[4:]
    retries = [e for e in logs if e["event"] == "agent.execution.stage_retry"]
    assert [(e["progressed"], e["cause"]) for e in retries] == [
        (True, "transfer_error")
    ]
    staged = [e for e in logs if e["event"] == "agent.cas.staged"]
    assert staged[-1]["chunks_resumed"] == 4
    # the attempt's transfer telemetry carries the resume
    assert reports.final().transfers[0].chunks_resumed == 4


@pytest.mark.asyncio
async def test_a_try_without_progress_past_the_window_fails_transfer_error(
    tmp_path: Path,
) -> None:
    now = [0.0]
    built = _tree(tmp_path)
    # try 1: 2 chunks then a cut at 90 s (progress: retried, window 90..150);
    # try 2: its first request is cut 70 s later (no progress, past 150 s)
    surface = BlobSurface(built, now, {3: ("cut", 90.0), 4: ("cut", 70.0)})
    executor, runtime, reports = _executor(
        tmp_path, surface, lambda: now[0], staging_retry_s=60
    )
    with capture_logs() as logs:
        executor.handle(_start(built))
        await _settle(executor)
    final = reports.final()
    assert _state(final) == "failed" and _failure(final) == "transfer_error"
    assert final.failure.retryable
    assert "within 60 s without progress" in final.failure.message
    assert "(last: chunk fetch failed: RemoteProtocolError)" in final.failure.message
    assert runtime.calls == 0
    assert surface.range_requests == 4  # no third try
    retries = [e for e in logs if e["event"] == "agent.execution.stage_retry"]
    assert [e["progressed"] for e in retries] == [True]


@pytest.mark.asyncio
async def test_no_retry_budget_still_fails_at_the_first_error(tmp_path: Path) -> None:
    now = [0.0]
    built = _tree(tmp_path)
    surface = BlobSurface(built, now, {3: ("cut", 1.0)})
    executor, runtime, reports = _executor(
        tmp_path, surface, lambda: now[0], staging_retry_s=0
    )
    executor.handle(_start(built))
    await _settle(executor)
    final = reports.final()
    assert _state(final) == "failed" and _failure(final) == "transfer_error"
    assert "without progress" not in final.failure.message  # reported as is
    assert "chunk fetch failed" in final.failure.message
    assert surface.range_requests == 3 and runtime.calls == 0


@pytest.mark.asyncio
async def test_a_controller_refusal_after_progress_still_waits_for_the_refresh(
    tmp_path: Path,
) -> None:
    now = [0.0]
    built = _tree(tmp_path)
    surface = BlobSurface(built, now, {3: ("refuse", 90.0)}, tokens=("tok", "tok2"))
    executor, _runtime, reports = _executor(
        tmp_path, surface, lambda: now[0], staging_retry_s=60
    )
    msg = _start(built)
    executor.handle(msg)
    for _ in range(500):
        if surface.cut_at:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)  # now waiting for RefreshFetchToken
    assert surface.range_requests == 3  # not retried with the refused token
    surface.tokens.discard("tok")  # the restarted controller forgot it
    start = msg.start_attempt
    executor.handle(
        agent_pb2.ControllerMessage(
            message_id=str(new_command_id()),
            refresh_fetch_token=agent_pb2.RefreshFetchToken(
                attempt_id=start.attempt_id,
                fencing_token=start.fencing_token,
                fetch_token="tok2",
                blob_url="http://ctl:47110",
                blob_port=47110,
            ),
        )
    )
    await _settle(executor)
    assert _state(reports.final()) == "done", reports.final().failure.message
    chunks = _chunk_ranges()
    assert surface.ranges[3:] == chunks[2:]  # resumed after 2 verified chunks
