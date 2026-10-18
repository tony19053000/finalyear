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
from meshforge.agent.conditions.runner import ConditionRunner, probe_kinds
from meshforge.agent.envs.node_prepare import PrepareService
from meshforge.agent.execution.docker_runtime import DockerConfig, DockerRuntime
from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import ExecutionSetup, build_execution
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.gc.service import GcService
from meshforge.agent.hardware.limits import clip_capabilities
from meshforge.agent.http_tls import AgentHttpTls, load_http_tls
from meshforge.agent.identity import load_or_create
from meshforge.agent.owner_activity import ActivityState
from meshforge.agent.owner_guard import OwnerGuard
from meshforge.agent.peer import PeerBlobServer
from meshforge.agent.peer.probe import ProbeService
from meshforge.agent.policy import LivePolicy
from meshforge.agent.runtime import (
    build_live_policy,
    controller_endpoint,
    controller_host,
    route_commands,
    start_cache,
    start_gc,
    start_peer,
    start_policy_watch,
    start_renewer,
    stop_task,
    wire_executor,
)
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.hello import basic_hardware
from meshforge.agent.session.state import AgentState
from meshforge.common.bench_conditions import capability_tag
from meshforge.common.bundle import pack_to_file

__all__ = ["TERMINAL", "ExecAgent", "no_docker", "submit", "wait_job"]

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class _NoDocker(DockerRuntime):
    async def probe(self) -> RuntimeAvailability:
        return RuntimeAvailability("docker", False, reason="disabled_in_test")


def no_docker(config: DockerConfig) -> DockerRuntime:
    return _NoDocker(config)


