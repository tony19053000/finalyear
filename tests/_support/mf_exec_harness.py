"""V0.4 G4 end-to-end helpers: a real controller, a real agent with its executor.

Imported as ``mf_exec_harness`` by ``tests/component/agent/test_exec_*.py``.

* :class:`ExecAgent` - the real :class:`AgentSessionClient` wired to the real
  :class:`Executor` exactly as ``agent/runtime.serve`` wires them, running in the
  test's event loop. By default the offer is **subprocess only** (a Docker probe
  that reports "unavailable"), so the tier needs no Docker; ``docker=True`` probes
  the real daemon. The V0.5 venv runtime (preferred over subprocess when offered)
  is dropped unless ``venv=True``, so V0.4 tests keep exercising the subprocess
  runtime; ``agent_overrides`` adds ``[agent]`` sections (e.g. ``envs``).
* :func:`submit` packs a project folder, uploads the bundle and posts the job
  through the REST API with the fabric token - the product surface, nothing else.
* :func:`wait_job` polls ``GET /v1/jobs/{id}`` on the monotonic clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
from mf_agent_harness import make_agent_settings

from meshforge.agent.cache import CacheReporter
from meshforge.agent.cas import AgentCas
from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import ExecutionSetup, build_execution
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.gc.service import GcService
from meshforge.agent.hardware.limits import clip_capabilities
from meshforge.agent.identity import load_or_create
from meshforge.agent.peer import PeerBlobServer
from meshforge.agent.runtime import (
    controller_host,
    route_commands,
    start_cache,
    start_gc,
    start_peer,
    wire_executor,
)
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.hello import basic_hardware
from meshforge.agent.session.state import AgentState
from meshforge.common.bundle import pack_to_file

__all__ = ["TERMINAL", "ExecAgent", "no_docker", "submit", "wait_job"]

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class _NoDocker(DockerRuntime):
    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("docker", False, reason="disabled_in_test")


def no_docker(config: DockerConfig) -> DockerRuntime:
    return _NoDocker(config)


class ExecAgent:
    """A running agent process-equivalent (client + executor) in this loop."""

    def __init__(
        self, client: AgentSessionClient, executor: Executor, setup: ExecutionSetup
    ) -> None:
        self.client = client
        self.executor = executor
        self.setup = setup
        self.node_id = client.node_id
        self.peer: PeerBlobServer | None = None
        self.cache: CacheReporter | None = None
        self.gc: GcService | None = None
        self.task: asyncio.Task[None] = asyncio.create_task(client.run())

    @classmethod
    async def start(
        cls,
        controller_address: str,
        data_dir: Path,
        *,
        execution: Mapping[str, Any] | None = None,
        docker: bool = False,
        venv: bool = False,
        setup: ExecutionSetup | None = None,
        agent_overrides: Mapping[str, Any] | None = None,
    ) -> ExecAgent:
        exec_cfg = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}
        exec_cfg.update(execution or {})
        settings = make_agent_settings(
            data_dir, controller_address, execution=exec_cfg, **(agent_overrides or {})
        )
        identity = load_or_create(settings.identity_dir)
        hardware = basic_hardware()
        if setup is None:
            setup = await build_execution(
                settings,
                node_id=str(identity.node_id),
                host_has_nvidia_gpu=False,
                docker_factory=None if docker else no_docker,
            )
            if not venv:
                setup = dataclasses.replace(
                    setup,
                    offer=dataclasses.replace(setup.offer, venv=None),
                    runtimes={k: v for k, v in setup.runtimes.items() if k != "venv"},
                )
        hardware.capabilities[:] = clip_capabilities(
            [*hardware.capabilities, *setup.offer.capabilities]
        )
        client = AgentSessionClient(settings, identity, hardware=hardware)
        cas = AgentCas.at(settings.data_dir)
        # V0.5 G4: the peer server starts only when [agent.peer] bind is set
        # (agent_overrides={"peer": {"bind": "127.0.0.1", "port": 0}}).
        peer = await start_peer(settings, cas, client)
        cache = await start_cache(settings, cas, client)
        # V0.5 G5: GC as in serve() ([agent.gc] via agent_overrides={"gc": {...}}).
        gc = await start_gc(settings, cas, node_id=client.node_id, cache=cache)
        executor = Executor(
            settings,
            setup,
            cas=cas,
            node_id=client.node_id,
            report=client.send_report,
            resend=client.resend_report,
            log_sink=client.send_log,
            controller_host=lambda: controller_host(client.controller_target),
            gc=gc,
        )
        wire_executor(client, executor)
        client.command_handler = route_commands(executor, gc=gc)
        if setup.enabled:
            await executor.start()
        agent = cls(client, executor, setup)
        agent.peer = peer
        agent.cache = cache
        agent.gc = gc
        await client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)
        return agent

    async def stop(self, timeout_s: float = 20.0) -> None:
        self.client.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(self.task), timeout_s)
        if not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.executor.close()
        if self.peer is not None:
            await self.peer.close()
        if self.gc is not None:
            await self.gc.close()
        if self.cache is not None:
            await self.cache.close()


async def submit(
    http: httpx.AsyncClient,
    project: Path,
    node_id: str,
    manifest: Mapping[str, Any],
    *,
    idempotency_key: str | None = None,
) -> str:
    """Pack, upload, POST; returns the job id (201 or an idempotent 200)."""
    bundle = project.parent / f"{project.name}.bundle"
    packed = await asyncio.to_thread(pack_to_file, project, bundle)
    put = await http.put(f"/v1/blobs/{packed.digest.hex}", content=bundle.read_bytes())
    assert put.status_code in (200, 201), put.text
    headers = {"idempotency-key": idempotency_key} if idempotency_key else {}
    posted = await http.post(
        "/v1/jobs",
        json={
            "manifest": dict(manifest),
            "bundle_digest": str(packed.digest),
            "placement": {"node_id": node_id},
        },
        headers=headers,
    )
    assert posted.status_code in (200, 201), posted.text
    return str(posted.json()["job_id"])


async def wait_job(
    http: httpx.AsyncClient,
    job_id: str,
    *,
    states: frozenset[str] = TERMINAL,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = await http.get(f"/v1/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        last = resp.json()
        if last["state"] in states:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"job {job_id} not in {sorted(states)} after {timeout_s} s: {last}"
    )
