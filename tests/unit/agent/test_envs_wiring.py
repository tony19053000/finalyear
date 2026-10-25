"""V0.5 G2 (D85-D87): how plans, selection and the runtimes use cached environments,
without a daemon or a real ``uv`` (those are in ``tests/component/envs``)."""

from __future__ import annotations

import io
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
from meshforge.agent.execution import docker_runtime as dr
from meshforge.agent.execution.docker_runtime import (
    CONTAINER_ENV,
    LABEL_ENV_HASH,
    DockerConfig,
    DockerRuntime,
)
from meshforge.agent.execution.plan import plan_attempt
from meshforge.agent.execution.runtime import (
    EnvRequest,
    RunLimits,
    RunResult,
    RunSpec,
    RuntimeAvailability,
    Stream,
)
from meshforge.agent.execution.selection import (
    EXEC_VENV_CAPABILITY,
    GpuNeed,
    choose_runtime,
    execution_offer,
)
from meshforge.agent.settings import PYTHON_CPU_IMAGE, ExecutionSettings
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

DOCKER = RuntimeAvailability("docker", True, sandboxed=True)
VENV = RuntimeAvailability("venv", True)
SUB = RuntimeAvailability("subprocess", True)
VENV_GPU = RuntimeAvailability("venv", True, gpu_capable=True)


# ---------------------------------------------------------------- selection
def test_order_is_docker_then_venv_then_subprocess() -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=DOCKER, subprocess=SUB, venv=VENV
    )
    assert [r.name for r in offer.offered()] == ["docker", "venv", "subprocess"]
    assert choose_runtime(offer, GpuNeed.NONE) is DOCKER
    no_docker = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB, venv=VENV
    )
    assert choose_runtime(no_docker, GpuNeed.NONE) is VENV


def test_venv_is_unsandboxed_so_it_needs_the_second_opt_in() -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=False, docker=None, subprocess=SUB, venv=VENV
    )
    assert offer.offered() == ()
    assert offer.capabilities == ()


def test_venv_capability_tag() -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB, venv=VENV_GPU
    )
    assert offer.capabilities == (
        "exec",
        "exec-gpu",
        "exec-subprocess",
        EXEC_VENV_CAPABILITY,
    )
    assert choose_runtime(offer, GpuNeed.REQUIRED) is VENV_GPU


def test_unavailable_venv_is_skipped() -> None:
    broken = RuntimeAvailability("venv", False, reason="uv_missing")
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB, venv=broken
    )
    assert choose_runtime(offer, GpuNeed.NONE) is SUB


# --------------------------------------------------------------------- plan
def _msg(manifest: dict[str, Any], runtime: str = "") -> agent_pb2.StartAttempt:
    return agent_pb2.StartAttempt(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        spec=agent_pb2.ExecutionSpec(
            bundle_digest="sha256:" + "0" * 64,
            fetch_token="tok",
            manifest_json=json.dumps(manifest),
            timeout_s=60,
            runtime=runtime,
        ),
    )


MANIFEST = {
    "name": "x",
    "entrypoint": ["python", "main.py"],
    "env": {"base": "python-cpu", "python": "3.12", "requirements": "req.txt"},
}


@pytest.mark.parametrize("runtime", ["docker", "venv", "subprocess"])
def test_every_plan_carries_the_env_request(tmp_path: Path, runtime: str) -> None:
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=DOCKER,
        subprocess=SUB,
        venv=VENV,
    )
    plan = plan_attempt(
        _msg(MANIFEST, runtime),
        settings=ExecutionSettings(),
        offer=offer,
        declared_gpus=(),
        workdir=tmp_path,
    )
    assert plan.runtime == runtime
    assert plan.spec.env_request == EnvRequest("python-cpu", "3.12", "req.txt")
    argv0 = plan.spec.argv[0]
    if runtime == "subprocess":
        assert argv0 == sys.executable  # the agent's own interpreter
    else:
        assert argv0 == "python"  # the image's / the venv's
    # the V0.4 per-job install stays planned only for runtimes without a builder
    assert (plan.spec.setup_argv is None) == (runtime == "venv")
    assert (plan.spec.image is not None) == (runtime == "docker")


def test_no_requirements_means_no_setup_but_an_env_request(tmp_path: Path) -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=None, venv=VENV
    )
    plan = plan_attempt(
        _msg({"name": "x", "entrypoint": ["python", "-V"]}),
        settings=ExecutionSettings(),
        offer=offer,
        declared_gpus=(),
        workdir=tmp_path,
    )
    assert plan.spec.env_request == EnvRequest("python-cpu", None, None)
    assert plan.spec.setup_argv is None


# --------------------------------------------------------------- RunResult
def test_run_result_env_cache_states() -> None:
    for state in ("hit", "waited", "built"):
        assert RunResult(exit_code=0, env_cache=state).env_cache == state
    with pytest.raises(ValueError, match="env_cache"):
        RunResult(exit_code=0, env_cache="warm")


