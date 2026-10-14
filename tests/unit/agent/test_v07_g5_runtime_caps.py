"""V0.7 G5 part 2 (roadmap 0.7.8, D142 item 5): the owner's caps become runtime
limits, and a cap no runtime can enforce on this OS is said, never claimed.

* :func:`not_enforced_caps` per OS (Linux, Windows, other) and per policy - and
  :class:`LivePolicy` recomputes it from every effective policy, so turning
  ``allow_unsandboxed`` off live drops the unsandboxed caveats;
* the plan marks an owner RAM cap ``enforce_memory`` (not for a GPU run);
* Docker ``--memory`` / ``--cpus`` come from policy ∩ manifest;
* the unsandboxed runtimes' process limits (Linux shim arguments, Windows
  CPU-rate arithmetic) and the fail-closed spawn when a required cap cannot be
  applied;
* ``GET /v1/nodes/{id}`` shows the live owner policy with ``not_enforced``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.plan import plan_attempt
from meshforge.agent.execution.runtime import RunLimits, RunSpec, RuntimeAvailability
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.execution.subprocess_runtime import run_limits
from meshforge.agent.policy import LivePolicy, not_enforced_caps, write_overlay
from meshforge.agent.process import (
    ProcessLimits,
    SpawnError,
    shim_limits,
    spawn,
    windows_cpu_rate,
)
from meshforge.agent.settings import ExecutionSettings, PolicySettings
from meshforge.common import owner_policy as op
from meshforge.common.ids import new_attempt_id, new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.api.shape import owner_policy_view

pytestmark = [pytest.mark.unit, pytest.mark.security]

GIB = 1024**3
MIB = 1024**2
DOCKER = RuntimeAvailability("docker", True, sandboxed=True)
SUB = RuntimeAvailability("subprocess", True, gpu_capable=True)
OPEN = op.OPEN_POLICY
VRAM = "max_vram_bytes:no_runtime_limit"


# ------------------------------------------------------------ not_enforced


@pytest.mark.parametrize(
    ("platform", "gpus", "unsandboxed", "expected"),
    [
        ("linux", 0, False, (VRAM,)),
        ("win32", 1, False, (VRAM,)),
        ("linux", 0, True, (VRAM, "max_cpu_percent:unsandboxed_linux")),
        (
            "linux",
            2,
            True,
            (
                VRAM,
                "max_cpu_percent:unsandboxed_linux",
                "max_ram_bytes:unsandboxed_gpu_jobs",
            ),
        ),
        ("win32", 0, True, (VRAM,)),
        ("win32", 1, True, (VRAM, "max_ram_bytes:unsandboxed_gpu_jobs")),
        (
            "darwin",
            0,
            True,
            (
                VRAM,
                "max_ram_bytes:unsandboxed_runtime",
                "max_cpu_percent:unsandboxed_runtime",
            ),
        ),
    ],
)
def test_not_enforced_caps_per_os(
    platform: str, gpus: int, unsandboxed: bool, expected: tuple[str, ...]
) -> None:
    policy = PolicySettings(accept_jobs=True, allow_unsandboxed=unsandboxed)
    caps = not_enforced_caps(policy, gpu_count=gpus, platform=platform)
    assert caps == expected
    # every entry is a valid wire value (the controller refuses malformed ones)
    op.validate(op.OwnerPolicy(not_enforced=caps))


def test_the_live_policy_recomputes_not_enforced(tmp_path: Path) -> None:
    import functools

    base = PolicySettings(accept_jobs=True, allow_unsandboxed=True)
    live = LivePolicy(
        base,
        tmp_path / "policy.toml",
        gpu_count=1,
        max_concurrent_attempts=4,
        not_enforced=functools.partial(
            not_enforced_caps, gpu_count=1, platform="linux"
        ),
    )
    assert "max_cpu_percent:unsandboxed_linux" in live.policy.not_enforced
    write_overlay(tmp_path / "policy.toml", {"allow_unsandboxed": False})
    assert live.reload() is True
    assert live.policy.not_enforced == (VRAM,)


# ------------------------------------------------------------ plan -> limits


def _msg(manifest: dict[str, Any], **spec: Any) -> Any:
    return agent_pb2.StartAttempt(
        job_id=str(new_job_id()),
        attempt_id=str(new_attempt_id()),
        fencing_token=1,
        spec=agent_pb2.ExecutionSpec(
            bundle_digest="sha256:" + "0" * 64,
            fetch_token="tok",
            manifest_json=json.dumps(manifest),
            timeout_s=60,
            owner_name="alice",
            **spec,
        ),
    )


BASE = {"name": "x", "entrypoint": ["python", "main.py"]}


def _plan(
    tmp_path: Path,
    policy: op.OwnerPolicy,
    manifest: dict[str, Any] = BASE,
    *,
    docker: bool = True,
    gpus: tuple[int, ...] = (),
    **spec: Any,
) -> Any:
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=DOCKER if docker else None,
        subprocess=SUB,
    )
    return plan_attempt(
        _msg(manifest, gpu_indices=list(gpus), **spec),
        settings=ExecutionSettings(),
        offer=offer,
        declared_gpus=gpus,
        workdir=tmp_path,
        policy=policy,
        cpu_threads=8,
    )


def test_an_owner_ram_cap_is_enforced_by_every_runtime(tmp_path: Path) -> None:
    capped = dataclasses.replace(OPEN, max_ram_bytes=GIB)
    plan = _plan(tmp_path, capped, docker=False)
    assert plan.runtime == "subprocess"
    assert plan.spec.limits.memory_bytes == GIB
    assert plan.spec.limits.enforce_memory is True
    limits = run_limits(plan.spec)
    assert limits == ProcessLimits(
        memory_bytes=GIB, tasks=plan.spec.limits.pids, cpus=None, required=True
    )


def test_no_owner_cap_means_no_address_space_limit(tmp_path: Path) -> None:
    plan = _plan(tmp_path, OPEN, docker=False)
    assert plan.spec.limits.enforce_memory is False
    limits = run_limits(plan.spec)
    # the default memory limit is not an address-space limit; pids always is
    assert limits.memory_bytes is None and limits.required is False
    assert limits.tasks == plan.spec.limits.pids


def test_a_gpu_run_is_not_given_an_address_space_limit(tmp_path: Path) -> None:
    capped = dataclasses.replace(OPEN, max_ram_bytes=GIB)
    manifest = {**BASE, "resources": {"gpu": "required"}}
    plan = _plan(tmp_path, capped, manifest, docker=False, gpus=(0,))
    assert plan.spec.gpu_indices == (0,)
    assert plan.spec.limits.memory_bytes == GIB  # still the Docker/report limit
    assert plan.spec.limits.enforce_memory is False


def _fake_docker() -> Any:
    def record(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    return SimpleNamespace(
        types=SimpleNamespace(Mount=record, DeviceRequest=record, LogConfig=record)
    )


def test_docker_memory_and_cpus_are_policy_and_manifest(tmp_path: Path) -> None:
    runtime = DockerRuntime(DockerConfig(agent_label="node_x", run_as="1000:1000"))
    policy = dataclasses.replace(OPEN, max_ram_bytes=GIB, max_cpu_percent=25)
    plan = _plan(tmp_path, policy)
    assert plan.runtime == "docker"
    kwargs = runtime._create_kwargs(_fake_docker(), plan.spec, "run")
    assert kwargs["mem_limit"] == GIB and kwargs["memswap_limit"] == GIB
    assert kwargs["nano_cpus"] == 2 * 10**9  # 25 % of 8 threads
    # the manifest asks less: the smaller ask wins
    small = _plan(
        tmp_path,
        policy,
        {**BASE, "resources": {"cpus": 1}},
        memory_bytes=512 * MIB,
        cpus=1.0,
    )
    kwargs = runtime._create_kwargs(_fake_docker(), small.spec, "run")
    assert kwargs["mem_limit"] == 512 * MIB and kwargs["nano_cpus"] == 10**9


# ------------------------------------------------------- process limits


def test_shim_limit_arguments() -> None:
    assert shim_limits(None) == "-"
    assert shim_limits(ProcessLimits()) == "-"
    assert shim_limits(ProcessLimits(tasks=64)) == "nproc=64"
    assert shim_limits(ProcessLimits(memory_bytes=GIB, tasks=8)) == (
        f"as={GIB};nproc=8"
    )


@pytest.mark.parametrize(
    ("cpus", "threads", "rate"),
    [
        (2.0, 8, 2500),
        (8.0, 8, 10_000),
        (16.0, 8, 10_000),
        (0.0001, 8, 1),
        (1, 0, 10_000),
    ],
)
def test_windows_cpu_rate(cpus: float, threads: int, rate: int) -> None:
    assert windows_cpu_rate(cpus, threads) == rate


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux shim rule")
def test_required_limits_without_the_shim_refuse_to_spawn() -> None:
    async def scenario() -> None:
        await spawn(
            [sys.executable, "-c", "pass"],
            env={"PATH": ""},
            die_with_parent=False,
            limits=ProcessLimits(memory_bytes=GIB, required=True),
        )

    with pytest.raises(SpawnError) as exc:
        asyncio.run(scenario())
    assert exc.value.reason == "limits_not_applied"


def test_enforce_memory_is_part_of_the_limits() -> None:
    spec = RunSpec(
        attempt_id=new_attempt_id(),
        argv=("python",),
        workdir=Path("/").resolve(),
        env={},
        limits=RunLimits(
            memory_bytes=64 * MIB, pids=16, timeout_s=1, cpus=0.5, enforce_memory=True
        ),
    )
    assert run_limits(spec) == ProcessLimits(
        memory_bytes=64 * MIB, tasks=16, cpus=0.5, required=True
    )


# ------------------------------------------------------- the controller's view


def test_the_node_view_shows_the_policy_and_counts_users() -> None:
    policy = dataclasses.replace(
        OPEN, allowed_users=("alice", "bob"), not_enforced=(VRAM,)
    )
    live: Any = SimpleNamespace(policy=policy, policy_refused=None)
    view = owner_policy_view(live)
    assert view is not None
    assert view["not_enforced"] == [VRAM]
    assert view["allowed_users_count"] == 2 and "allowed_users" not in view
    assert view["accept_jobs"] is True and view["refused"] is None
    assert owner_policy_view(None) is None
    assert owner_policy_view(SimpleNamespace(policy=None)) is None  # type: ignore[arg-type]
