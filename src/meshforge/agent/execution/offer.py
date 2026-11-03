"""What this agent offers for execution, probed once at start (brief D45/D51, V0.4 G4).

:func:`build_execution` turns ``[agent.execution]`` plus the runtime probes into an
:class:`ExecutionSetup`: the :class:`~.selection.ExecutionOffer` (whose
``capabilities`` go into ``Hello.hardware.capabilities``) and the runtime
instances the executor may use. Rules (ADR-0014; V1.0 G8 F5, D428):

* **What the agent CAN run is separate from what its owner ALLOWS** (V1.0 G8
  F5, D428). The ``exec*`` tags say which runtimes were found; whether a job
  may run is the owner's **live** policy (``accept_jobs``, ``allow_unsandboxed``,
  limits, slots), re-checked by the executor at every ``StartAttempt``
  (:func:`.plan.plan_attempt`) and by the controller's planner.
* **Look only, act later** (team decision 2026-11-03, D428). With ``accept_jobs``
  off at start the setup is built **look-only** (``side_effects=False``,
  :attr:`ExecutionSetup.complete` False): read-only checks only - the Docker
  API (ping, version, info, local images; GPU pass-through only *assumed* from
  the registered runtimes, never run), ``uv --version``, the host's NVIDIA GPUs
  from discovery. **No container is started** (no GPU-probe container) and **no
  cgroup is written** (the CPU-share cgroup probe, which may enable the ``cpu``
  controller, and its leftover sweep are deferred: the CPU cap is advertised
  not enforced meanwhile). The executor refuses every attempt on a look-only
  setup (retryable). When the owner turns ``accept_jobs`` on the first time,
  :mod:`.activation` builds the full setup once (``side_effects=True``), swaps
  it in and re-advertises the capabilities - no restart.
* Docker is probed bounded: a wedged daemon cannot delay the first Hello past
  :data:`PROBE_GRACE_S` over ``docker_timeout_s``.
* with the unsandboxed runtimes, the CPU-share cgroup is probed
  (:mod:`.cpu_cgroup`, V0.7 G6) - full setups only: when the agent runs
  delegated, every job of theirs gets a real ``cpu.max``; else the cap is
  advertised not enforced;
* one :class:`~meshforge.agent.envs.builder.EnvBuilder` (``[agent.envs]``) is shared
  by every runtime: requirements are installed once per env hash (V0.5 D86);
* a full setup (jobs allowed at start, or turned on later) + the controller
  found by **mDNS** -> ``agent.execution.unpinned_controller`` (warning): a
  controller on the LAN is trusted only through the fabric CA.

The Docker containers carry ``meshforge.agent=<node_id>`` (D63-6): two agents on
one host never sweep each other's containers.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
from meshforge.agent.execution.cpu_cgroup import (
    CpuCgroupProbe,
    CpuCgroups,
    probe_cpu_cgroups,
)
from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.runtime import Runtime, RuntimeAvailability
from meshforge.agent.execution.selection import ExecutionOffer, execution_offer
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
from meshforge.agent.execution.venv_runtime import VenvRuntime
from meshforge.agent.policy import startup_policy
from meshforge.agent.settings import AgentSettings
from meshforge.common.logging import get_logger

__all__ = ["PROBE_GRACE_S", "ExecutionSetup", "build_execution", "warn_unpinned"]

log = get_logger(__name__)

PROBE_GRACE_S: Final = 5.0
"""Slack over ``docker_timeout_s`` for the whole Docker probe (it makes several
API calls and may start one GPU-probe container)."""


@dataclass(frozen=True, slots=True)
class ExecutionSetup:
    """The probed offer plus the runtime instances behind it."""

    offer: ExecutionOffer
    runtimes: Mapping[str, Runtime] = field(default_factory=dict)
    """Offered **and available** runtimes by name (``docker``, ``venv``, ``subprocess``)."""
    docker: DockerRuntime | None = None
    """The Docker runtime when available (for the start-up orphan sweep)."""
    cpu_cgroups: CpuCgroups | None = None
    """V0.7 G6: per-job CPU-share cgroups for the unsandboxed Linux runtimes
    (None = not available here; ``max_cpu_percent`` stays not enforced)."""
    complete: bool = True
    """V1.0 G8 F5 (D428): the side-effecting probes ran (GPU-probe container,
    cgroup write). False = a look-only setup (jobs were off at start): no job
    ever runs on it; :mod:`.activation` replaces it on the first enable."""

    @property
    def enabled(self) -> bool:
        return self.offer.enabled


def disabled_setup() -> ExecutionSetup:
    return ExecutionSetup(
        offer=execution_offer(
            enabled=False, allow_unsandboxed=False, docker=None, subprocess=None
        )
    )


async def build_execution(
    settings: AgentSettings,
    *,
    node_id: str,
    host_has_nvidia_gpu: bool,
    docker_factory: Callable[[DockerConfig], DockerRuntime] | None = None,
    cgroup_probe: Callable[[], CpuCgroupProbe] = probe_cpu_cgroups,
    side_effects: bool | None = None,
) -> ExecutionSetup:
    """Probe and build the execution setup (see module docstring). Never raises.
    ``side_effects``: build a full setup (True) or a look-only one (False);
    None = full exactly when the effective policy at start accepts jobs."""
    cfg = settings.agent.execution
    policy = startup_policy(settings)
    full = policy.accept_jobs if side_effects is None else side_effects
    if full:
        warn_unpinned(settings, node_id)
    envs = EnvBuilder(EnvsConfig.from_settings(settings))  # V0.5 D86: one per agent
    docker_config = DockerConfig.from_settings(
        cfg, agent_label=node_id, host_has_nvidia_gpu=host_has_nvidia_gpu
    )
    if not full:
        # look only: without a probe image the GPU answer is read from the
        # daemon's registered runtimes - no container is created or started
        docker_config = dataclasses.replace(docker_config, gpu_probe_image=None)
    docker = (
        docker_factory(docker_config)
        if docker_factory is not None
        else DockerRuntime(docker_config, envs=envs)
    )
    docker_found = await _probe(docker, cfg.docker_timeout_s + PROBE_GRACE_S)
    # V1.0 G8 F5 (D428): built whatever allow_unsandboxed says now - the live
    # policy decides per attempt (plan_attempt skips them when not allowed)
    cgroups = (
        await asyncio.to_thread(_cpu_cgroups, node_id, cgroup_probe)
        if full
        else None  # look only: the probe may write subtree_control, the sweep rmdirs
    )
    subprocess_rt = SubprocessRuntime(
        host_has_nvidia_gpu=host_has_nvidia_gpu,
        env_timeout_s=cfg.env_timeout_s,
        envs=envs,
        cpu_cgroups=cgroups,
    )
    subprocess_found = await subprocess_rt.probe()
    venv_rt = VenvRuntime(
        envs=envs,
        host_has_nvidia_gpu=host_has_nvidia_gpu,
        env_timeout_s=cfg.env_timeout_s,
        cpu_cgroups=cgroups,
    )
    venv_found = await venv_rt.probe()
    found = docker_found.available or venv_found.available or subprocess_found.available
    offer = execution_offer(
        enabled=found,  # reviewer F5: no runtime at all = not enabled
        allow_unsandboxed=True,
        docker=docker_found,
        subprocess=subprocess_found,
        venv=venv_found,
    )
    runtimes: dict[str, Runtime] = {}
    if docker_found.available:
        runtimes[docker.name] = docker
    if venv_found.available:
        runtimes[venv_rt.name] = venv_rt
    if subprocess_found.available:
        runtimes[subprocess_rt.name] = subprocess_rt
    log.info(
        "agent.execution.ready" if runtimes else "agent.execution.no_runtime",
        node_id=node_id,
        capabilities=list(offer.capabilities),
        docker=docker_found.available,
        docker_reason=docker_found.reason or None,
        docker_gpu=docker_found.gpu_reason or None,
        accept_jobs=policy.accept_jobs,
        allow_unsandboxed=policy.allow_unsandboxed,
        complete=full,
    )
    return ExecutionSetup(
        offer=offer,
        runtimes=runtimes,
        # look only: no Docker handle for the orphan sweep, GC or cache index
        docker=docker if docker_found.available and full else None,
        cpu_cgroups=cgroups,
        complete=full,
    )


def warn_unpinned(settings: AgentSettings, node_id: str) -> None:
    """Jobs may run now and the controller is found by mDNS: say so (at start
    with jobs on, and again when jobs are turned on later - reviewer F5)."""
    if settings.agent.uses_discovery:
        log.warning(
            "agent.execution.unpinned_controller",
            node_id=node_id,
            detail=(
                "jobs may run here and the controller is found by mDNS: any "
                "controller this LAN answers for is trusted only through the "
                "fabric CA - set [agent] controller_address to pin it"
            ),
        )


def _cpu_cgroups(
    node_id: str, probe: Callable[[], CpuCgroupProbe]
) -> CpuCgroups | None:
    """V0.7 G6: the delegated cgroup for job CPU shares, or None (logged with
    the typed reason; the cap is then advertised ``not_enforced``)."""
    found = probe()
    if found.parent is None:
        log.info(
            "agent.execution.cpu_cgroup_unavailable",
            node_id=node_id,
            reason=found.reason,
        )
        return None
    cgroups = CpuCgroups(found.parent)
    cgroups.sweep()  # job cgroups a crashed agent left behind
    log.info("agent.execution.cpu_cgroup_ready", node_id=node_id)
    return cgroups


async def _probe(runtime: DockerRuntime, bound_s: float) -> RuntimeAvailability:
    try:
        return await asyncio.wait_for(runtime.probe(), bound_s)
    except TimeoutError:
        # The probe thread may still be blocked in the SDK; it is abandoned (it
        # holds no lock and returns into nothing). The node advertises no Docker.
        return RuntimeAvailability(
            runtime.name, False, reason="docker_probe_timeout", sandboxed=True
        )
    except Exception as exc:  # noqa: BLE001 - "never raises": a bug here is "no docker"
        return RuntimeAvailability(
            runtime.name,
            False,
            reason=f"docker_probe_failed: {type(exc).__name__}",
            sandboxed=True,
        )
