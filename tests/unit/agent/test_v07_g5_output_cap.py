"""V0.7 G5 part 2 (roadmap 0.7.9, D143): the output-dir size cap, enforced
while the job runs.

* :func:`disk_usage`: allocated blocks, each inode once, symlinks not
  followed, a folder the job made unreadable measured anyway, an unlistable
  one unmeasurable (fail closed);
* :class:`WorkdirCap`: calls back once past the cap (or on an unmeasurable
  folder, or when measuring itself crashes) and never below it;
* the executor: a running job that writes past ``max_written_bytes`` is
  stopped and fails ``output_error`` with ``output_limit`` (not retried); one
  that stays under it finishes normally.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
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

pytestmark = [pytest.mark.unit, pytest.mark.security]

MIB = 1024 * 1024
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX blocks/modes")


# ------------------------------------------------------------ disk_usage


@posix_only
def test_usage_counts_blocks_once_and_never_follows_links(tmp_path: Path) -> None:
    empty = wc.disk_usage(tmp_path)
    (tmp_path / "a.bin").write_bytes(b"x" * MIB)
    one = wc.disk_usage(tmp_path)
    assert one - empty >= MIB
    os.link(tmp_path / "a.bin", tmp_path / "hard.bin")  # same inode: once
    (tmp_path / "link").symlink_to("/usr")  # never followed
    assert wc.disk_usage(tmp_path) - one < 64 * 1024


@posix_only
def test_a_sparse_file_costs_what_it_occupies(tmp_path: Path) -> None:
    with (tmp_path / "sparse").open("wb") as fh:
        fh.truncate(10 * 1024**3)  # 10 GiB apparent, ~0 allocated
    assert wc.disk_usage(tmp_path) < MIB


@posix_only
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads all"
)
def test_a_folder_made_unreadable_is_measured_anyway(tmp_path: Path) -> None:
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "big.bin").write_bytes(b"y" * MIB)
    hidden.chmod(0)
    try:
        assert wc.disk_usage(tmp_path) >= MIB
    finally:
        hidden.chmod(0o700)


def test_an_unlistable_folder_is_unmeasurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "d").mkdir()
    real = os.scandir

    def refuse(path: Any) -> Any:
        if str(path).endswith("d"):
            raise PermissionError(13, "denied")
        return real(path)

    monkeypatch.setattr(wc.os, "scandir", refuse)
    monkeypatch.setattr(wc.os, "chmod", lambda *_a: None)
    assert wc.disk_usage(tmp_path) == wc.UNMEASURABLE


def test_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert wc.disk_usage(tmp_path / "gone") == 0


# ------------------------------------------------------------ WorkdirCap


def _measures(values: list[int]) -> Any:
    it: Iterator[int] = iter(values)

    def measure(_root: Path) -> int:
        return next(it)

    return measure


def _watch(values: list[int], max_bytes: int = 100) -> tuple[list[int], Any]:
    hits: list[int] = []
    cap = wc.WorkdirCap(Path("/x"), max_bytes, 0.001, measure=_measures(values))

    async def scenario() -> None:
        await asyncio.wait_for(cap.run(hits.append), 2)

    return hits, scenario


def test_the_cap_calls_back_once_past_the_limit() -> None:
    hits, scenario = _watch([1000, 1050, 1100, 1101, 5000])
    asyncio.run(scenario())
    assert hits == [101]


def test_an_unmeasurable_folder_trips_the_cap() -> None:
    hits, scenario = _watch([10, 20, wc.UNMEASURABLE])
    asyncio.run(scenario())
    assert hits == [wc.UNMEASURABLE]


def test_a_crashing_measurement_trips_the_cap() -> None:
    hits: list[int] = []
    calls = {"n": 0}

    def measure(_root: Path) -> int:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("boom")
        return 0

    cap = wc.WorkdirCap(Path("/x"), 100, 0.001, measure=measure)
    asyncio.run(asyncio.wait_for(cap.run(hits.append), 2))
    assert hits == [wc.UNMEASURABLE]


def test_under_the_cap_nothing_happens() -> None:
    hits: list[int] = []
    cap = wc.WorkdirCap(Path("/x"), 100, 0.001, measure=lambda _r: 50)

    async def scenario() -> None:
        task = asyncio.create_task(cap.run(hits.append))
        await asyncio.sleep(0.05)
        await wc.stop_watch(task)

    asyncio.run(scenario())
    assert hits == [] and cap.written == 0


# ------------------------------------------------------------ the executor


class WritingRuntime:
    name = "subprocess"

    def __init__(self, write: int) -> None:
        self.write = write

    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("subprocess", True)

    async def run(
        self, spec: Any, on_output: Any, *, observer: RunObserver | None = None
    ) -> RunResult:
        assert observer is not None
        observer.enter_run()
        # a real file, written while "running" (incompressible bytes)
        (spec.workdir / "out.bin").write_bytes(os.urandom(self.write))
        await asyncio.sleep(1.0)
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


def _executor(tmp_path: Path, runtime: WritingRuntime, reports: Reports) -> Any:
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
                "max_written_bytes": MIB,
                "output_check_interval_s": 0.05,
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
    for _ in range(500):
        tasks = [a.task for a in executor._attempts.values() if a.task]
        if tasks and all(t.done() for t in tasks):
            return
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_job_past_the_output_cap_is_stopped(tmp_path: Path) -> None:
    reports = Reports()
    executor, msg = _executor(tmp_path, WritingRuntime(3 * MIB), reports)
    executor.handle(msg)
    await _settle(executor)
    final = reports.sent[-1]
    assert attempt_state_from_proto(final.state).value == "failed"
    assert failure_type_from_proto(final.failure.failure_type) is (
        FailureType.OUTPUT_ERROR
    )
    assert final.failure.message.startswith("output_limit:")
    assert final.failure.retryable is False
    await executor.close()


@pytest.mark.asyncio
async def test_a_job_under_the_output_cap_finishes(tmp_path: Path) -> None:
    reports = Reports()
    executor, msg = _executor(tmp_path, WritingRuntime(64 * 1024), reports)
    executor.handle(msg)
    await _settle(executor)
    assert attempt_state_from_proto(reports.sent[-1].state).value == "done"
    await executor.close()
