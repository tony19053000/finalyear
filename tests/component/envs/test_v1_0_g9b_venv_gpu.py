"""V1.0 G9-B item 3: the venv runtime hands an NVIDIA GPU to a job.

The Windows laptops without Docker run GPU jobs on the venv runtime
(``env.base: python-cpu``, ``resources.gpu: required``, CUDA torch from the
owner's ``extra_index_urls``, D436). What MeshForge must guarantee there is
what the subprocess and Docker runtimes guarantee: the runtime is offered as
GPU-capable when hardware discovery saw an NVIDIA GPU, the planned GPU reaches
the workload as ``CUDA_VISIBLE_DEVICES`` (+ ``CUDA_DEVICE_ORDER=PCI_BUS_ID``,
NVML's numbering), and a job without a GPU gets an empty
``CUDA_VISIBLE_DEVICES``. Real uv venv, real child process; the GPU is
*injected* (``host_has_nvidia_gpu``, ``declared_gpus``) - this machine's own
GPU never decides the test. Whether CUDA then works inside the venv is the
lab's step (real driver, real torch wheel).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("uv", reason="needs the agent extra (the uv wheel)")

from mf_envs_support import offline_builder

from meshforge.agent.execution.plan import plan_attempt
from meshforge.agent.execution.runtime import RuntimeAvailability, Stream
from meshforge.agent.execution.selection import (
    EXEC_GPU_CAPABILITY,
    GpuNeed,
    choose_runtime,
    execution_offer,
)
from meshforge.agent.execution.venv_runtime import VenvRuntime
from meshforge.agent.settings import ExecutionSettings
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [pytest.mark.component, pytest.mark.timeout(240)]

PROBE = """\
import json, os, sys
print(json.dumps({
    "cuda": os.environ.get("CUDA_VISIBLE_DEVICES"),
    "order": os.environ.get("CUDA_DEVICE_ORDER"),
    "prefix": sys.prefix,
    "base": sys.base_prefix,
}))
"""


class Log:
    def __init__(self) -> None:
        self.out = bytearray()

    async def __call__(self, stream: Stream, data: bytes) -> None:
        if stream is Stream.STDOUT:
            self.out.extend(data)


def _start(gpu: str, gpu_indices: tuple[int, ...]) -> agent_pb2.StartAttempt:
    manifest = {
        "name": "g",
        "entrypoint": ["python", "probe.py"],
        "env": {"base": "python-cpu"},
        "resources": {"gpu": gpu},
    }
    return agent_pb2.StartAttempt(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        spec=agent_pb2.ExecutionSpec(
            bundle_digest="sha256:" + "0" * 64,
            fetch_token="tok",
            manifest_json=json.dumps(manifest),
            timeout_s=60,
            gpu_indices=list(gpu_indices),
        ),
    )


@pytest.mark.asyncio
async def test_a_venv_only_gpu_node_offers_exec_gpu(tmp_path: Path) -> None:
    builder = offline_builder(tmp_path / "envs", tmp_path)
    found = await VenvRuntime(envs=builder, host_has_nvidia_gpu=True).probe()
    assert found.available and found.gpu_capable
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=None, venv=found
    )
    assert EXEC_GPU_CAPABILITY in offer.capabilities
    assert choose_runtime(offer, GpuNeed.REQUIRED, "python-cpu") is found
    no_gpu = await VenvRuntime(envs=builder, host_has_nvidia_gpu=False).probe()
    assert not no_gpu.gpu_capable
    offer = execution_offer(
        enabled=True, allow_unsandboxed=True, docker=None, subprocess=None, venv=no_gpu
    )
    assert EXEC_GPU_CAPABILITY not in offer.capabilities
    assert choose_runtime(offer, GpuNeed.REQUIRED, "python-cpu") is None


async def _run(tmp_path: Path, gpu: str, indices: tuple[int, ...]) -> dict[str, str]:
    builder = offline_builder(tmp_path / "envs", tmp_path / "wheels")
    (tmp_path / "wheels").mkdir(exist_ok=True)
    runtime = VenvRuntime(envs=builder, host_has_nvidia_gpu=True)
    found = await runtime.probe()
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=None,
        subprocess=RuntimeAvailability("subprocess", True, gpu_capable=True),
        venv=found,
    )
    workdir = tmp_path / f"work-{gpu}"
    workdir.mkdir()
    (workdir / "probe.py").write_text(PROBE)
    plan = plan_attempt(
        _start(gpu, indices),
        settings=ExecutionSettings(),
        offer=offer,
        declared_gpus=(0, 1),
        workdir=workdir,
    )
    assert plan.runtime == "venv"  # docker > venv > subprocess
    log = Log()
    result = await runtime.run(plan.spec, log)
    assert result.ok, (result, bytes(log.out))
    line = bytes(log.out).decode().strip().splitlines()[-1]
    return dict(json.loads(line))


@pytest.mark.asyncio
async def test_the_planned_gpu_reaches_the_venv_workload(tmp_path: Path) -> None:
    seen = await _run(tmp_path, "required", (1,))
    assert seen["cuda"] == "1"
    assert seen["order"] == "PCI_BUS_ID"
    assert seen["prefix"] != seen["base"]  # the job ran in the venv


@pytest.mark.asyncio
async def test_a_cpu_job_on_the_venv_gets_no_gpu(tmp_path: Path) -> None:
    seen = await _run(tmp_path, "none", ())
    assert seen["cuda"] == ""
