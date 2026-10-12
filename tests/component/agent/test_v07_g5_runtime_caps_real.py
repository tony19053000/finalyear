"""V0.7 G5 part 2 (roadmap 0.7.8, D142 item 5): the owner's caps are real
runtime limits, measured inside real workloads.

* The **subprocess** runtime on Linux: an owner RAM cap is ``RLIMIT_AS`` (read
  back from ``/proc/self/limits``; a larger allocation fails with
  ``MemoryError``), ``pids`` is ``RLIMIT_NPROC`` (current tasks of the user +
  ``pids``; a fork bomb stops there), and without an owner cap there is no
  address-space limit.
* **Docker**: the plan's owner caps reach the container's cgroup
  (``memory.max``, ``cpu.max``, ``pids.max``).

Real processes and real containers; Docker cases skip without Docker or the
pinned image, the rlimit cases off Linux.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import resource
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
from mf_runtime_harness import Capture, Target, containers_with_label, new_label

from meshforge.agent.execution.plan import plan_attempt
from meshforge.agent.execution.runtime import RunLimits, RunResult, RunSpec, Stream
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
from meshforge.agent.settings import PYTHON_CPU_IMAGE, ExecutionSettings
from meshforge.common import owner_policy as op
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.timeout(120)

MIB = 1024**2
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="rlimits are the Linux mechanism"
)

LIMITS_PROBE = textwrap.dedent(
    """
    import json
    out = {}
    for line in open("/proc/self/limits"):
        for key, name in (("Max address space", "as"), ("Max processes", "nproc")):
            if line.startswith(key):
                out[name] = line[len(key):].split()[0]
    print(json.dumps(out), flush=True)
    """
)


def _sub_target() -> Target:
    return Target(SubprocessRuntime(), "subprocess", None, new_label(), sys.executable)


def _spec(
    target: Target,
    workdir: Path,
    code: str,
    *,
    memory_mb: int,
    pids: int,
    enforce: bool,
) -> RunSpec:
    spec = target.spec(workdir, code, memory_mb=memory_mb, pids=pids)
    return dataclasses.replace(
        spec,
        limits=RunLimits(
            memory_bytes=memory_mb * MIB,
            pids=pids,
            timeout_s=60,
            enforce_memory=enforce,
        ),
    )


def _run(target: Target, spec: RunSpec) -> tuple[RunResult, Capture]:
    capture = Capture()
    return asyncio.run(target.runtime.run(spec, capture)), capture


def _limits_seen(capture: Capture) -> dict[str, str]:
    seen: dict[str, str] = json.loads(
        capture.data(Stream.STDOUT).decode().strip().splitlines()[-1]
    )
    return seen


@linux_only
def test_an_owner_ram_cap_is_the_address_space_limit(tmp_path: Path) -> None:
    target = _sub_target()
    spec = _spec(target, tmp_path, LIMITS_PROBE, memory_mb=512, pids=64, enforce=True)
    result, capture = _run(target, spec)
    assert result.ok, capture.data(Stream.STDERR)
    seen = _limits_seen(capture)
    assert seen["as"] == str(512 * MIB)
    assert seen["nproc"] != "unlimited" and int(seen["nproc"]) >= 64


@linux_only
def test_allocating_past_the_owner_cap_fails_inside(tmp_path: Path) -> None:
    target = _sub_target()
    code = "b = bytearray(1024 * 1024 * 1024)\nprint('allocated', flush=True)"
    spec = _spec(target, tmp_path, code, memory_mb=256, pids=64, enforce=True)
    result, capture = _run(target, spec)
    assert result.failure_type is FailureType.USER_ERROR
    assert b"MemoryError" in capture.data(Stream.STDERR)
    assert b"allocated" not in capture.data(Stream.STDOUT)


@linux_only
def test_without_an_owner_cap_there_is_no_address_space_limit(tmp_path: Path) -> None:
    if resource.getrlimit(resource.RLIMIT_AS)[1] != resource.RLIM_INFINITY:
        pytest.skip("this test process already runs under an address-space limit")
    target = _sub_target()
    spec = _spec(target, tmp_path, LIMITS_PROBE, memory_mb=64, pids=64, enforce=False)
    result, capture = _run(target, spec)
    assert result.ok, capture.data(Stream.STDERR)
    assert _limits_seen(capture)["as"] == "unlimited"


FORK_BOMB = textwrap.dedent(
    """
    import os, time
    made = 0
    for _ in range(400):
        try:
            if os.fork() == 0:
                time.sleep(30)
                os._exit(0)
            made += 1
        except OSError:
            break
    print(made, flush=True)
    """
)


@linux_only
def test_the_task_limit_contains_a_fork_bomb(tmp_path: Path) -> None:
    if resource.getrlimit(resource.RLIMIT_NPROC)[1] != resource.RLIM_INFINITY and (
        resource.getrlimit(resource.RLIMIT_NPROC)[1] < 400
    ):
        pytest.skip("the user's own process limit is lower than the bomb")
    import os

    if os.geteuid() == 0:
        pytest.skip("RLIMIT_NPROC does not bind root (documented)")
    target = _sub_target()
    spec = _spec(target, tmp_path, FORK_BOMB, memory_mb=256, pids=16, enforce=False)
    result, capture = _run(target, spec)
    assert result.failure_type in (None, FailureType.USER_ERROR)
    made = int(capture.data(Stream.STDOUT).split()[0])
    # "at most 16 more tasks than the user had": other processes of this user
    # that end meanwhile give a little room back, never hundreds.
    assert made <= 32, made


# ------------------------------------------------------------------ Docker


def _image_local(image: str) -> bool:
    pytest.importorskip("docker", reason="needs the agent extra (docker SDK)")
    from meshforge.agent.execution.docker_support import open_client

    try:
        client = open_client(10)
    except Exception:  # noqa: BLE001
        return False
    try:
        client.images.get(image)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


CGROUP_PROBE = textwrap.dedent(
    """
    import json
    out = {}
    for name in ("memory.max", "cpu.max", "pids.max"):
        try:
            out[name] = open("/sys/fs/cgroup/" + name).read().strip()
        except OSError as exc:
            out[name] = type(exc).__name__
    print(json.dumps(out), flush=True)
    """
)


@pytest.mark.docker
def test_docker_gets_the_owner_caps_in_its_cgroup(tmp_path: Path) -> None:
    if not _image_local(PYTHON_CPU_IMAGE):
        pytest.skip(f"{PYTHON_CPU_IMAGE} is not local: docker pull it first")
    from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
    from meshforge.agent.execution.runtime import RuntimeAvailability

    policy = dataclasses.replace(
        op.OPEN_POLICY, max_ram_bytes=256 * MIB, max_cpu_percent=25
    )
    manifest = {"name": "x", "entrypoint": ["python", "-c", CGROUP_PROBE]}
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
        ),
    )
    plan: Any = plan_attempt(
        msg,
        settings=ExecutionSettings(default_pids=64),
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
    assert plan.runtime == "docker"
    label = new_label()
    runtime = DockerRuntime(DockerConfig(agent_label=label))
    capture = Capture()
    try:
        result = asyncio.run(runtime.run(plan.spec, capture))
    finally:
        assert containers_with_label(label) == []
    assert result.ok, capture.data(Stream.STDERR)
    seen = _limits_seen(capture)
    assert seen["memory.max"] == str(256 * MIB)
    assert seen["cpu.max"].split() == ["200000", "100000"]  # 2 of 8 threads
    assert seen["pids.max"] == "64"
