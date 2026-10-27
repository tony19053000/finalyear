"""V0.7 G6 item 0 (D161 N1): repeated free-disk read failures never fail open.

* :class:`WorkdirCap` counts failed free-space reads; ``max_free_read_failures``
  in a row call ``on_disk_unknown(failures)`` once and return; a successful read
  resets the count (a flaky read that recovers never stops a job);
* the executor ends such a run ``FAILED(disk_full)`` with detail
  ``disk_unknown: ...`` (``[agent.execution] free_disk_read_failure_limit``),
  ``retryable=False`` like ``disk_floor`` (the controller's D126 rule retries it
  on another node). ``shutil.disk_usage`` is faked for the work root only.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution import workdir_cap as wc
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
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_command_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    attempt_state_from_proto,
    failure_type_from_proto,
)

pytestmark = pytest.mark.unit

FLOOR = 100
FAIL = object()


def _reads(values: list[Any]) -> Any:
    """A free-space reader that returns ints and raises for :data:`FAIL`;
    past the list it keeps returning plenty."""
    it: Iterator[Any] = iter(values)

    def read(_root: Path) -> int:
        value = next(it, 10**9)
        if value is FAIL:
            raise OSError("statvfs failed")
        return int(value)

    return read


def _run(
    cap: wc.WorkdirCap, timeout: float = 2.0
) -> tuple[list[int], list[int], list[int]]:
    over: list[int] = []
    low: list[int] = []
    unknown: list[int] = []

    async def scenario() -> None:
        task = asyncio.create_task(cap.run(over.append, low.append, unknown.append))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            await wc.stop_watch(task)

    asyncio.run(scenario())
    return over, low, unknown


def _cap(reads: list[Any], limit: int) -> wc.WorkdirCap:
    return wc.WorkdirCap(
        Path("/x"),
        10**12,
        0.001,
        measure=lambda _r: 0,
        free_floor_bytes=FLOOR,
        disk_free=_reads(reads),
        max_free_read_failures=limit,
    )


def test_a_streak_of_failed_reads_calls_back_once_with_the_count() -> None:
    cap = _cap([FAIL, FAIL, FAIL, FAIL], 3)
    over, low, unknown = _run(cap)
    assert unknown == [3] and over == [] and low == []


def test_one_short_of_the_streak_does_nothing() -> None:
    reads = _reads([FAIL, FAIL])
    calls: list[int] = []

    def counted(root: Path) -> int:
        calls.append(1)
        return int(reads(root))

    cap = _cap([], 3)
    cap.disk_free = counted
    over: list[int] = []
    low: list[int] = []
    unknown: list[int] = []

    async def scenario() -> None:
        task = asyncio.create_task(cap.run(over.append, low.append, unknown.append))
        # Wait for the good reads after the two failures (by count, not a fixed
        # time: a 1 ms sleep is ~16 ms on Windows).
        for _ in range(500):
            if len(calls) >= 4 or task.done():
                break
            await asyncio.sleep(0.01)
        await wc.stop_watch(task)

    asyncio.run(scenario())
    assert len(calls) >= 4
    assert unknown == [] and over == [] and low == []
    assert cap.free_read_failures == 0  # the later good reads reset it


def test_a_successful_read_resets_the_count() -> None:
    cap = _cap([FAIL, FAIL, 500, FAIL, FAIL, 500, FAIL, FAIL], 3)
    over, low, unknown = _run(cap, timeout=0.2)
    assert unknown == [] and low == [] and over == []


def test_the_streak_after_a_reset_still_stops_the_job() -> None:
    cap = _cap([FAIL, FAIL, 500, FAIL, FAIL, FAIL], 3)
    _over, _low, unknown = _run(cap)
    assert unknown == [3]


def test_limit_one_stops_on_the_first_failure() -> None:
    cap = _cap([FAIL], 1)
    _over, _low, unknown = _run(cap)
    assert unknown == [1]


def test_a_low_disk_still_wins_after_failures() -> None:
    cap = _cap([FAIL, FLOOR - 1], 3)
    _over, low, unknown = _run(cap)
    assert low == [FLOOR - 1] and unknown == []


def test_without_the_callback_failures_are_only_counted() -> None:
    hits: list[int] = []
    cap = wc.WorkdirCap(
        Path("/x"),
        100,
        0.001,
        measure=_reads([0, 0, 0, 0, 500]),
        free_floor_bytes=FLOOR,
        disk_free=_reads([FAIL] * 10),
        max_free_read_failures=2,
    )
    asyncio.run(asyncio.wait_for(cap.run(hits.append, lambda _f: None), 2))
    assert hits == [500] and cap.free_read_failures >= 2


def test_the_setting_is_bounded() -> None:
    from pydantic import ValidationError

    base = {"controller_address": "127.0.0.1:1"}
    ok = AgentSettings.load(
        data_dir=Path("/tmp/x"),
        agent={**base, "execution": {"free_disk_read_failure_limit": 1}},
    )
    assert ok.agent.execution.free_disk_read_failure_limit == 1
    assert (
        AgentSettings.load(
            data_dir=Path("/tmp/x"), agent=base
        ).agent.execution.free_disk_read_failure_limit
        == 5
    )
    for bad in (0, 1001):
        with pytest.raises((ValidationError, ValueError)):
            AgentSettings.load(
                data_dir=Path("/tmp/x"),
                agent={**base, "execution": {"free_disk_read_failure_limit": bad}},
            )


# ------------------------------------------------------------ the executor


class SlowRuntime:
    name = "subprocess"

    def __init__(self, run_s: float) -> None:
        self.run_s = run_s

    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("subprocess", True)

    async def run(
        self, spec: Any, on_output: Any, *, observer: RunObserver | None = None
    ) -> RunResult:
        assert observer is not None
        observer.enter_run()
        (spec.workdir / "out.bin").write_bytes(os.urandom(4096))
        await asyncio.sleep(self.run_s)
        observer.leave_run()
        return RunResult(exit_code=0)


class Reports:
    def __init__(self) -> None:
        self.sent: list[agent_pb2.AttemptStatus] = []

    async def __call__(self, _name: str, payload: agent_pb2.AttemptStatus) -> str:
        copy_ = agent_pb2.AttemptStatus()
        copy_.CopyFrom(payload)
        self.sent.append(copy_)
        return str(new_command_id())


def _executor(tmp_path: Path, runtime: SlowRuntime, reports: Reports) -> Any:
    project = tmp_path / "proj"
    project.mkdir()
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
            "policy": {"accept_jobs": True, "allow_unsandboxed": True},
            "execution": {
                "grace_s": 0.5,
                "output_check_interval_s": 0.05,
                "free_disk_read_failure_limit": 3,
            },
        },
    )
    setup = ExecutionSetup(
        offer=execution_offer(
            enabled=True,
            allow_unsandboxed=True,
            docker=None,
            subprocess=RuntimeAvailability("subprocess", True),
        ),
        runtimes={"subprocess": runtime},  # type: ignore[dict-item]
    )
    executor = Executor(
        settings,
        setup,
        node_id="node_x",
        report=reports,
        controller_host=lambda: "127.0.0.1",
        fetch=fake_fetch,
    )
    msg = agent_pb2.ControllerMessage(
        message_id=str(new_command_id()),
        start_attempt=agent_pb2.StartAttempt(
            job_id=str(new_job_id()),
            attempt_id=str(new_attempt_id()),
            fencing_token=1,
            spec=agent_pb2.ExecutionSpec(
                bundle_digest=digest,
                blob_port=47110,
                fetch_token="tok",
                manifest_json='{"name":"x","entrypoint":["python","main.py"]}',
                timeout_s=60,
            ),
        ),
    )
    return executor, msg


async def _settle(executor: Executor) -> None:
    for _ in range(800):
        tasks = [a.task for a in executor._attempts.values() if a.task]
        if tasks and all(t.done() for t in tasks):
            return
        await asyncio.sleep(0.01)


def _broken_workdir_disk(monkeypatch: pytest.MonkeyPatch, work_root: Path) -> None:
    """``shutil.disk_usage`` raises for paths under the work root only."""
    real = shutil.disk_usage

    def fake(path: Any) -> Any:
        if Path(path).is_relative_to(work_root):
            raise OSError(5, "Input/output error")
        return real(path)

    monkeypatch.setattr(shutil, "disk_usage", fake)


@pytest.mark.asyncio
async def test_a_job_whose_disk_cannot_be_read_fails_disk_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = Reports()
    executor, msg = _executor(tmp_path, SlowRuntime(30.0), reports)
    _broken_workdir_disk(monkeypatch, executor.work_root)
    executor.handle(msg)
    await _settle(executor)
    final = reports.sent[-1]
    assert attempt_state_from_proto(final.state).value == "failed"
    assert failure_type_from_proto(final.failure.failure_type) is (
        FailureType.DISK_FULL
    )
    assert final.failure.message.startswith("disk_unknown:")
    assert "3 times in a row" in final.failure.message
    assert final.failure.retryable is False
    await executor.close()
