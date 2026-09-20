"""V0.3 G4 component tests: real discovery in the Hello, real monitor on the stream.

The agent's own hardware discovery runs against *this* machine, so the assertions
are about the **shape** of what it produced (there is a CPU, the thread count is
``os.cpu_count()``, ``gpus`` is a list, the first capability is the shape marker) -
never about a GPU, a driver version or a hostname, because the same test has to pass
on a CPU-only Windows laptop after a plain ``uv sync``.

Everything crosses a real loopback socket into a real ControllerRuntime backed by a
real PostgreSQL; the sampler is the real one (psutil + NVML if present).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mf_agent_harness import make_agent_settings
from mf_session_client import poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.hardware import NvmlSession
from meshforge.agent.hardware.probe import run_command
from meshforge.agent.identity import NodeIdentity
from meshforge.agent.resources import ResourceMonitor, ResourceSampler
from meshforge.agent.runtime import discover_hardware, serve
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSettings
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.docker]

ONLINE_TIMEOUT_S = 10.0
TIMEOUT_S = 15.0
ROOT_MOUNT = os.path.abspath(os.sep)
"""The one mount every platform has; keeps the sampler off this machine's layout."""

FAST_RESOURCES: dict[str, Any] = {
    "sample_interval_s": 0.1,
    "change_threshold_percent": 0.0,
    "max_report_interval_s": 0.1,
}


async def wait_for(check: Callable[[], bool], timeout_s: float = TIMEOUT_S) -> None:
    async def predicate() -> bool:
        return check()

    await poll_until(predicate, timeout_s=timeout_s)


# ------------------------------------------------------------------ discovery


@pytest.mark.asyncio
async def test_the_hello_carries_this_machines_discovered_hardware(
    runtime: ControllerRuntime,
    registry: Registry,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    settings = make_agent_settings(data_dir, grpc_target)
    nvml = NvmlSession()
    summary, facts = await discover_hardware(settings, str(identity.node_id), nvml)
    await asyncio.to_thread(nvml.close)
    assert facts is not None

    client = AgentSessionClient(settings, identity, hardware=summary)
    task = asyncio.create_task(client.run())
    try:
        await client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
        node = await registry.get_node(str(identity.node_id))
        assert node is not None
        hardware = node.hardware
        # Shape, never this machine's specifics.
        assert int(hardware["cpu_threads"]) == (os.cpu_count() or 0)
        assert int(hardware["cpu_cores"]) >= 1
        assert int(hardware.get("ram_bytes", 0)) >= 0
        assert isinstance(hardware.get("gpus", []), list)
        assert hardware["capabilities"][0] == "v0.3"
        assert any(tag.startswith("python") for tag in hardware["capabilities"]), (
            hardware["capabilities"]
        )
        assert len(hardware["capabilities"]) == len(set(hardware["capabilities"]))
        for gpu in hardware.get("gpus", []):
            assert len(gpu.get("model", "")) <= 128
            assert len(gpu.get("driver_version", "")) <= 64
        # The rich local record has one outcome per probe with a duration.
        assert {outcome.name for outcome in facts.probes} == {
            "cpu",
            "disks",
            "gpu",
            "software",
        }
        assert all(outcome.duration_s >= 0.0 for outcome in facts.probes)
        assert len(facts.disks) >= 1
    finally:
        client.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 10.0)


# -------------------------------------------------------------------- monitor


@pytest.mark.asyncio
async def test_the_monitor_reports_reach_the_controllers_live_ring_in_order(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    assert runtime.resources is not None
    settings = make_agent_settings(
        data_dir, grpc_target, resources=dict(FAST_RESOURCES)
    )
    client = AgentSessionClient(settings, identity)
    sampler = ResourceSampler(mounts=[ROOT_MOUNT], nvml=NvmlSession())
    monitor = ResourceMonitor(client, sampler, settings.agent.resources)
    node_id = str(identity.node_id)

    client_task = asyncio.create_task(client.run())
    monitor_task = asyncio.create_task(monitor.run())
    try:
        await client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
        tracker = runtime.resources

        def ring_len() -> int:
            ring = tracker.ring(node_id)
            return 0 if ring is None else len(ring)

        await wait_for(lambda: ring_len() >= 3)
        ring = tracker.ring(node_id)
        assert ring is not None
        samples = ring.snapshot()
        sequences = [s.sequence for s in samples]
        assert sequences == sorted(sequences)
        assert sequences == list(range(sequences[0], sequences[0] + len(sequences)))
        # One process, so one epoch across every sample (its value is whatever
        # this identity directory had reached; never asserted absolutely).
        assert len({s.session_epoch for s in samples}) == 1
        assert samples[0].session_epoch >= 1
        assert all(0.0 <= s.cpu_percent <= 100.0 for s in samples)
        assert all(s.ram_total_bytes >= s.ram_used_bytes for s in samples)
        assert all(len(s.disks) >= 1 for s in samples)
        assert tracker.duplicates == 0
        assert monitor.stats.reports >= 3
        assert monitor.stats.sampler_errors == 0
    finally:
        monitor.request_stop()
        client.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.gather(monitor_task, client_task), 10.0)
    assert monitor_task.done() and monitor_task.exception() is None


