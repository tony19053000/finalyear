"""V0.5 G5 (0.5.8): the agent's PrepareService with fake images and runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.envs.errors import EnvError
from meshforge.agent.envs.node_prepare import PrepareService
from meshforge.agent.envs.prepare import ImageReport, PrepareReport
from meshforge.agent.execution.offer import ExecutionSetup, execution_offer
from meshforge.agent.settings import AgentSettings
from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class FakeRuntime:
    def __init__(self) -> None:
        self.built: set[str] = set()
        self.calls = 0

    async def prepare_env(self, spec: Any, on_output: Any) -> tuple[str, str]:
        self.calls += 1
        data = (spec.workdir / "requirements.txt").read_bytes()
        key = f"{len(data):064d}"
        if key in self.built:
            return key, "hit"
        self.built.add(key)
        return key, "built"


class Sent:
    def __init__(self) -> None:
        self.statuses: list[agent_pb2.PrepareEnvStatus] = []

    async def __call__(self, name: str, payload: Any) -> str:
        assert name == "prepare_env_status"
        copy = agent_pb2.PrepareEnvStatus()
        copy.CopyFrom(payload)
        self.statuses.append(copy)
        return "m"


def _settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings.model_validate(
        {"data_dir": str(tmp_path), "agent": {"controller_address": "127.0.0.1:1"}}
    )


def _service(
    tmp_path: Path,
    runtimes: dict[str, Any] | None = None,
    report: PrepareReport | None = None,
) -> tuple[PrepareService, Sent]:
    setup = ExecutionSetup(
        offer=execution_offer(
            enabled=True, allow_unsandboxed=True, docker=None, subprocess=None
        ),
        runtimes=runtimes or {},
    )
    sent = Sent()

    async def images(bases: Sequence[str]) -> PrepareReport:
        return report or PrepareReport(docker="unavailable: DockerException", uv="uv")

    return (
        PrepareService(_settings(tmp_path), setup, sent, prepare_images=images),
        sent,
    )


async def _settle(service: PrepareService) -> None:
    for _ in range(200):
        if not service._tasks:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("prepare never finished")


def _env(**kw: Any) -> agent_pb2.EnvSpecRequest:
    return agent_pb2.EnvSpecRequest(base="python-cpu", **kw)


async def test_env_built_then_hit_and_a_replayed_request_runs_once(
    tmp_path: Path,
) -> None:
    runtime = FakeRuntime()
    service, sent = _service(tmp_path, {"subprocess": runtime})
    msg = agent_pb2.PrepareEnv(request_id="r1", envs=[_env(requirements=b"numpy==2\n")])
    service.handle(msg)
    await _settle(service)
    final = sent.statuses[-1]
    assert final.done and [i.state for i in final.items] == ["built"]
    assert sent.statuses[0].items[0].state == "pending"
    service.handle(msg)  # the same command replayed
    await _settle(service)
    assert runtime.calls == 1  # never ran twice
    assert sent.statuses[-1].done
    service.handle(
        agent_pb2.PrepareEnv(request_id="r2", envs=[_env(requirements=b"numpy==2\n")])
    )
    await _settle(service)
    assert [i.state for i in sent.statuses[-1].items] == ["hit"]
    assert not list((tmp_path / "prepare").iterdir())  # scratch removed


async def test_images_typed_states(tmp_path: Path) -> None:
    report = PrepareReport(
        docker="available",
        uv="uv",
        images=[
            ImageReport(
                "python-cpu", "img@sha256:" + "a" * 64, "present", size_bytes=5
            ),
            ImageReport(
                "cuda-pytorch",
                "img@sha256:" + "b" * 64,
                "failed",
                seconds=3.0,
                reason="timed out after 3 s",
            ),
        ],
    )
    service, sent = _service(tmp_path, report=report)
    service.handle(
        agent_pb2.PrepareEnv(request_id="r", base_images=["python-cpu", "cuda-pytorch"])
    )
    await _settle(service)
    items = {i.name: i for i in sent.statuses[-1].items}
    assert (
        items["python-cpu"].state == "hit"
        and items["python-cpu"].key == "sha256:" + "a" * 64
    )
    assert items["cuda-pytorch"].state == "failed"
    assert items["cuda-pytorch"].failure_type == FailureType.TIMEOUT.value
    assert items["cuda-pytorch"].reason == "pull_timeout"


async def test_unreachable_image_and_no_docker_are_typed(tmp_path: Path) -> None:
    unreachable = PrepareReport(
        docker="available",
        uv="uv",
        images=[
            ImageReport(
                "python-cpu", "img@sha256:" + "c" * 64, "failed", reason="APIError"
            )
        ],
    )
    service, sent = _service(tmp_path, report=unreachable)
    service.handle(agent_pb2.PrepareEnv(request_id="a", base_images=["python-cpu"]))
    await _settle(service)
    (item,) = sent.statuses[-1].items
    assert (item.failure_type, item.reason) == ("network_error", "pull_failed")
    service, sent = _service(tmp_path)  # Docker unavailable
    service.handle(agent_pb2.PrepareEnv(request_id="b", base_images=["python-cpu"]))
    await _settle(service)
    (item,) = sent.statuses[-1].items
    assert item.state == "failed" and item.reason == "docker_unavailable"


@pytest.mark.parametrize(
    "msg",
    [
        agent_pb2.PrepareEnv(request_id="x", base_images=["python-cpu"] * 9),
        agent_pb2.PrepareEnv(request_id="x", base_images=["windows"]),
        agent_pb2.PrepareEnv(request_id="x", envs=[_env()] * 33),
        agent_pb2.PrepareEnv(request_id="x", envs=[_env(runtime="kvm")]),
        agent_pb2.PrepareEnv(request_id="x", envs=[_env(python="2.7.1")]),
        agent_pb2.PrepareEnv(request_id="x", envs=[_env(requirements=b"a" * 65_537)]),
    ],
)
async def test_bounds_refuse_the_whole_request(
    tmp_path: Path, msg: agent_pb2.PrepareEnv
) -> None:
    runtime = FakeRuntime()
    service, sent = _service(tmp_path, {"subprocess": runtime})
    service.handle(msg)
    await _settle(service)
    final = sent.statuses[-1]
    assert final.done and all(i.reason == "invalid_request" for i in final.items)
    assert runtime.calls == 0


async def test_max_envs_and_max_requirements_are_accepted(tmp_path: Path) -> None:
    runtime = FakeRuntime()
    service, sent = _service(tmp_path, {"subprocess": runtime})
    service.handle(
        agent_pb2.PrepareEnv(
            request_id="m",
            base_images=["python-cpu"] * 8,
            envs=[_env(requirements=b"# " + b"x" * 65_532 + b"\n")] + [_env()] * 31,
        )
    )
    await _settle(service)
    final = sent.statuses[-1]
    assert final.done and len(final.items) == 1 + 32
    assert all(i.reason != "invalid_request" for i in final.items)


async def test_local_paths_and_missing_runtimes_fail_typed(tmp_path: Path) -> None:
    service, sent = _service(tmp_path, {"subprocess": FakeRuntime()})
    service.handle(
        agent_pb2.PrepareEnv(
            request_id="p",
            envs=[
                _env(requirements=b"-e ./mine\n"),
                _env(runtime="docker"),
                agent_pb2.EnvSpecRequest(base="cuda-pytorch", runtime="venv"),
            ],
        )
    )
    await _settle(service)
    items = sent.statuses[-1].items
    assert items[0].reason == "requirements_rejected"
    assert items[1].reason == "runtime_unavailable"
    assert items[1].failure_type == FailureType.NO_FEASIBLE_PLAN.value
    assert items[2].reason == "runtime_unavailable"  # venv is python-cpu only


async def test_a_failing_build_is_reported_with_its_reason(tmp_path: Path) -> None:
    class Broken(FakeRuntime):
        async def prepare_env(self, spec: Any, on_output: Any) -> tuple[str, str]:
            raise EnvError("pip said no", reason="setup_failed")

    service, sent = _service(tmp_path, {"subprocess": Broken()})
    service.handle(
        agent_pb2.PrepareEnv(request_id="f", envs=[_env(requirements=b"x\n")])
    )
    await _settle(service)
    (item,) = sent.statuses[-1].items
    assert (item.state, item.failure_type, item.reason) == (
        "failed",
        "env_error",
        "setup_failed",
    )