class FakeActivity:
    """V0.9 G5: owner activity the test sets (quacks like ``OwnerActivity``
    for :class:`~meshforge.agent.owner_guard.OwnerGuard`)."""

    def __init__(self) -> None:
        self.state = ActivityState(idle_source="unknown")


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
        self.preparer: PrepareService | None = None
        self.prober: ProbeService | None = None
        self.http_tls: AgentHttpTls | None = None
        self.renew_task: asyncio.Task[None] | None = None
        self.policy_task: asyncio.Task[None] | None = None
        self.live_policy: LivePolicy | None = None
        self.guard: OwnerGuard | None = None
        self.activity = FakeActivity()
        self.task: asyncio.Task[None] = asyncio.create_task(client.run())

    async def set_owner_active(self, active: bool, reason: str = "idle") -> int:
        """V0.9 G5: the owner starts / stops using this "laptop" (the heartbeat
        carries it; turning on pre-empts running benchmark attempts)."""
        self.activity.state = ActivityState(
            active=active, reason=reason if active else "", idle_source="unknown"
        )
        assert self.guard is not None
        return await self.guard.refresh()

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
        fake_gpus: int = 0,
        renew: bool = False,
    ) -> ExecAgent:
        """``fake_gpus`` (V0.6 G2): declare that many 4 GiB NVIDIA GPUs and let
        the subprocess runtime "pass" them (it only sets CUDA_VISIBLE_DEVICES),
        so GPU slots and GPU placement run on a CPU-only machine.
        ``renew`` (V0.7 G4): run the certificate renewer as serve() does (off by
        default: G3's expiry-cut tests need an agent that does NOT renew)."""
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
                host_has_nvidia_gpu=fake_gpus > 0,
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
        for index in range(fake_gpus):
            hardware.gpus.add(index=index, model="fake", vram_bytes=4 * 1024**3)
        # V0.9 G4 (D217): the benchmark conditions, as in serve() (never
        # gpu_load: the fake GPUs cannot be loaded)
        condition_kinds = (
            await probe_kinds(settings.agent.conditions, has_gpu=False)
            if setup.enabled
            else frozenset()
        )
        condition_tag = capability_tag(condition_kinds)
        if condition_tag is not None:
            hardware.capabilities[:] = clip_capabilities(
                [*hardware.capabilities, condition_tag]
            )
        client = AgentSessionClient(settings, identity, hardware=hardware)
        # V0.7 G5 (D142): the owner's live policy, wired as in serve().
        live_policy = build_live_policy(settings, gpu_count=fake_gpus)
        client.live_policy = live_policy
        # V0.7 G3: HTTPS with the node certificate, as in serve().
        http_tls = None if settings.security.insecure else load_http_tls(identity)
        cas = AgentCas.at(settings.data_dir)
        # V0.5 G4: the peer server starts only when [agent.peer] bind is set
        # (agent_overrides={"peer": {"bind": "127.0.0.1", "port": 0}}).
        peer = await start_peer(settings, cas, client)
        cache = await start_cache(settings, cas, client)
        # V0.5 G5: GC as in serve() ([agent.gc] via agent_overrides={"gc": {...}}).
        gc = await start_gc(settings, cas, node_id=client.node_id, cache=cache)
        conditions = ConditionRunner(
            settings.agent.conditions,
            data_dir=settings.data_dir,
            kinds=condition_kinds,
            store=cas.store,
            index=lambda: None if gc is None else gc.index,
            controller=lambda: controller_endpoint(client.controller_target),
        )
        activity = FakeActivity()
        guard = OwnerGuard(policy=live_policy, activity=activity)  # type: ignore[arg-type]
        executor = Executor(
            settings,
            setup,
            conditions=conditions,
            owner_guard=guard.blocked,
            cas=cas,
            node_id=client.node_id,
            report=client.send_report,
            resend=client.resend_report,
            log_sink=client.send_log,
            controller_host=lambda: controller_host(client.controller_target),
            gc=gc,
            declared_gpus=range(fake_gpus),
            http_tls=http_tls,
            policy=live_policy,
            cpu_threads=hardware.cpu_threads or hardware.cpu_cores or None,
        )
        wire_executor(client, executor)
        # V0.9 G5 (D246): the owner guard, wired as in serve()
        guard.preempt = executor.preempt_benchmarks
        client.owner_heartbeat = guard.heartbeat_fields
        preparer = (
            PrepareService(settings, setup, client.send_report, node_id=client.node_id)
            if setup.enabled
            else None
        )
        # V0.6 G4 (D111): network probes, as in serve().
        prober = ProbeService(
            settings.agent.probe,
            client.send_report,
            node_id=client.node_id,
            http_tls=http_tls,
        )
        client.command_handler = route_commands(
            executor,
            gc=gc,
            prepare=preparer.handle if preparer is not None else None,
            probe=prober.handle,
        )
        if setup.enabled:
            await executor.start()
        agent = cls(client, executor, setup)
        agent.http_tls = http_tls
        agent.live_policy = live_policy
        agent.guard = guard
        agent.activity = activity
        agent.policy_task = start_policy_watch(
            settings, client, live_policy, guard=guard
        )
        if renew:
            agent.renew_task = start_renewer(
                settings, client, http_tls=http_tls, peer=peer
            )
        agent.peer = peer
        agent.cache = cache
        agent.gc = gc
        agent.preparer = preparer
        agent.prober = prober
        await client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)
        return agent

    async def stop(self, timeout_s: float = 20.0) -> None:
        self.client.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(self.task), timeout_s)
        if not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await stop_task(self.renew_task)
        await stop_task(self.policy_task)
        await self.executor.close()
        if self.peer is not None:
            await self.peer.close()
        if self.preparer is not None:
            await self.preparer.close()
        if self.prober is not None:
            await self.prober.close()
        if self.gc is not None:
            await self.gc.close()
        if self.cache is not None:
            await self.cache.close()


async def submit(
    http: httpx.AsyncClient,
    project: Path,
    node_id: str | None,
    manifest: Mapping[str, Any],
    *,
    idempotency_key: str | None = None,
    placement: Mapping[str, Any] | None = None,
    body: Mapping[str, Any] | None = None,
) -> str:
    """Pack, upload, POST; returns the job id (201 or an idempotent 200).
    ``node_id`` None = automatic placement (V0.6); ``placement`` adds keys
    (``policy``), ``body`` top-level ones (``submitter_node_id``)."""
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
            "placement": {
                **({} if node_id is None else {"node_id": node_id}),
                **(placement or {}),
            },
            **(body or {}),
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
