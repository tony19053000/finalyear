"""V1.0 G9-B item 2 (D432): PyTorch in the agent's interpreter is a probed
capability (``exec-torch``).

* the subprocess runtime's probe asks an injected ``find_spec``-style probe -
  this machine's own torch never decides a test; a failing probe is "no torch";
* ``exec-torch`` is advertised only with the subprocess runtime;
* the agent's defence: a ``cuda-pytorch`` job whose only fitting runtime is a
  torch-less subprocess runtime fails ``env_error`` ``torch_missing`` (never
  ``user_error`` from a ``ModuleNotFoundError``); Docker still takes it;
* the venv GPU path (``python-cpu`` + ``gpu: required`` + torch in the job's
  requirements) is never blocked by D432.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.agent.execution.plan import TORCH_MISSING_REASON, PlanError, plan_attempt
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.execution.selection import (
    GpuNeed,
    choose_runtime,
    execution_offer,
    torch_missing,
)
from meshforge.agent.execution.subprocess_runtime import (
    SubprocessRuntime,
    interpreter_has_torch,
)
from meshforge.agent.settings import ExecutionSettings
from meshforge.common.capabilities import EXEC_TORCH_CAPABILITY
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [pytest.mark.unit]

DOCKER = RuntimeAvailability("docker", True, sandboxed=True)
SUB = RuntimeAvailability("subprocess", True)
SUB_TORCH = RuntimeAvailability("subprocess", True, torch=True)
SUB_GPU = RuntimeAvailability("subprocess", True, gpu_capable=True)
VENV_GPU = RuntimeAvailability("venv", True, gpu_capable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("has", [True, False])
async def test_the_probe_reports_what_the_injected_check_says(has: bool) -> None:
    calls: list[int] = []

    def probe() -> bool:
        calls.append(1)
        return has

    found = await SubprocessRuntime(torch_probe=probe).probe()
    assert found.torch is has and calls == [1]


@pytest.mark.asyncio
async def test_a_probe_that_raises_is_no_torch() -> None:
    def broken() -> bool:
        raise RuntimeError("boom")

    found = await SubprocessRuntime(torch_probe=broken).probe()
    assert found.available and found.torch is False


def test_interpreter_has_torch_uses_find_spec_and_never_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib.util

    seen: list[str] = []
    real_spec = importlib.util.spec_from_loader("torch", loader=None, origin="/x.py")
    namespace = importlib.util.spec_from_loader("torch", loader=None)  # origin None

    def fake(name: str, *_a: Any) -> Any:
        seen.append(name)
        return answers.pop(0)

    answers: list[Any] = [real_spec, None, namespace]
    monkeypatch.setattr(importlib.util, "find_spec", fake)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    assert interpreter_has_torch() is True
    assert interpreter_has_torch() is False  # not installed
    assert interpreter_has_torch() is False  # a leftover namespace dir
    assert seen == ["torch", "torch", "torch"]
    assert "torch" not in sys.modules  # never imported

    def raising(name: str, *_a: Any) -> Any:
        raise ValueError("torch.__spec__ is None")

    monkeypatch.setattr(importlib.util, "find_spec", raising)
    assert interpreter_has_torch() is False


def test_exec_torch_only_with_the_subprocess_runtime() -> None:
    with_torch = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=DOCKER, subprocess=SUB_TORCH
    )
    assert EXEC_TORCH_CAPABILITY in with_torch.capabilities
    without = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=DOCKER, subprocess=SUB
    )
    assert EXEC_TORCH_CAPABILITY not in without.capabilities
    # the owner's old opt-out shape (subprocess not offered): no tag either
    off = execution_offer(
        enabled=True, allow_unsandboxed=False, docker=DOCKER, subprocess=SUB_TORCH
    )
    assert EXEC_TORCH_CAPABILITY not in off.capabilities
    # a venv "with torch" is not a thing the tag speaks about
    venv_only = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=None,
        subprocess=None,
        venv=RuntimeAvailability("venv", True, torch=True),
    )
    assert EXEC_TORCH_CAPABILITY not in venv_only.capabilities


def _msg(manifest: dict[str, Any]) -> agent_pb2.StartAttempt:
    return agent_pb2.StartAttempt(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        spec=agent_pb2.ExecutionSpec(
            bundle_digest="sha256:" + "0" * 64,
            fetch_token="tok",
            manifest_json=json.dumps(manifest),
            timeout_s=60,
        ),
    )


TORCH_JOB = {
    "name": "t",
    "entrypoint": ["python", "t.py"],
    "env": {"base": "cuda-pytorch"},
}


def _plan(offer: Any, tmp_path: Path, manifest: dict[str, Any] = TORCH_JOB) -> Any:
    return plan_attempt(
        _msg(manifest),
        settings=ExecutionSettings(),
        offer=offer,
        declared_gpus=(),
        workdir=tmp_path,
    )


def test_cuda_pytorch_on_a_torchless_subprocess_node_is_env_error(
    tmp_path: Path,
) -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB, venv=VENV_GPU
    )
    with pytest.raises(PlanError) as info:
        _plan(offer, tmp_path)
    err = info.value
    assert err.failure_type is FailureType.ENV_ERROR
    assert err.failure_type is not FailureType.USER_ERROR
    assert err.details == {"reason": TORCH_MISSING_REASON}
    assert err.message.startswith("torch_missing: ")
    assert "install Docker, or PyTorch for the agent" in err.message
    assert "python-cpu" in err.message  # names the venv recipe
    assert err.retryable is False


def test_gpu_required_cuda_pytorch_on_a_torchless_gpu_subprocess_is_env_error() -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB_GPU
    )
    assert choose_runtime(offer, GpuNeed.REQUIRED, "cuda-pytorch") is None
    assert torch_missing(offer, GpuNeed.REQUIRED, "cuda-pytorch")
    # a GPU job the subprocess runtime could not give a GPU anyway is not
    # "torch missing" - it is no runtime at all
    no_gpu = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB
    )
    assert not torch_missing(no_gpu, GpuNeed.REQUIRED, "cuda-pytorch")
    assert not torch_missing(offer, GpuNeed.NONE, "python-cpu")


def test_docker_still_takes_cuda_pytorch_and_torch_subprocess_still_does(
    tmp_path: Path,
) -> None:
    both = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=DOCKER, subprocess=SUB
    )
    assert choose_runtime(both, GpuNeed.NONE, "cuda-pytorch") is DOCKER
    torch_sub = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB_TORCH
    )
    assert _plan(torch_sub, tmp_path).runtime == "subprocess"


def test_an_explicitly_requested_torchless_subprocess_is_env_error(
    tmp_path: Path,
) -> None:
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB
    )
    msg = _msg(TORCH_JOB)
    msg.spec.runtime = "subprocess"
    with pytest.raises(PlanError) as info:
        plan_attempt(
            msg,
            settings=ExecutionSettings(),
            offer=offer,
            declared_gpus=(),
            workdir=tmp_path,
        )
    assert info.value.failure_type is FailureType.ENV_ERROR


def test_the_venv_gpu_recipe_is_not_blocked(tmp_path: Path) -> None:
    """The Windows-without-Docker GPU path: python-cpu, gpu required, CUDA torch
    from the job's requirements - the venv runtime, whatever torch the agent's
    own Python has."""
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=SUB, venv=VENV_GPU
    )
    assert choose_runtime(offer, GpuNeed.REQUIRED, "python-cpu") is VENV_GPU
    plan = plan_attempt(
        _msg(
            {
                "name": "g",
                "entrypoint": ["python", "train.py"],
                "env": {"base": "python-cpu", "requirements": "requirements.txt"},
                "resources": {"gpu": "required"},
            }
        ),
        settings=ExecutionSettings(),
        offer=offer,
        declared_gpus=(0,),
        workdir=tmp_path,
    )
    assert plan.runtime == "venv"
