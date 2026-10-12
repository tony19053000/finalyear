"""What this agent offers for execution, probed once at start (brief D45/D51, V0.4 G4).

:func:`build_execution` turns ``[agent.execution]`` plus the runtime probes into an
:class:`ExecutionSetup`: the :class:`~.selection.ExecutionOffer` (whose
``capabilities`` go into ``Hello.hardware.capabilities``) and the runtime
instances the executor may use. Rules (ADR-0014):

* the policy read at start is the **effective** one: ``[agent.policy]`` plus
  the owner's live ``<data_dir>/policy.toml`` (``policy.startup_policy``; V0.7
  G5 fix r1, T-8), so ``policy set ...`` then a restart really probes what the
  owner turned on;
* ``accept_jobs = false`` (was ``[agent.execution] enabled``, V0.7 G5) at
  start -> nothing is probed, nothing is advertised, every ``StartAttempt`` is
  refused ``unauthorized``;
* the Docker runtime is probed (bounded: a wedged daemon cannot delay the first
  Hello past :data:`PROBE_GRACE_S` over ``docker_timeout_s``);
* the subprocess and venv runtimes exist only with ``[agent.policy]
  allow_unsandboxed = true`` at start (turning it on live needs a restart;
  turning it off live is honoured per attempt by the policy check);
* one :class:`~meshforge.agent.envs.builder.EnvBuilder` (``[agent.envs]``) is shared
  by every runtime: requirements are installed once per env hash (V0.5 D86);
* enabled + the controller found by **mDNS** -> ``agent.execution.unpinned_controller``
  (warning): until V0.7 mTLS, a rogue controller on the LAN could send work.

The Docker containers carry ``meshforge.agent=<node_id>`` (D63-6): two agents on
one host never sweep each other's containers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from meshforge.agent.envs.builder import EnvBuilder, EnvsConfig
from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.runtime import Runtime, RuntimeAvailability
from meshforge.agent.execution.selection import ExecutionOffer, execution_offer
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
from meshforge.agent.execution.venv_runtime import VenvRuntime
from meshforge.agent.policy import startup_policy
from meshforge.agent.settings import AgentSettings
from meshforge.common.logging import get_logger

__all__ = ["PROBE_GRACE_S", "ExecutionSetup", "build_execution"]

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
) -> ExecutionSetup:
    """Probe and build the execution setup (see module docstring). Never raises."""
    cfg = settings.agent.execution
    policy = startup_policy(settings)
    if not policy.accept_jobs:
        log.info("agent.execution.disabled", node_id=node_id)
        return disabled_setup()
    if settings.agent.uses_discovery:
        log.warning(
            "agent.execution.unpinned_controller",
            node_id=node_id,
            detail=(
                "execution is enabled and the controller is found by mDNS: any "
                "controller on this LAN could send work until V0.7 mTLS - set "
                "[agent] controller_address to pin it"
            ),
        )
    envs = EnvBuilder(EnvsConfig.from_settings(settings))  # V0.5 D86: one per agent
    docker_config = DockerConfig.from_settings(
        cfg, agent_label=node_id, host_has_nvidia_gpu=host_has_nvidia_gpu
    )
    docker = (
        docker_factory(docker_config)
        if docker_factory is not None
        else DockerRuntime(docker_config, envs=envs)
    )
    docker_found = await _probe(docker, cfg.docker_timeout_s + PROBE_GRACE_S)
    subprocess_rt: SubprocessRuntime | None = None
    subprocess_found: RuntimeAvailability | None = None
    venv_rt: VenvRuntime | None = None
    venv_found: RuntimeAvailability | None = None
    if policy.allow_unsandboxed:
        subprocess_rt = SubprocessRuntime(
            host_has_nvidia_gpu=host_has_nvidia_gpu,
            env_timeout_s=cfg.env_timeout_s,
            envs=envs,
        )
        subprocess_found = await subprocess_rt.probe()
        venv_rt = VenvRuntime(
            envs=envs,
            host_has_nvidia_gpu=host_has_nvidia_gpu,
            env_timeout_s=cfg.env_timeout_s,
        )
        venv_found = await venv_rt.probe()
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=policy.allow_unsandboxed,
        docker=docker_found,
        subprocess=subprocess_found,
        venv=venv_found,
    )
    runtimes: dict[str, Runtime] = {}
    if docker_found.available:
        runtimes[docker.name] = docker
    if venv_rt is not None and venv_found is not None and venv_found.available:
        runtimes[venv_rt.name] = venv_rt
    if (
        subprocess_rt is not None
        and subprocess_found is not None
        and subprocess_found.available
    ):
        runtimes[subprocess_rt.name] = subprocess_rt
    log.info(
        "agent.execution.ready" if runtimes else "agent.execution.no_runtime",
        node_id=node_id,
        capabilities=list(offer.capabilities),
        docker=docker_found.available,
        docker_reason=docker_found.reason or None,
        docker_gpu=docker_found.gpu_reason or None,
        unsandboxed=policy.allow_unsandboxed,
    )
    return ExecutionSetup(
        offer=offer,
        runtimes=runtimes,
        docker=docker if docker_found.available else None,
    )


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