# ------------------------------------------------------------ docker kwargs
def _fake_docker() -> Any:
    def record(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    return SimpleNamespace(
        types=SimpleNamespace(Mount=record, DeviceRequest=record, LogConfig=record)
    )


def _spec(tmp_path: Path, image: str = PYTHON_CPU_IMAGE) -> RunSpec:
    return RunSpec(
        attempt_id=new_attempt_id(),
        argv=("python", "main.py"),
        workdir=tmp_path,
        env={"PATH": "/usr/bin"},
        limits=RunLimits(memory_bytes=64 << 20, pids=64, timeout_s=10),
        image=image,
        setup_argv=("pip", "install", "-r", "req.txt"),
        env_request=EnvRequest(requirements="req.txt"),
    )


def _runtime(tmp_path: Path, **config: Any) -> DockerRuntime:
    builder = EnvBuilder(EnvsConfig(root=tmp_path / "envs", **config), uv_argv=("uv",))
    return DockerRuntime(
        DockerConfig(agent_label="node_x", run_as="1000:1000"), envs=builder
    )


def test_run_container_on_an_env_image_gets_env_on_pythonpath(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    kwargs = runtime._create_kwargs(
        _fake_docker(), _spec(tmp_path), "run", env_image=True
    )
    assert kwargs["environment"]["PYTHONPATH"] == CONTAINER_ENV
    assert kwargs["read_only"] is True  # /env is read-only to the job
    plain = runtime._create_kwargs(_fake_docker(), _spec(tmp_path), "run")
    assert plain["environment"]["PYTHONPATH"] == "/work/.meshforge/deps"


def test_envbuild_container_keeps_the_sandbox_hardening(tmp_path: Path) -> None:
    wheels = tmp_path / "wheels"
    runtime = _runtime(tmp_path, find_links=wheels, no_index=True)
    spec = _spec(tmp_path)
    staging = tmp_path / "staging"
    kwargs = runtime._envbuild_kwargs(
        _fake_docker(), spec, PYTHON_CPU_IMAGE, "ab" * 32, staging
    )
    assert kwargs["user"] == "1000:1000"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["network_mode"] == "none"  # wheelhouse only: no network at all
    assert kwargs["pids_limit"] == 64
    assert kwargs["memswap_limit"] == kwargs["mem_limit"] == 64 << 20
    assert kwargs["labels"][LABEL_ENV_HASH] == "ab" * 32
    assert kwargs["labels"]["meshforge.role"] == "envbuild"
    mounts = {m["target"]: m for m in kwargs["mounts"]}
    assert set(mounts) == {"/req", "/wheels"}  # never the job's workdir
    assert all(m["read_only"] for m in mounts.values())
    assert "--no-index" in kwargs["command"]
    assert kwargs["command"][kwargs["command"].index("--target") + 1] == CONTAINER_ENV
    # the committed image inherits this environment: nothing GPU- or job-specific
    assert set(kwargs["environment"]) == {"HOME", "PYTHONDONTWRITEBYTECODE"}


def test_envbuild_uses_the_network_with_an_index(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, index_url="https://mirror.example/simple")
    kwargs = runtime._envbuild_kwargs(
        _fake_docker(), _spec(tmp_path), PYTHON_CPU_IMAGE, "ab" * 32, tmp_path
    )
    assert kwargs["network_mode"] == "bridge"
    assert "https://mirror.example/simple" in kwargs["command"]


def test_env_dir_archive_is_one_dir_owned_by_the_container_user(tmp_path: Path) -> None:
    data = _runtime(tmp_path)._env_dir_archive()
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        (member,) = tar.getmembers()
    assert member.name == "env" and member.isdir()
    assert (member.uid, member.gid, member.mode) == (1000, 1000, 0o755)


class _Client:
    closed = False

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    "image",
    [
        "python:3.12-slim",
        "python:3.12-slim@sha256:abc",
        "python@sha512:" + "a" * 64,
    ],
)
@pytest.mark.asyncio
async def test_an_unpinned_base_is_refused_before_any_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image: str
) -> None:
    client = _Client()
    monkeypatch.setattr(dr, "open_client", lambda _t: client)
    runtime = DockerRuntime(
        DockerConfig(agent_label="node_x"),
        envs=EnvBuilder(EnvsConfig(root=tmp_path / "envs"), uv_argv=("uv",)),
    )
    work = tmp_path / "work"
    work.mkdir()
    out: list[bytes] = []

    async def sink(_s: Stream, data: bytes) -> None:
        out.append(data)

    result = await runtime.run(_spec(work, image), sink)
    assert result.failure_type is FailureType.CONFIG_ERROR
    assert result.reason == "unpinned_base"
    assert client.closed