@pytest.mark.asyncio
async def test_the_monitor_keeps_sampling_but_sends_nothing_while_offline(
    grpc_target: str,
    runtime: ControllerRuntime,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """D9: an outage must not push stale samples through the replay ring."""
    settings = make_agent_settings(
        data_dir, grpc_target, resources=dict(FAST_RESOURCES)
    )
    client = AgentSessionClient(settings, identity)
    sampler = ResourceSampler(mounts=[ROOT_MOUNT], nvml=NvmlSession())
    monitor = ResourceMonitor(client, sampler, settings.agent.resources)

    client_task = asyncio.create_task(client.run())
    monitor_task = asyncio.create_task(monitor.run())
    try:
        await client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
        await wait_for(lambda: monitor.stats.reports >= 2)

        await runtime.stop()  # the controller goes away under the agent
        await client.wait_for_state(AgentState.BACKOFF, timeout_s=ONLINE_TIMEOUT_S)
        reports_at_outage = monitor.stats.reports
        ring_at_outage = len(client.ring)
        await wait_for(lambda: monitor.stats.offline_samples >= 3)

        assert monitor.stats.reports == reports_at_outage
        assert len(client.ring) == ring_at_outage
        assert monitor.stats.samples > monitor.stats.reports
    finally:
        monitor.request_stop()
        client.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.gather(monitor_task, client_task), 10.0)


# --------------------------------------------------------------- full runtime


@pytest.mark.asyncio
async def test_serve_wires_discovery_and_the_monitor_together(
    runtime: ControllerRuntime,
    registry: Registry,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """``meshforge-agent run``'s own path: discovery -> Hello -> monitor -> rows."""
    assert runtime.resources is not None
    settings = make_agent_settings(
        data_dir, grpc_target, resources=dict(FAST_RESOURCES)
    )
    node_id = str(identity.node_id)
    task = asyncio.create_task(serve(settings, identity=identity))
    try:
        await wait_for(
            lambda: bool(runtime.resources and runtime.resources.ring(node_id))
        )
        node = await registry.get_node(node_id)
        assert node is not None
        assert node.hardware["capabilities"][0] == "v0.3"
        assert any(tag.startswith("python") for tag in node.hardware["capabilities"])
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(task, 10.0)
    # No monitor task is left behind once serve() has returned.
    leftover = [
        t
        for t in asyncio.all_tasks()
        if t.get_name().startswith("agent-resources:") and not t.done()
    ]
    assert leftover == []


# ------------------------------------------------------------- process safety


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
def test_a_timed_out_probe_leaves_no_grandchild_behind(tmp_path: Path) -> None:
    """The whole process tree is killed, not just the child the probe started."""
    marker = tmp_path / "grandchild-was-alive"
    script = (
        "import subprocess, sys;"
        f"subprocess.Popen([sys.executable, '-c', "
        f"\"import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('x')\"]);"
        "import time; time.sleep(30)"
    )
    result = run_command(sys.executable, ["-c", script], timeout_s=0.5)
    assert not result.ok
    assert "timed out" in result.describe_failure()
    # Well past the grandchild's sleep: if the group kill missed it, the file
    # would be there by now.
    import time

    time.sleep(2.0)
    assert not marker.exists()


def test_the_sampler_survives_a_mount_that_disappears(tmp_path: Path) -> None:
    """A real sampler pointed at a path that is removed under it keeps working."""
    gone = tmp_path / "gone"
    gone.mkdir()
    sampler = ResourceSampler(mounts=[str(gone), ROOT_MOUNT])
    sampler.prime()
    assert len(sampler.sample().disks) == 2
    gone.rmdir()
    after = sampler.sample()
    assert len(after.disks) == 1
    sampler.close()


def test_agent_settings_expose_the_new_sections(tmp_path: Path) -> None:
    settings: AgentSettings = make_agent_settings(tmp_path, "127.0.0.1:47100")
    assert settings.agent.hardware.timeout_s == 5.0
    assert settings.agent.resources.enabled is True
    assert settings.agent.resources.sample_interval_s == 2.0
