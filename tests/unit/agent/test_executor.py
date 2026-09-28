"""V0.4 G4 executor rules without a network (D45/D52/D66/D67).

A real bundle is packed and "fetched" by copying it; the runtime is a fake that
records calls. Checked: state order, the disabled agent never calls a runtime,
dedupe on (attempt_id, fencing_token), cancel and agent shutdown typing, and
"held until the terminal report is acked", and the D75 re-send / release rules
(clock injected: no real waits).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.runtime import (
    RunObserver,
    RunResult,
    RuntimeAvailability,
)
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.settings import AgentSettings
from meshforge.common.bundle import pack_to_file
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


class FakeRuntime:
    name = "subprocess"

    def __init__(self, *, hang: bool = False, exit_code: int = 0) -> None:
        self.calls = 0
        self.hang = hang
        self.exit_code = exit_code

    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("subprocess", True)

    async def run(
        self, spec: Any, on_output: Any, *, observer: RunObserver | None = None
    ) -> RunResult:
        assert observer is not None
        self.calls += 1
        observer.enter_run()
        await on_output("stdout", b"hi\n")
        if self.hang:
            await asyncio.Event().wait()
        observer.leave_run()
        return RunResult(exit_code=self.exit_code)


class Reports:
    def __init__(self) -> None:
        self.sent: list[tuple[str, agent_pb2.AttemptStatus]] = []

    async def __call__(self, name: str, payload: agent_pb2.AttemptStatus) -> str:
        mid = str(new_command_id())
        copy = agent_pb2.AttemptStatus()
        copy.CopyFrom(payload)
        self.sent.append((mid, copy))
        return mid

    def states(self) -> list[str]:
        return [attempt_state_from_proto(s.state).value for _, s in self.sent]


def _setup(runtime: FakeRuntime, *, enabled: bool = True) -> ExecutionSetup:
    offer = execution_offer(
        enabled=enabled,
        allow_unsandboxed=True,
        docker=None,
        subprocess=RuntimeAvailability("subprocess", True),
    )
    return ExecutionSetup(
        offer=offer, runtimes={"subprocess": runtime} if enabled else {}
    )  # type: ignore[dict-item]


def _executor(
    tmp_path: Path,
    runtime: FakeRuntime,
    reports: Reports,
    *,
    enabled: bool = True,
    **extra: Any,
) -> tuple[Executor, str]:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text("print(1)\n")
    bundle = tmp_path / "src.bundle"
    digest = str(pack_to_file(project, bundle).digest)

    async def fake_fetch(_base: str, _digest: Any, _token: str, dest: Path) -> int:
        shutil.copyfile(bundle, dest)
        return dest.stat().st_size  # noqa: ASYNC240 - a test fake

    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "execution": {
                "enabled": enabled,
                "allow_unsandboxed": True,
                "grace_s": 0.5,
            },
        },
    )
    executor = Executor(
        settings,
        _setup(runtime, enabled=enabled),
        node_id="node_x",
        report=reports,
        controller_host=lambda: "127.0.0.1",
        fetch=fake_fetch,
        **extra,
    )
    return executor, digest


def _start(
    digest: str, attempt_id: str | None = None, token: int = 1
) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        start_attempt=agent_pb2.StartAttempt(
            job_id=str(new_job_id()),
            attempt_id=attempt_id or str(new_attempt_id()),
            fencing_token=token,
            spec=agent_pb2.ExecutionSpec(
                bundle_digest=digest,
                blob_port=47110,
                fetch_token="tok",
                manifest_json='{"name":"x","entrypoint":["python","main.py"]}',
                timeout_s=60,
            ),
        ),
    )


async def _settle(executor: Executor) -> None:
    for _ in range(200):
        tasks = [a.task for a in executor._attempts.values() if a.task]
        if all(t.done() for t in tasks):
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_state_order_and_release_after_ack(tmp_path: Path) -> None:
    runtime, reports = FakeRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    msg = _start(digest)
    executor.handle(msg)
    await _settle(executor)
    assert reports.states() == [
        "staging",
        "env_ready",
        "running",
        "uploading",
        "done",
    ]
    final = reports.sent[-1][1]
    assert final.phases.HasField("stage_s") and final.phases.HasField("run_s")
    assert not final.phases.HasField("env_s")  # never ran: absent, not 0
    assert final.runtime == "subprocess" and final.exit_code == 0
    held = executor.held()
    assert [h.attempt_id for h in held] == [msg.start_attempt.attempt_id]
    executor.report_acked(reports.sent[-1][0])
    assert executor.held() == []
    # a late duplicate of the finished attempt runs nothing and reports nothing
    before = len(reports.sent)
    executor.handle(msg)
    await _settle(executor)
    assert runtime.calls == 1
    assert len(reports.sent) == before and executor.held() == []


@pytest.mark.asyncio
async def test_a_duplicate_start_while_running_runs_once(tmp_path: Path) -> None:
    runtime, reports = FakeRuntime(hang=True), Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    msg = _start(digest)
    executor.handle(msg)
    resent = _start(digest, attempt_id=msg.start_attempt.attempt_id)
    for _ in range(100):
        if runtime.calls:
            break
        await asyncio.sleep(0.01)
    before = len(reports.sent)
    executor.handle(resent)  # same (attempt_id, token), new message_id
    await asyncio.sleep(0.02)
    assert runtime.calls == 1
    assert len(reports.sent) == before  # no second lifecycle at all
    await executor.close()


@pytest.mark.asyncio
async def test_disabled_agent_never_calls_a_runtime(tmp_path: Path) -> None:
    runtime, reports = FakeRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, enabled=False)
    executor.handle(_start(digest))
    await _settle(executor)
    assert runtime.calls == 0
    ((_, status),) = reports.sent
    assert attempt_state_from_proto(status.state).value == "failed"
    assert failure_type_from_proto(status.failure.failure_type).value == "unauthorized"


@pytest.mark.asyncio
async def test_cancel_and_shutdown_are_typed(tmp_path: Path) -> None:
    runtime, reports = FakeRuntime(hang=True), Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    first = _start(digest)
    executor.handle(first)
    for _ in range(100):
        if runtime.calls:
            break
        await asyncio.sleep(0.01)
    wrong = agent_pb2.ControllerMessage(
        cancel_attempt=agent_pb2.CancelAttempt(
            attempt_id=first.start_attempt.attempt_id, fencing_token=9
        )
    )
    executor.handle(wrong)  # stale token: ignored
    await asyncio.sleep(0.02)
    assert reports.states()[-1] == "running"
    executor.handle(
        agent_pb2.ControllerMessage(
            cancel_attempt=agent_pb2.CancelAttempt(
                attempt_id=first.start_attempt.attempt_id, fencing_token=1
            )
        )
    )
    await _settle(executor)
    last = reports.sent[-1][1]
    assert failure_type_from_proto(last.failure.failure_type).value == "cancelled"

    second = _start(digest)
    executor.handle(second)
    for _ in range(100):
        if runtime.calls == 2:
            break
        await asyncio.sleep(0.01)
    await executor.shutdown()
    last = reports.sent[-1][1]
    assert last.attempt_id == second.start_attempt.attempt_id
    assert failure_type_from_proto(last.failure.failure_type).value == "node_lost"
    assert last.failure.message == "agent_shutdown" and last.failure.retryable
    # after shutdown a new StartAttempt is refused, not run
    executor.handle(_start(digest))
    await _settle(executor)
    assert runtime.calls == 2
    assert reports.sent[-1][1].failure.failure_type == common_pb2.FAILURE_TYPE_NODE_LOST


@pytest.mark.asyncio
async def test_an_invalid_manifest_fails_typed_before_staging(tmp_path: Path) -> None:
    runtime, reports = FakeRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    msg = _start(digest)
    msg.start_attempt.spec.manifest_json = '{"name": "x", "split": {}}'
    executor.handle(msg)
    await _settle(executor)
    assert reports.states() == ["failed"]
    status = reports.sent[0][1]
    assert failure_type_from_proto(status.failure.failure_type).value == "invalid_input"
    assert runtime.calls == 0


# ------------------------------------------------------------------ D75


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Ring:
    """What ``AgentSessionClient.resend_report`` sees: ids still buffered."""

    def __init__(self) -> None:
        self.buffered: set[str] = set()
        self.rewritten: list[str] = []

    async def __call__(self, message_id: str) -> bool:
        if message_id not in self.buffered:
            return False
        self.rewritten.append(message_id)
        return True


async def _finished(
    tmp_path: Path, **extra: Any
) -> tuple[Executor, Reports, agent_pb2.ControllerMessage]:
    runtime, reports = FakeRuntime(), Reports()
    executor, digest = _executor(tmp_path, runtime, reports, **extra)
    msg = _start(digest)
    executor.handle(msg)
    await _settle(executor)
    assert reports.states()[-1] == "done"
    return executor, reports, msg


@pytest.mark.asyncio
async def test_an_unacked_final_report_is_resent_until_acked(tmp_path: Path) -> None:
    clock, ring = Clock(), Ring()
    executor, reports, msg = await _finished(tmp_path, clock=clock, resend=ring)
    first_id = reports.sent[-1][0]
    ring.buffered.add(first_id)
    period = executor._cfg.terminal_resend_s
    clock.now += period - 0.01
    assert await executor.resend_due() == 0  # not due yet
    clock.now += 0.02
    assert await executor.resend_due() == 1
    assert ring.rewritten == [first_id]  # still buffered: the same message again
    # the ring evicted it: a NEW report carrying the same final status
    ring.buffered.clear()
    clock.now += period
    before = len(reports.sent)
    assert await executor.resend_due() == 1
    assert len(reports.sent) == before + 1
    new_id, again = reports.sent[-1]
    assert again.attempt_id == msg.start_attempt.attempt_id
    assert attempt_state_from_proto(again.state).value == "done"
    assert again.phases.HasField("run_s") and again.exit_code == 0
    # an Ack of EITHER copy releases the attempt; then nothing is re-sent
    executor.report_acked(new_id)
    assert executor.held() == []
    clock.now += 10 * period
    assert await executor.resend_due() == 0
    await executor.close()


@pytest.mark.asyncio
async def test_an_ack_of_the_first_copy_after_a_resend_releases_too(
    tmp_path: Path,
) -> None:
    clock, ring = Clock(), Ring()
    executor, reports, _ = await _finished(tmp_path, clock=clock, resend=ring)
    first_id = reports.sent[-1][0]
    clock.now += executor._cfg.terminal_resend_s
    await executor.resend_due()  # evicted -> a second message id
    executor.report_acked(first_id)
    assert executor.held() == []
    await executor.close()


@pytest.mark.asyncio
async def test_after_reconcile_makes_every_held_final_report_due(
    tmp_path: Path,
) -> None:
    clock, ring = Clock(), Ring()
    executor, reports, _ = await _finished(tmp_path, clock=clock, resend=ring)
    ring.buffered.add(reports.sent[-1][0])
    assert await executor.resend_due() == 0
    executor.after_reconcile()
    assert await executor.resend_due() == 1  # at once, no period to wait
    await executor.close()


@pytest.mark.asyncio
async def test_release_frees_an_ended_attempt_without_an_ack(tmp_path: Path) -> None:
    executor, _reports, msg = await _finished(tmp_path)
    workdir = executor.work_root / msg.start_attempt.attempt_id
    assert workdir.is_dir()
    release = agent_pb2.ControllerMessage(
        cancel_attempt=agent_pb2.CancelAttempt(
            attempt_id=msg.start_attempt.attempt_id, fencing_token=1, release=True
        )
    )
    executor.handle(release)
    assert executor.held() == []
    await asyncio.gather(*executor._cleanup)
    assert not workdir.exists()
    # a plain cancel of an ended attempt releases nothing (it waits for its Ack)
    (tmp_path / "b").mkdir()
    executor2, _, msg2 = await _finished(tmp_path / "b")
    executor2.handle(
        agent_pb2.ControllerMessage(
            cancel_attempt=agent_pb2.CancelAttempt(
                attempt_id=msg2.start_attempt.attempt_id, fencing_token=1
            )
        )
    )
    assert len(executor2.held()) == 1
    await executor.close()
    await executor2.close()


@pytest.mark.asyncio
async def test_release_of_a_running_attempt_stops_it_then_frees_it(
    tmp_path: Path,
) -> None:
    runtime, reports = FakeRuntime(hang=True), Reports()
    executor, digest = _executor(tmp_path, runtime, reports)
    msg = _start(digest)
    executor.handle(msg)
    for _ in range(100):
        if runtime.calls:
            break
        await asyncio.sleep(0.01)
    wrong = agent_pb2.CancelAttempt(
        attempt_id=msg.start_attempt.attempt_id, fencing_token=7, release=True
    )
    executor.handle(agent_pb2.ControllerMessage(cancel_attempt=wrong))
    await asyncio.sleep(0.02)
    assert len(executor.held()) == 1  # another token: not this attempt
    right = agent_pb2.CancelAttempt(
        attempt_id=msg.start_attempt.attempt_id, fencing_token=1, release=True
    )
    executor.handle(agent_pb2.ControllerMessage(cancel_attempt=right))
    await _settle(executor)
    assert executor.held() == []  # released right after its final report
    last = reports.sent[-1][1]
    assert failure_type_from_proto(last.failure.failure_type).value == "cancelled"
    await executor.close()
