"""V1.0 G8 F5 tester: adversarial agent-side checks of "CAN vs ALLOWS" (D428)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.envs.node_prepare import PrepareService
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.plan import PlanError, plan_attempt
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.settings import AgentSettings, ExecutionSettings
from meshforge.common import owner_policy as op
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

DOCKER = RuntimeAvailability("docker", True, sandboxed=True)
SUB = RuntimeAvailability("subprocess", True)
VENV = RuntimeAvailability("venv", True)
OPEN = op.OPEN_POLICY
OFF = dataclasses.replace(op.OPEN_POLICY, accept_jobs=False)
NO_UNSANDBOXED = dataclasses.replace(op.OPEN_POLICY, allow_unsandboxed=False)
BASE = {"name": "x", "entrypoint": ["python", "main.py"]}


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))


def _msg(runtime: str = "") -> Any:
    return agent_pb2.StartAttempt(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        spec=agent_pb2.ExecutionSpec(
            bundle_digest="sha256:" + "0" * 64,
            fetch_token="tok",
            manifest_json=json.dumps(BASE),
            timeout_s=60,
            runtime=runtime,
            owner_name="alice",
        ),
    )


def _plan(
    tmp_path: Path, policy: op.OwnerPolicy, *, docker: bool, runtime: str = ""
) -> Any:
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,  # the F5 agent offers them regardless of the policy
        docker=DOCKER if docker else None,
        subprocess=SUB,
        venv=VENV,
    )
    return plan_attempt(
        _msg(runtime),
        settings=ExecutionSettings(),
        offer=offer,
        declared_gpus=(),
        workdir=tmp_path,
        policy=policy,
        cpu_threads=8,
    )


@pytest.mark.parametrize("runtime", ["", "subprocess", "venv"])
def test_unsandboxed_advertised_but_never_used_when_disallowed(
    tmp_path: Path, runtime: str
) -> None:
    """(2) no Docker: the only runtimes are unsandboxed; policy says no."""
    with pytest.raises(PlanError) as exc:
        _plan(tmp_path, NO_UNSANDBOXED, docker=False, runtime=runtime)
    assert exc.value.failure_type is FailureType.UNAUTHORIZED
    assert exc.value.message.startswith("policy_denied:policy_unsandboxed")


def test_pinned_unsandboxed_request_denied_even_with_docker_present(
    tmp_path: Path,
) -> None:
    """The controller (stale view) names subprocess; Docker exists; the owner
    turned allow_unsandboxed off live. The agent must not fall back silently."""
    with pytest.raises(PlanError) as exc:
        _plan(tmp_path, NO_UNSANDBOXED, docker=True, runtime="subprocess")
    assert exc.value.message.startswith("policy_denied:")


def test_unrequested_job_with_docker_picks_sandboxed_when_disallowed(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, NO_UNSANDBOXED, docker=True)
    assert plan is not None


@pytest.mark.parametrize("docker", [True, False])
def test_jobs_off_always_refused_whatever_runtime_is_advertised(
    tmp_path: Path, docker: bool
) -> None:
    for runtime in ("", "subprocess", "venv", "docker"):
        if runtime == "docker" and not docker:
            continue
        with pytest.raises(PlanError) as exc:
            _plan(tmp_path, OFF, docker=docker, runtime=runtime)
        assert exc.value.message.startswith("policy_denied:policy_not_accepting")


def test_deny_all_refuses_and_toggle_back_allows(tmp_path: Path) -> None:
    with pytest.raises(PlanError):
        _plan(tmp_path, op.DENY_ALL, docker=False)
    _plan(tmp_path, OPEN, docker=False)  # next attempt follows the new policy


# ------------------------------------------------------------ node prepare


class _Rt:
    def __init__(self) -> None:
        self.calls = 0

    async def prepare_env(self, spec: Any, on_output: Any) -> tuple[str, str]:
        self.calls += 1
        await asyncio.sleep(0)
        return "k" * 64, "built"


class _Sent:
    def __init__(self) -> None:
        self.statuses: list[agent_pb2.PrepareEnvStatus] = []

    async def __call__(self, name: str, payload: Any) -> str:
        copy = agent_pb2.PrepareEnvStatus()
        copy.CopyFrom(payload)
        self.statuses.append(copy)
        return "m"


def _service(
    tmp_path: Path, runtimes: dict[str, Any], policy_fn: Any
) -> tuple[PrepareService, _Sent]:
    setup = ExecutionSetup(
        offer=execution_offer(
            enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB
        ),
        runtimes=runtimes,
    )
    sent = _Sent()

    async def images(bases: Any) -> Any:
        raise AssertionError("images pulled while jobs off")

    service = PrepareService(
        AgentSettings.model_validate(
            {"data_dir": str(tmp_path), "agent": {"controller_address": "127.0.0.1:1"}}
        ),
        setup,
        sent,
        prepare_images=images,
        policy=policy_fn,
    )
    return service, sent


async def _settle(service: PrepareService) -> None:
    for _ in range(500):
        if not service._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("prepare never finished")


@pytest.mark.asyncio
async def test_prepare_jobs_off_with_base_images_requested_pulls_nothing(
    tmp_path: Path,
) -> None:
    rt = _Rt()
    service, sent = _service(tmp_path, {"subprocess": rt}, lambda: OFF)
    env = agent_pb2.EnvSpecRequest(base="python-cpu", requirements=b"numpy\n")
    service.handle(
        agent_pb2.PrepareEnv(request_id="r", base_images=["python-cpu"], envs=[env] * 5)
    )
    await _settle(service)
    assert sent.statuses[-1].done
    assert {i.reason for i in sent.statuses[-1].items} == {"policy_not_accepting"}
    assert rt.calls == 0


@pytest.mark.asyncio
async def test_prepare_deny_all_and_policy_callable_raising_is_not_a_build(
    tmp_path: Path,
) -> None:
    rt = _Rt()
    service, sent = _service(tmp_path, {"subprocess": rt}, lambda: op.DENY_ALL)
    env = agent_pb2.EnvSpecRequest(base="python-cpu")
    service.handle(agent_pb2.PrepareEnv(request_id="r", envs=[env]))
    await _settle(service)
    assert rt.calls == 0
    assert {i.reason for i in sent.statuses[-1].items} == {"policy_not_accepting"}


@pytest.mark.asyncio
async def test_prepare_toggle_between_requests_follows_policy(tmp_path: Path) -> None:
    rt = _Rt()
    state = [OFF]
    service, _sent = _service(tmp_path, {"subprocess": rt}, lambda: state[0])
    env = agent_pb2.EnvSpecRequest(base="python-cpu")
    expected_calls = 0
    for i in range(12):
        state[0] = OPEN if i % 2 else OFF
        service.handle(agent_pb2.PrepareEnv(request_id=f"r{i}", envs=[env]))
        await _settle(service)
        if state[0] is OPEN:
            expected_calls += 1
        assert rt.calls == expected_calls