def test_check_pinned_accepts_the_default_bases() -> None:
    from meshforge.agent.settings import CUDA_PYTORCH_IMAGE

    for image in (PYTHON_CPU_IMAGE, CUDA_PYTORCH_IMAGE):
        assert dr._check_pinned(image) == image.split("@", 1)[1]


# ------------------------------------------------------- agent start-up wiring
@pytest.mark.asyncio
@pytest.mark.extras("agent")
async def test_build_execution_gives_every_runtime_one_shared_builder(
    tmp_path: Path,
) -> None:
    from meshforge.agent.execution.offer import build_execution
    from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
    from meshforge.agent.execution.venv_runtime import VenvRuntime
    from meshforge.agent.settings import AgentSettings

    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "execution": {"enabled": True, "allow_unsandboxed": True},
        },
    )
    built: list[DockerRuntime] = []

    class NoDocker(DockerRuntime):
        async def probe(self) -> RuntimeAvailability:
            return RuntimeAvailability("docker", False, reason="docker_sdk_missing")

    def factory(config: DockerConfig) -> DockerRuntime:
        runtime = NoDocker(config)
        built.append(runtime)
        return runtime

    setup = await build_execution(
        settings, node_id="node_x", host_has_nvidia_gpu=False, docker_factory=factory
    )
    venv = setup.runtimes["venv"]
    sub = setup.runtimes["subprocess"]
    assert isinstance(venv, VenvRuntime) and isinstance(sub, SubprocessRuntime)
    envs = venv._envs
    assert envs is not None and sub._envs is envs
    assert envs.root == tmp_path / "data" / "envs"
    assert [r.name for r in setup.offer.offered()] == ["venv", "subprocess"]


# ------------------------------------------------- bases the venv can provide
def _offer_venv_sub() -> Any:
    return execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB, venv=VENV
    )


def test_cuda_pytorch_never_goes_to_the_venv_runtime() -> None:
    offer = _offer_venv_sub()
    assert choose_runtime(offer, GpuNeed.NONE, "python-cpu") is VENV
    assert choose_runtime(offer, GpuNeed.NONE, "cuda-pytorch") is SUB
    with_docker = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=DOCKER, subprocess=SUB, venv=VENV
    )
    assert choose_runtime(with_docker, GpuNeed.NONE, "cuda-pytorch") is DOCKER


def test_cuda_pytorch_plan_on_a_venv_and_subprocess_node(tmp_path: Path) -> None:
    manifest = {
        "name": "x",
        "entrypoint": ["python", "main.py"],
        "env": {"base": "cuda-pytorch"},
    }
    plan = plan_attempt(
        _msg(manifest),
        settings=ExecutionSettings(),
        offer=_offer_venv_sub(),
        declared_gpus=(),
        workdir=tmp_path,
    )
    assert plan.runtime == "subprocess"  # the agent's interpreter (torch), as in V0.4


def test_an_explicit_venv_request_for_cuda_pytorch_is_no_feasible_plan(
    tmp_path: Path,
) -> None:
    from meshforge.agent.execution.plan import PlanError

    manifest = {
        "name": "x",
        "entrypoint": ["python", "main.py"],
        "env": {"base": "cuda-pytorch"},
    }
    with pytest.raises(PlanError) as info:
        plan_attempt(
            _msg(manifest, "venv"),
            settings=ExecutionSettings(),
            offer=_offer_venv_sub(),
            declared_gpus=(),
            workdir=tmp_path,
        )
    assert info.value.failure_type is FailureType.NO_FEASIBLE_PLAN


def test_cuda_pytorch_with_only_venv_offered_is_no_feasible_plan(
    tmp_path: Path,
) -> None:
    from meshforge.agent.execution.plan import PlanError

    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=None, venv=VENV
    )
    manifest = {
        "name": "x",
        "entrypoint": ["python", "main.py"],
        "env": {"base": "cuda-pytorch"},
    }
    with pytest.raises(PlanError) as info:
        plan_attempt(
            _msg(manifest),
            settings=ExecutionSettings(),
            offer=offer,
            declared_gpus=(),
            workdir=tmp_path,
        )
    assert info.value.failure_type is FailureType.NO_FEASIBLE_PLAN


def test_env_image_changes_restore_the_base_and_blank_build_labels() -> None:
    changes = dr.env_image_changes(
        {"Cmd": ["python3"], "WorkingDir": "", "User": "", "Labels": {"x": "1"}}
    )
    assert changes[0] == (
        'LABEL meshforge.managed="" meshforge.agent="" meshforge.attempt_id="" '
        'meshforge.role=""'
    )
    assert "WORKDIR /" in changes and "USER root" in changes
    assert 'CMD ["python3"]' in changes
    assert not any("pip" in c for c in changes)
    torch = dr.env_image_changes({"Cmd": None, "WorkingDir": "/workspace", "User": "1"})
    assert "WORKDIR /workspace" in torch and "USER 1" in torch
    assert not any(c.startswith("CMD") for c in torch)
