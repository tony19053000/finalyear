# ruff: noqa: E501
# (adversarial literals / world-writable modes / seeded fuzz are the point of these tests)
"""V0.7 G5 tester round 1: the owner's caps in a REAL container, read back from
the cgroup and exercised by an over-allocating workload (64 MiB, short).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")

from mf_runtime_harness import Capture, containers_with_label, new_label

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.docker_support import open_client
from meshforge.agent.execution.plan import plan_attempt
from meshforge.agent.execution.runtime import RuntimeAvailability, Stream
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.settings import PYTHON_CPU_IMAGE, ExecutionSettings
from meshforge.common import owner_policy as op
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = [pytest.mark.docker, pytest.mark.timeout(120)]
MIB = 1024**2

PROBE = """
import json
out = {}
for name in ("memory.max", "memory.swap.max", "cpu.max", "pids.max"):
    try:
        out[name] = open("/sys/fs/cgroup/" + name).read().strip()
    except OSError as exc:
        out[name] = type(exc).__name__
print(json.dumps(out), flush=True)
"""
HOG = "x = bytearray(200 * 1024 * 1024)\nfor i in range(0, len(x), 4096):\n    x[i] = 1\nprint('survived')"


def _local() -> bool:
    client = open_client(10)
    try:
        client.images.get(PYTHON_CPU_IMAGE)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


def _plan(
    tmp_path: Path, code: str, manifest_extra: dict[str, Any], policy: op.OwnerPolicy
) -> Any:
    manifest = {"name": "x", "entrypoint": ["python", "-c", code], **manifest_extra}
    msg = agent_pb2.StartAttempt(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        spec=agent_pb2.ExecutionSpec(
            bundle_digest="sha256:" + "0" * 64,
            fetch_token="tok",
            manifest_json=json.dumps(manifest),
            timeout_s=60,
            owner_name="alice",
            cpus=float(manifest_extra.get("resources", {}).get("cpus", 0)),
        ),
    )
    plan = plan_attempt(
        msg,
        settings=ExecutionSettings(default_pids=32),
        offer=execution_offer(
            enabled=True,
            allow_unsandboxed=False,
            docker=RuntimeAvailability("docker", True, sandboxed=True),
            subprocess=None,
        ),
        declared_gpus=(),
        workdir=tmp_path,
        policy=policy,
        cpu_threads=8,
    )
    return plan


def _run(plan: Any) -> tuple[Any, Capture]:
    label = new_label()
    runtime = DockerRuntime(DockerConfig(agent_label=label))
    capture = Capture()
    try:
        result = asyncio.run(runtime.run(plan.spec, capture))
    finally:
        assert containers_with_label(label) == []
    return result, capture


@pytest.fixture(autouse=True)
def _need_image() -> None:
    if not _local():
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")


def test_the_stricter_of_owner_and_manifest_wins_and_swap_is_off(
    tmp_path: Path,
) -> None:
    policy = op.OwnerPolicy(
        accept_jobs=True, max_ram_bytes=512 * MIB, max_cpu_percent=50
    )
    # manifest asks 1 cpu: stricter than the owner's 4 (50% of 8); ram: owner wins
    plan = _plan(tmp_path, PROBE, {"resources": {"cpus": 1, "ram_gb": 0.25}}, policy)
    result, capture = _run(plan)
    assert result.ok, capture.data(Stream.STDERR)
    seen = json.loads(capture.data(Stream.STDOUT).decode().strip().splitlines()[-1])
    assert seen["cpu.max"].split() == ["100000", "100000"], seen
    assert int(seen["memory.max"]) <= 512 * MIB, seen
    assert seen["memory.swap.max"] == "0", seen  # no swap escape from the cap
    assert seen["pids.max"] == "32", seen


def test_a_job_over_the_owner_ram_cap_is_killed_not_swapped(tmp_path: Path) -> None:
    policy = op.OwnerPolicy(accept_jobs=True, max_ram_bytes=64 * MIB)
    plan = _plan(tmp_path, HOG, {}, policy)
    result, capture = _run(plan)
    assert not result.ok
    assert result.oom_killed or result.failure_type is FailureType.OOM, result
    assert b"survived" not in capture.data(Stream.STDOUT)
