"""V0.7 G5 fix r2: the free-disk floor while a job runs, and the NanoCpus pin.

* :class:`WorkdirCap` also reads the free space of the workdir's disk each poll;
  below ``free_floor_bytes`` it calls ``on_disk_low(free)`` once and returns
  (at the floor is fine; floor 0 = never read; a failed read is skipped);
* the executor: a running job whose workdir disk drops below the agent CAS's
  ``free_disk_floor_bytes`` is stopped and fails ``disk_full`` (detail
  ``disk_floor: ...``, ``retryable=False`` like the ENOSPC path - D126 decides
  on the controller). ``shutil.disk_usage`` is faked for the workdir only; no
  real disk is filled;
* T-10: one ``nano_cpus_for`` for both create kwargs and the create-time check;
  a container whose ``NanoCpus`` is missing while the plan caps CPUs is refused.
"""

from __future__ import annotations

import asyncio
import copy
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
from meshforge.agent.execution.sandbox_check import (
    ExpectedLimits,
    ExpectedMount,
    SandboxViolationError,
    check_host_config,
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

MIB = 1024 * 1024
FLOOR = 100


# ------------------------------------------------------------ WorkdirCap


def _frees(values: list[int]) -> Any:
    it: Iterator[int] = iter(values)
    return lambda _root: next(it)


def _run(cap: wc.WorkdirCap, timeout: float = 2.0) -> tuple[list[int], list[int]]:
    over: list[int] = []
    low: list[int] = []

    async def scenario() -> None:
        task = asyncio.create_task(cap.run(over.append, low.append))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            await wc.stop_watch(task)

    asyncio.run(scenario())
    return over, low


def test_below_the_floor_calls_back_once_with_the_free_bytes() -> None:
    cap = wc.WorkdirCap(
        Path("/x"),
        10**9,
        0.001,
        measure=lambda _r: 0,
        free_floor_bytes=FLOOR,
        disk_free=_frees([500, FLOOR, FLOOR - 1, 0]),
    )
    over, low = _run(cap)
    assert low == [FLOOR - 1] and over == []
    assert cap.free == FLOOR - 1


def test_at_or_above_the_floor_nothing_happens() -> None:
    cap = wc.WorkdirCap(
        Path("/x"),
        10**9,
        0.001,
        measure=lambda _r: 0,
        free_floor_bytes=FLOOR,
        disk_free=lambda _r: FLOOR,
    )
    over, low = _run(cap, timeout=0.05)
    assert over == [] and low == []


def test_floor_zero_never_reads_the_disk() -> None:
    def boom(_root: Path) -> int:
        raise AssertionError("read with no floor")

    cap = wc.WorkdirCap(Path("/x"), 10**9, 0.001, measure=lambda _r: 0, disk_free=boom)
    over, low = _run(cap, timeout=0.05)
    assert over == [] and low == [] and cap.free is None


def test_a_failed_free_read_is_skipped_and_the_output_cap_still_works() -> None:
    def broken(_root: Path) -> int:
        raise OSError("gone")

    cap = wc.WorkdirCap(
        Path("/x"),
        100,
        0.001,
        measure=_frees([0, 50, 500]),
        free_floor_bytes=FLOOR,
        disk_free=broken,
    )
    over, low = _run(cap)
    assert over == [500] and low == []


def test_without_a_disk_low_callback_the_floor_is_not_acted_on() -> None:
    hits: list[int] = []
    cap = wc.WorkdirCap(
        Path("/x"),
        100,
        0.001,
        measure=_frees([0, 0, 500]),
        free_floor_bytes=FLOOR,
        disk_free=lambda _r: 0,
    )
    asyncio.run(asyncio.wait_for(cap.run(hits.append), 2))
    assert hits == [500]


def test_the_default_reader_is_shutil_disk_usage(tmp_path: Path) -> None:
    assert wc.free_bytes(tmp_path) == pytest.approx(
        shutil.disk_usage(tmp_path).free, rel=0.05
    )


# ------------------------------------------------------------ the executor


class SlowRuntime:
    """Writes a little, then 'runs' for a while (the floor must stop it)."""

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
            "execution": {"grace_s": 0.5, "output_check_interval_s": 0.05},
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


def _fake_workdir_disk(
    monkeypatch: pytest.MonkeyPatch, work_root: Path, free: int
) -> None:
    """``shutil.disk_usage`` says ``free`` for paths under the work root only
    (staging's CAS check on ``data/cas`` sees the real disk)."""
    real = shutil.disk_usage

    def fake(path: Any) -> Any:
        usage = real(path)
        if Path(path).is_relative_to(work_root):
            return type(usage)(usage.total, usage.total - free, free)
        return usage

    monkeypatch.setattr(shutil, "disk_usage", fake)


@pytest.mark.asyncio
async def test_a_job_whose_disk_drops_below_the_floor_fails_disk_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = Reports()
    executor, msg = _executor(tmp_path, SlowRuntime(30.0), reports)
    floor = executor.cas.store.free_disk_floor_bytes
    assert floor > 0  # the agent's staging floor is what the run keeps
    _fake_workdir_disk(monkeypatch, executor.work_root, floor - 1)
    executor.handle(msg)
    await _settle(executor)
    final = reports.sent[-1]
    assert attempt_state_from_proto(final.state).value == "failed"
    assert failure_type_from_proto(final.failure.failure_type) is (
        FailureType.DISK_FULL
    )
    assert final.failure.message.startswith("disk_floor:")
    assert str(floor - 1) in final.failure.message
    assert final.failure.retryable is False
    await executor.close()


@pytest.mark.asyncio
async def test_a_job_with_the_floor_free_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = Reports()
    executor, msg = _executor(tmp_path, SlowRuntime(0.5), reports)
    _fake_workdir_disk(
        monkeypatch, executor.work_root, executor.cas.store.free_disk_floor_bytes
    )
    executor.handle(msg)
    await _settle(executor)
    assert attempt_state_from_proto(reports.sent[-1].state).value == "done"
    await executor.close()


# ------------------------------------------------------------ T-10 NanoCpus

WORK = "/data/work/att_x"
MEM = 67108864
TMP = "rw,nosuid,nodev,size=512m"
HOST: dict[str, Any] = {
    "Binds": None,
    "NetworkMode": "none",
    "CapAdd": None,
    "CapDrop": ["ALL"],
    "IpcMode": "private",
    "PidMode": "",
    "UTSMode": "",
    "UsernsMode": "",
    "CgroupnsMode": "private",
    "Privileged": False,
    "ReadonlyRootfs": True,
    "SecurityOpt": ["no-new-privileges:true"],
    "Tmpfs": {"/tmp": TMP},
    "Memory": MEM,
    "MemorySwap": MEM,
    "OomKillDisable": False,
    "Sysctls": None,
    "PortBindings": {},
    "GroupAdd": None,
    "PidsLimit": 64,
    "Devices": [],
    "Mounts": [{"Type": "bind", "Source": WORK, "Target": "/work"}],
    "MaskedPaths": ["/proc/acpi", "/proc/kcore"],
    "ReadonlyPaths": ["/proc/bus", "/proc/sys", "/proc/sysrq-trigger"],
    "VolumesFrom": None,
}


def _check_nano(value: Any, planned: int) -> None:
    host = copy.deepcopy(HOST)
    host["NanoCpus"] = value
    check_host_config(
        host,
        role="run",
        mounts=[ExpectedMount(WORK, "/work", False)],
        network_allowed=False,
        limits=ExpectedLimits(MEM, TMP, pids=64, nano_cpus=planned),
    )


@pytest.mark.parametrize("missing", [None, 0])
def test_a_missing_cpu_cap_is_refused_when_the_plan_has_one(missing: Any) -> None:
    pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    from meshforge.agent.execution.docker_runtime import nano_cpus_for

    with pytest.raises(SandboxViolationError) as exc:
        _check_nano(missing, nano_cpus_for(1.5))
    assert exc.value.what == "cpu_limit"
    _check_nano(nano_cpus_for(1.5), nano_cpus_for(1.5))
    _check_nano(None, 0)  # no cap planned, none reported


def test_create_kwargs_and_the_check_share_one_rounding(tmp_path: Path) -> None:
    docker = pytest.importorskip("docker", reason="needs the agent extra")
    from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
    from meshforge.agent.execution.docker_runtime import (
        DockerConfig,
        DockerRuntime,
        _planned_limits,
        nano_cpus_for,
    )
    from meshforge.agent.execution.runtime import RunLimits, RunSpec

    envs = EnvBuilder(EnvsConfig(root=tmp_path / "envs"))
    runtime = DockerRuntime(DockerConfig(agent_label="n"), envs=envs)
    for cpus in (0.1, 0.3, 1.5, 2.01, 2.7, 7.0):  # 2.01: int != round
        spec = RunSpec(
            attempt_id=str(new_attempt_id()),
            image="img",
            argv=("python", "-c", "1"),
            workdir=tmp_path,
            env={},
            limits=RunLimits(timeout_s=10, memory_bytes=MEM, pids=64, cpus=cpus),
        )
        for role in ("run", "setup"):
            kwargs = runtime._create_kwargs(docker, spec, role)
            planned = _planned_limits(spec, role, TMP)
            assert kwargs["nano_cpus"] == planned.nano_cpus == nano_cpus_for(cpus)
