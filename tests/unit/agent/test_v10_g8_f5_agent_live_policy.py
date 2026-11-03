"""V1.0 G8 F5 (D428), agent side: the agent probes and advertises what it CAN
run whatever its owner's policy says at start; the owner's LIVE policy alone
decides what may run (executor plan check, node prepare).
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.envs.node_prepare import PrepareService
from meshforge.agent.envs.prepare import PrepareReport
from meshforge.agent.execution.offer import (
    ExecutionSetup,
    build_execution,
    execution_offer,
)
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.policy import RESTART_KEYS, apply_changes
from meshforge.agent.settings import AgentSettings
from meshforge.common import owner_policy as op
from meshforge.common.capabilities import EXEC_CAPABILITY, EXEC_GPU_CAPABILITY
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


def _settings(tmp_path: Path, **policy: Any) -> AgentSettings:
    return AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={"controller_address": "127.0.0.1:1", "policy": policy},
    )


class _Docker:
    name = "docker"

    def __init__(self, available: bool) -> None:
        self.available = available
        self.probed = 0

    async def probe(self) -> RuntimeAvailability:
        self.probed += 1
        if not self.available:
            return RuntimeAvailability("docker", False, reason="docker_sdk_missing")
        return RuntimeAvailability("docker", True, sandboxed=True, gpu_capable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("docker", [True, False])
async def test_jobs_off_at_start_still_advertises_exec_and_exec_gpu(
    tmp_path: Path, docker: bool
) -> None:
    """The lab laptops: Run jobs (and no-container) off when the agent
    connected. Docker or not, the agent says what it CAN run."""
    fake = _Docker(docker)
    setup = await build_execution(
        _settings(tmp_path, accept_jobs=False, allow_unsandboxed=False),
        node_id="node_x",
        host_has_nvidia_gpu=True,
        docker_factory=lambda _c: fake,  # type: ignore[arg-type,return-value]
    )
    assert fake.probed == 1
    assert EXEC_CAPABILITY in setup.offer.capabilities
    assert EXEC_GPU_CAPABILITY in setup.offer.capabilities
    assert "subprocess" in setup.runtimes


def test_no_policy_key_needs_a_restart_any_more(tmp_path: Path) -> None:
    assert dict(RESTART_KEYS) == {}
    settings = _settings(tmp_path, accept_jobs=False, allow_unsandboxed=False)
    result = apply_changes(settings, {"accept_jobs": True, "allow_unsandboxed": True})
    assert result.restart_keys == ()


# ------------------------------------------------------------- node prepare


class _Runtime:
    def __init__(self) -> None:
        self.calls = 0

    async def prepare_env(self, spec: Any, on_output: Any) -> tuple[str, str]:
        self.calls += 1
        return "k" * 64, "built"


class _Sent:
    def __init__(self) -> None:
        self.statuses: list[agent_pb2.PrepareEnvStatus] = []

    async def __call__(self, name: str, payload: Any) -> str:
        copy = agent_pb2.PrepareEnvStatus()
        copy.CopyFrom(payload)
        self.statuses.append(copy)
        return "m"


def _prepare(
    tmp_path: Path, runtimes: dict[str, Any], policy: list[op.OwnerPolicy]
) -> tuple[PrepareService, _Sent, list[str]]:
    setup = ExecutionSetup(
        offer=execution_offer(
            enabled=True, allow_unsandboxed=True, docker=None, subprocess=None
        ),
        runtimes=runtimes,
    )
    sent = _Sent()
    pulled: list[str] = []

    async def images(bases: Sequence[str]) -> PrepareReport:
        pulled.extend(bases)
        return PrepareReport(docker="unavailable: DockerException", uv="uv")

    service = PrepareService(
        AgentSettings.model_validate(
            {"data_dir": str(tmp_path), "agent": {"controller_address": "127.0.0.1:1"}}
        ),
        setup,
        sent,
        prepare_images=images,
        policy=lambda: policy[0],
    )
    return service, sent, pulled


async def _settle(service: PrepareService) -> None:
    for _ in range(300):
        if not service._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("prepare never finished")


@pytest.mark.asyncio
async def test_prepare_follows_the_live_accept_jobs(tmp_path: Path) -> None:
    runtime = _Runtime()
    policy = [dataclasses.replace(op.OPEN_POLICY, accept_jobs=False)]
    service, sent, pulled = _prepare(tmp_path, {"subprocess": runtime}, policy)
    env = agent_pb2.EnvSpecRequest(base="python-cpu", requirements=b"numpy\n")
    service.handle(
        agent_pb2.PrepareEnv(request_id="r1", base_images=["python-cpu"], envs=[env])
    )
    await _settle(service)
    last = sent.statuses[-1]
    assert last.done
    assert {i.reason for i in last.items} == {"policy_not_accepting"}
    assert {i.failure_type for i in last.items} == {"unauthorized"}
    assert pulled == [] and runtime.calls == 0  # nothing pulled or built

    policy[0] = op.OPEN_POLICY  # turned on live
    service.handle(agent_pb2.PrepareEnv(request_id="r2", envs=[env]))
    await _settle(service)
    assert [i.state for i in sent.statuses[-1].items] == ["built"]
    assert runtime.calls == 1


@pytest.mark.asyncio
async def test_prepare_never_builds_unsandboxed_when_the_owner_disallows(
    tmp_path: Path,
) -> None:
    runtime = _Runtime()
    policy = [dataclasses.replace(op.OPEN_POLICY, allow_unsandboxed=False)]
    service, sent, _ = _prepare(
        tmp_path, {"subprocess": runtime, "venv": runtime}, policy
    )
    named = agent_pb2.EnvSpecRequest(base="python-cpu", runtime="subprocess")
    any_runtime = agent_pb2.EnvSpecRequest(base="python-cpu")
    service.handle(agent_pb2.PrepareEnv(request_id="r1", envs=[named, any_runtime]))
    await _settle(service)
    items = sent.statuses[-1].items
    assert [i.reason for i in items] == ["policy_unsandboxed", "runtime_unavailable"]
    assert runtime.calls == 0


# ------------------------------------------- look only, act later (D428)


class _CgroupSpy:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        from meshforge.agent.execution.cpu_cgroup import CpuCgroupProbe

        self.calls += 1
        return CpuCgroupProbe(None, "not_delegated")


@pytest.mark.asyncio
async def test_jobs_off_start_has_zero_side_effects(tmp_path: Path) -> None:
    """No GPU-probe container (the probe image is withheld, so ``probe_docker``
    reads the daemon's runtimes only), no cgroup probe (it may write
    ``subtree_control``) or sweep, no Docker handle for orphan sweep / GC."""
    configs: list[Any] = []
    spy = _CgroupSpy()
    fake = _Docker(True)

    def factory(config: Any) -> Any:
        configs.append(config)
        return fake

    setup = await build_execution(
        _settings(tmp_path, accept_jobs=False),
        node_id="node_x",
        host_has_nvidia_gpu=True,
        docker_factory=factory,
        cgroup_probe=spy,
    )
    assert not setup.complete
    assert configs[0].gpu_probe_image is None
    assert spy.calls == 0 and setup.cpu_cgroups is None
    assert setup.docker is None  # no orphan sweep, no image GC
    assert EXEC_CAPABILITY in setup.offer.capabilities  # CAN is still said


@pytest.mark.asyncio
async def test_jobs_on_start_runs_the_side_effecting_probes(tmp_path: Path) -> None:
    configs: list[Any] = []
    spy = _CgroupSpy()

    def factory(config: Any) -> Any:
        configs.append(config)
        return _Docker(True)

    setup = await build_execution(
        _settings(tmp_path, accept_jobs=True),
        node_id="node_x",
        host_has_nvidia_gpu=True,
        docker_factory=factory,
        cgroup_probe=spy,
    )
    assert setup.complete and spy.calls == 1
    assert configs[0].gpu_probe_image is not None
    assert setup.docker is not None


def test_probe_docker_without_a_probe_image_never_creates_a_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    from meshforge.agent.execution import docker_support

    class _Containers:
        def create(self, *_a: Any, **_k: Any) -> Any:
            raise AssertionError("a container was created while looking only")

    class _Client:
        containers = _Containers()

        def ping(self) -> bool:
            return True

        def version(self) -> dict[str, str]:
            return {"Version": "29.0"}

        def info(self) -> dict[str, Any]:
            return {
                "SecurityOptions": ["name=seccomp,profile=builtin"],
                "Runtimes": {"nvidia": {}},
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr(docker_support, "open_client", lambda _t: _Client())
    found = docker_support.probe_docker(
        timeout_s=1.0,
        host_has_nvidia_gpu=True,
        gpu_probe_image=None,
        agent_label="node_x",
    )
    assert found.available and found.gpu_capable
    assert found.gpu_reason.startswith("assumed")


def test_no_runtime_at_all_is_not_enabled() -> None:
    offer = execution_offer(
        enabled=False, allow_unsandboxed=True, docker=None, subprocess=None
    )
    assert not offer.enabled and offer.capabilities == ()


class _Target:
    def __init__(self) -> None:
        self.setups: list[ExecutionSetup] = []

    def replace_setup(self, setup: ExecutionSetup) -> None:
        self.setups.append(setup)


@pytest.mark.asyncio
async def test_activation_runs_once_swaps_the_setup_and_readvertises(
    tmp_path: Path,
) -> None:
    from meshforge.agent.execution.activation import ExecutionActivator
    from meshforge.agent.execution.cpu_cgroup import CpuCgroups

    look_only = dataclasses.replace(
        ExecutionSetup(
            offer=execution_offer(
                enabled=True,
                allow_unsandboxed=True,
                docker=None,
                subprocess=RuntimeAvailability("subprocess", True),
            )
        ),
        complete=False,
    )
    full = ExecutionSetup(
        offer=execution_offer(
            enabled=True,
            allow_unsandboxed=True,
            docker=RuntimeAvailability(
                "docker", True, sandboxed=True, gpu_capable=True
            ),
            subprocess=RuntimeAvailability("subprocess", True),
        ),
        cpu_cgroups=CpuCgroups(tmp_path),
    )
    builds: list[dict[str, Any]] = []

    async def build(*_a: Any, **kw: Any) -> ExecutionSetup:
        builds.append(kw)
        return full

    starts: list[int] = []
    cgroup: list[bool] = []
    handovers: list[str] = []

    async def start() -> None:
        starts.append(1)

    hardware = agent_pb2.HardwareSummary(
        capabilities=["v0.3", "hardware-scores", "exec", "exec-subprocess"]
    )
    executor, preparer = _Target(), _Target()
    activator = ExecutionActivator(
        _settings(tmp_path, accept_jobs=True),
        look_only,
        node_id="node_x",
        host_has_nvidia_gpu=True,
        hardware=hardware,
        targets=(executor, preparer),
        start=start,
        on_cgroup=lambda available: cgroup.append(available) or True,
        request_handover=lambda reason: handovers.append(reason) or True,
        build=build,
    )
    assert activator.pending
    assert await activator.activate()
    assert not await activator.activate()  # once
    assert len(builds) == 1 and builds[0]["side_effects"] is True
    assert executor.setups == [full] and preparer.setups == [full]
    assert starts == [1] and cgroup == [True]
    caps = set(hardware.capabilities)
    assert {"exec", "exec-docker", "exec-gpu", "exec-split"} <= caps
    assert {"v0.3", "hardware-scores"} <= caps
    assert activator.readvertise() and handovers == ["capabilities_changed"]


def test_live_policy_enforces_the_cpu_cap_once_the_cgroup_exists(
    tmp_path: Path,
) -> None:
    from meshforge.agent.runtime import build_live_policy, cgroup_updater

    settings = _settings(
        tmp_path, accept_jobs=True, allow_unsandboxed=True, max_cpu_percent=50
    )
    live = build_live_policy(settings, gpu_count=0, cpu_cgroup=False)
    linux = any("max_cpu_percent" in c for c in live.policy.not_enforced)
    changed = cgroup_updater(live, gpu_count=0)(True)
    assert not any("max_cpu_percent" in c for c in live.policy.not_enforced)
    assert changed is linux  # only Linux advertised it not enforced before


@pytest.mark.asyncio
async def test_a_look_only_setup_never_runs_an_attempt(tmp_path: Path) -> None:
    """Jobs on but the full setup not built yet: retryable refusal; jobs off:
    the owner's refusal. Nothing is fetched or run either way."""
    from meshforge.agent.execution.executor import Executor

    look_only = dataclasses.replace(
        ExecutionSetup(
            offer=execution_offer(
                enabled=True,
                allow_unsandboxed=True,
                docker=None,
                subprocess=RuntimeAvailability("subprocess", True),
            )
        ),
        complete=False,
    )
    reports: list[Any] = []

    async def report(name: str, payload: Any) -> str:
        copy = type(payload)()
        copy.CopyFrom(payload)
        reports.append(copy)
        return "m"

    class _Policy:
        def __init__(self, policy: op.OwnerPolicy) -> None:
            self.policy = policy

    from meshforge.common.errors import FailureType
    from meshforge.common.ids import new_attempt_id, new_job_id
    from meshforge.common.proto_codec import failure_type_to_proto

    off = dataclasses.replace(op.OPEN_POLICY, accept_jobs=False)
    for policy, failure, retryable in (
        (op.OPEN_POLICY, FailureType.RESOURCE_BUSY, True),
        (off, FailureType.UNAUTHORIZED, False),
    ):
        reports.clear()
        executor = Executor(
            _settings(tmp_path),
            look_only,
            node_id="node_x",
            report=report,
            controller_host=lambda: "127.0.0.1",
            policy=_Policy(policy),  # type: ignore[arg-type]
        )
        msg = agent_pb2.ControllerMessage(
            start_attempt=agent_pb2.StartAttempt(
                job_id=str(new_job_id()),
                attempt_id=str(new_attempt_id()),
                fencing_token=1,
            )
        )
        executor.handle(msg)
        for _ in range(200):
            if reports:
                break
            await asyncio.sleep(0.01)
        await executor.close()
        last = reports[-1]
        assert last.failure.failure_type == failure_type_to_proto(failure), last
        assert last.failure.retryable is retryable


