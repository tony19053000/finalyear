"""What this agent offers for execution, probed once at start (brief D45/D51, V0.4 G4).

:func:`build_execution` turns ``[agent.execution]`` plus the runtime probes into an
:class:`ExecutionSetup`: the :class:`~.selection.ExecutionOffer` (whose
``capabilities`` go into ``Hello.hardware.capabilities``) and the runtime
instances the executor may use. Rules (ADR-0014):

* ``enabled = false`` -> nothing is probed, nothing is advertised, every
  ``StartAttempt`` is refused ``unauthorized``;
* the Docker runtime is probed (bounded: a wedged daemon cannot delay the first
  Hello past :data:`PROBE_GRACE_S` over ``docker_timeout_s``);
* the subprocess runtime exists only with ``allow_unsandboxed = true``;
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

from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.runtime import Runtime, RuntimeAvailability
from meshforge.agent.execution.selection import ExecutionOffer, execution_offer
from meshforge.agent.execution.subprocess_runtime import SubprocessRuntime
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
    """Offered **and available** runtimes by name (``docker``, ``subprocess``)."""
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
    if not cfg.enabled:
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
    docker = (docker_factory or DockerRuntime)(
        DockerConfig.from_settings(
            cfg, agent_label=node_id, host_has_nvidia_gpu=host_has_nvidia_gpu
        )
    )
    docker_found = await _probe(docker, cfg.docker_timeout_s + PROBE_GRACE_S)
    subprocess_rt: SubprocessRuntime | None = None
    subprocess_found: RuntimeAvailability | None = None
    if cfg.allow_unsandboxed:
        subprocess_rt = SubprocessRuntime(
            host_has_nvidia_gpu=host_has_nvidia_gpu, env_timeout_s=cfg.env_timeout_s
        )
        subprocess_found = await subprocess_rt.probe()
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=cfg.allow_unsandboxed,
        docker=docker_found,
        subprocess=subprocess_found,
    )
    runtimes: dict[str, Runtime] = {}
    if docker_found.available:
        runtimes[docker.name] = docker
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
        unsandboxed=cfg.allow_unsandboxed,
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