# ------------------------------------- a failed activation recovers (round 3)


def _look_only() -> ExecutionSetup:
    return dataclasses.replace(
        ExecutionSetup(
            offer=execution_offer(
                enabled=True,
                allow_unsandboxed=True,
                docker=None,
                subprocess=RuntimeAvailability("subprocess", True),
            )
        ),
        complete=False,
    )


def _full() -> ExecutionSetup:
    return ExecutionSetup(
        offer=execution_offer(
            enabled=True,
            allow_unsandboxed=True,
            docker=None,
            subprocess=RuntimeAvailability("subprocess", True),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["build", "start"])
async def test_a_failed_activation_stays_look_only_and_retries_after_backoff(
    tmp_path: Path, where: str
) -> None:
    from meshforge.agent.execution.activation import ExecutionActivator

    now = [100.0]
    calls = {"build": 0, "start": 0}

    async def build(*_a: Any, **_k: Any) -> ExecutionSetup:
        calls["build"] += 1
        if where == "build" and calls["build"] == 1:
            raise RuntimeError("probe crashed")
        return _full()

    async def start() -> None:
        calls["start"] += 1
        if where == "start" and calls["start"] == 1:
            raise OSError("work root not writable")

    executor = _Target()
    look_only = _look_only()
    activator = ExecutionActivator(
        _settings(tmp_path, accept_jobs=True),
        look_only,
        node_id="node_x",
        host_has_nvidia_gpu=False,
        hardware=agent_pb2.HardwareSummary(),
        targets=(executor,),
        start=start,
        on_cgroup=lambda _a: False,
        request_handover=lambda _r: True,
        build=build,
        clock=lambda: now[0],
    )
    assert not await activator.activate()
    assert activator.pending and activator.failures == 1
    assert executor.setups[-1] is look_only  # never left on a half setup
    assert not activator.due()  # backoff
    now[0] += 2.5
    assert activator.due()
    assert await activator.activate()
    assert not activator.pending and activator.activations == 1


@pytest.mark.asyncio
async def test_the_policy_watch_retries_a_failed_activation_without_owner_action(
    tmp_path: Path,
) -> None:
    """Build raises once: the PolicyUpdate is still sent at once, the watch
    retries on its own ticks, then re-advertises - no file change needed."""
    from meshforge.agent.execution.activation import ExecutionActivator
    from meshforge.agent.policy import write_overlay
    from meshforge.agent.runtime import build_live_policy, start_policy_watch

    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "policy": {"accept_jobs": False, "reload_interval_s": 0.1},
        },
    )
    live = build_live_policy(settings, gpu_count=0)
    builds = [0]
    ticks = itertools.count(0, 10)

    async def build(*_a: Any, **_k: Any) -> ExecutionSetup:
        builds[0] += 1
        if builds[0] == 1:
            raise RuntimeError("probe crashed")
        return _full()

    async def start() -> None:
        return None

    class _Client:
        node_id = "node_x"

        def __init__(self) -> None:
            self.updates: list[bool] = []
            self.handovers: list[str] = []

        async def send_report(self, name: str, payload: Any) -> str:
            assert name == "policy_update"
            self.updates.append(payload.policy.accept_jobs)
            return "m"

        def request_handover(self, reason: str) -> bool:
            self.handovers.append(reason)
            return True

    client = _Client()
    activator = ExecutionActivator(
        settings,
        _look_only(),
        node_id="node_x",
        host_has_nvidia_gpu=False,
        hardware=agent_pb2.HardwareSummary(),
        targets=(),
        start=start,
        on_cgroup=lambda _a: False,
        request_handover=client.request_handover,
        build=build,
        clock=lambda: float(next(ticks)),  # +10 s per reading: backoff elapses
    )
    task = start_policy_watch(settings, client, live, activator=activator)  # type: ignore[arg-type]
    try:
        write_overlay(live.path, {"accept_jobs": True})
        for _ in range(200):
            if not activator.pending and client.handovers:
                break
            await asyncio.sleep(0.02)
        assert builds[0] == 2 and activator.activations == 1
        assert client.updates[0] is True  # sent although the first build failed
        assert client.handovers == ["capabilities_changed"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_an_off_written_during_activation_is_what_the_controller_hears(
    tmp_path: Path,
) -> None:
    from meshforge.agent.execution.activation import ExecutionActivator
    from meshforge.agent.policy import write_overlay
    from meshforge.agent.runtime import build_live_policy, start_policy_watch

    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "policy": {"accept_jobs": False, "reload_interval_s": 0.1},
        },
    )
    live = build_live_policy(settings, gpu_count=0)

    async def build(*_a: Any, **_k: Any) -> ExecutionSetup:
        write_overlay(live.path, {"accept_jobs": False})  # the owner changed mind
        return _full()

    async def start() -> None:
        return None

    sent: list[bool] = []

    class _Client:
        node_id = "node_x"

        async def send_report(self, _n: str, payload: Any) -> str:
            sent.append(payload.policy.accept_jobs)
            return "m"

        def request_handover(self, _r: str) -> bool:
            return True

    activator = ExecutionActivator(
        settings,
        _look_only(),
        node_id="node_x",
        host_has_nvidia_gpu=False,
        hardware=agent_pb2.HardwareSummary(),
        targets=(),
        start=start,
        on_cgroup=lambda _a: False,
        request_handover=lambda _r: True,
        build=build,
    )
    task = start_policy_watch(settings, _Client(), live, activator=activator)  # type: ignore[arg-type]
    try:
        write_overlay(live.path, {"accept_jobs": True})
        for _ in range(200):
            if sent:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.2)
        assert sent and sent[-1] is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
