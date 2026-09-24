"""V0.3 G5 component tests: the read-only ``/v1`` surface and ``compute``, for real.

Real PostgreSQL (``pg_settings``), the real :class:`ControllerRuntime` (gRPC +
uvicorn on ephemeral loopback ports), the real
:class:`~meshforge.agent.session.client.AgentSessionClient` with the real resource
monitor, and real HTTP over the loopback socket - no ASGI shortcut, no fake
registry. The CLI is invoked **in-process** (``typer.testing.CliRunner``) inside
``asyncio.to_thread``: its httpx client is synchronous, so calling it on the test's
event loop would deadlock against the uvicorn server running there.

This file lives under ``tests/component/agent`` rather than
``tests/component/controller`` because it needs the *real* agent fixtures (identity
directory, ``AgentSessionClient``, hardware discovery) that only this directory's
conftest provides; the controller directory has the hand-rolled ``FakeAgent``,
which cannot produce this machine's discovered hardware or a real monitor.

Nothing asserts this machine's hostname, IP addresses, GPUs or driver versions -
only shapes - so the file passes on a CPU-only Windows laptop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from mf_agent_harness import make_agent_settings
from mf_session_client import poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import httpx
from typer.testing import CliRunner

from meshforge.agent.hardware import NvmlSession
from meshforge.agent.hardware.deadline import DeadlineThread
from meshforge.agent.identity import NodeIdentity
from meshforge.agent.resources import ResourceMonitor, ResourceSampler
from meshforge.agent.runtime import discover_hardware
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.state import AgentState
from meshforge.cli.app import app as cli_app
from meshforge.common.errors import FailureType
from meshforge.common.exit_codes import ExitCode
from meshforge.common.ids import new_node_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings, LivenessSettings
from meshforge.controller.store.settings import DatabaseSettings

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
FAST_HISTORY: dict[str, Any] = {
    "bucket_s": 1.0,
    "flush_interval_s": 0.2,
    "retention_sweep_s": 3600.0,
    "live_ring_size": 100,
}
SLOW_LIVENESS = LivenessSettings(
    heartbeat_interval_s=30.0, miss_allowance_s=5.0, lease_multiplier=3
)
"""A lease that cannot expire during a test: it pins "the stream is gone but the
database still says ONLINE", which is the D16 disagreement the API must expose."""


def json_line(stdout: str) -> dict[str, Any]:
    """The CLI's ``--json`` line, ignoring anything else on stdout.

    ``CliRunner`` redirects ``sys.stdout`` for the whole process, so the
    controller's and the agent's structlog console output - written from this
    test's event loop while the CLI runs in a worker thread - lands in the same
    buffer. In a real terminal the CLI is the only writer; this filter is a test
    artefact, not a tolerance the product needs.
    """
    lines = [line for line in stdout.splitlines() if line.startswith("{")]
    assert lines, stdout
    body: dict[str, Any] = json.loads(lines[-1])
    return body


async def wait_for(check: Callable[[], bool], timeout_s: float = TIMEOUT_S) -> None:
    async def predicate() -> bool:
        return check()

    await poll_until(predicate, timeout_s=timeout_s)


class Rest:
    """Real HTTP against the runtime's uvicorn port."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def get(self, path: str, **params: Any) -> httpx.Response:
        return await self._client.get(path, params=params)

    async def json(self, path: str, **params: Any) -> dict[str, Any]:
        response = await self.get(path, **params)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body


@contextlib.asynccontextmanager
async def rest_for(runtime: ControllerRuntime) -> Any:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}", timeout=10.0
    ) as client:
        yield Rest(client)


class Agent:
    """A real agent plus its monitor, as two tasks in this test's loop."""

    def __init__(
        self,
        client: AgentSessionClient,
        monitor: ResourceMonitor | None = None,
    ) -> None:
        self.client = client
        self.monitor = monitor
        self.client_task = asyncio.create_task(client.run())
        self.monitor_task = (
            None if monitor is None else asyncio.create_task(monitor.run())
        )

    async def online(self) -> None:
        await self.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)

    async def stop_monitor(self) -> None:
        if self.monitor is not None and self.monitor_task is not None:
            self.monitor.request_stop()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(self.monitor_task, 10.0)

    async def kill_stream(self) -> None:
        """Drop the gRPC stream without a graceful goodbye (a cable pull)."""
        await self.stop_monitor()
        self.client_task.cancel()
        await asyncio.gather(self.client_task, return_exceptions=True)

    async def close(self) -> None:
        await self.stop_monitor()
        self.client.request_stop()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(self.client_task, 10.0)
        if not self.client_task.done():
            self.client_task.cancel()
            await asyncio.gather(self.client_task, return_exceptions=True)


def make_agent(
    data_dir: Path,
    grpc_target: str,
    identity: NodeIdentity,
    *,
    monitor: bool = True,
    hardware: Any = None,
) -> Agent:
    settings = make_agent_settings(
        data_dir, grpc_target, resources=dict(FAST_RESOURCES)
    )
    client = AgentSessionClient(settings, identity, hardware=hardware)
    resource_monitor = (
        ResourceMonitor(
            client,
            ResourceSampler(mounts=[ROOT_MOUNT], nvml=NvmlSession()),
            settings.agent.resources,
        )
        if monitor
        else None
    )
    return Agent(client, resource_monitor)


# ------------------------------------------------------------- GET /v1/nodes


@pytest.mark.asyncio
async def test_a_connected_agent_appears_online_with_a_fresh_sample_age(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """The happy path end to end, and that ``sample_age_s`` really is an age:
    it must *grow* once the monitor stops feeding the ring."""
    node_id = str(identity.node_id)
    agent = make_agent(data_dir, grpc_target, identity)
    try:
        await agent.online()
        assert runtime.resources is not None
        await wait_for(
            lambda: (
                runtime.resources is not None
                and runtime.resources.latest(node_id) is not None
            )
        )

        async with rest_for(runtime) as rest:
            body = await rest.json("/v1/nodes")
            assert body["count"] == 1
            row = body["nodes"][0]
            assert row["node_id"] == node_id
            assert row["state"] == NodeState.ONLINE.value
            assert row["connected"] is True
            assert row["rtt_ms"] is None or row["rtt_ms"] >= 0.0
            assert 0.0 <= row["sample_age_s"] < 5.0
            assert 0.0 <= row["cpu_percent"] <= 100.0
            assert row["ram_total_bytes"] >= row["ram_used_bytes"] > 0
            assert isinstance(row["gpus"], list)
            # Timestamps carry an explicit offset (the SDK and the dashboard parse them).
            assert row["last_seen_at"].endswith("+00:00")

            await agent.stop_monitor()
            first = (await rest.json("/v1/nodes"))["nodes"][0]["sample_age_s"]
            await asyncio.sleep(0.5)
            second = (await rest.json("/v1/nodes"))["nodes"][0]["sample_age_s"]
            assert second > first, (first, second)
            # Still connected: the stream is alive, only the reports stopped (D16).
            assert (await rest.json("/v1/nodes"))["nodes"][0]["connected"] is True
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_node_detail_returns_the_hardware_the_agent_actually_sent(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """Shape only: cpu_threads is this machine's, but a GPU, a driver version, a
    hostname or an IP address is never asserted (D13 keeps the rich facts local)."""
    settings = make_agent_settings(data_dir, grpc_target)
    nvml = NvmlSession()
    guard = DeadlineThread("hardware-rest-test")
    summary, _facts = await discover_hardware(
        settings, str(identity.node_id), nvml, guard
    )
    await asyncio.to_thread(nvml.close)

    agent = make_agent(data_dir, grpc_target, identity, monitor=False, hardware=summary)
    try:
        await agent.online()
        async with rest_for(runtime) as rest:
            body = await rest.json(f"/v1/nodes/{identity.node_id}")
        hardware = body["hardware"]
        assert int(hardware["cpu_threads"]) == (os.cpu_count() or 0)
        assert int(hardware["cpu_cores"]) >= 1
        assert isinstance(hardware.get("gpus", []), list)
        assert hardware["capabilities"][0] == "v0.3"
        # Session fields of the *live* session.
        assert body["protocol_version"] >= 1
        assert body["agent_version"]
        assert body["session_epoch"] >= 1
        assert body["last_session_id"]
        assert body["first_seen_at"].endswith("+00:00")
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_an_unknown_node_is_the_typed_404_envelope(
    runtime: ControllerRuntime,
) -> None:
    unknown = str(new_node_id())
    async with rest_for(runtime) as rest:
        response = await rest.get(f"/v1/nodes/{unknown}")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": FailureType.NOT_FOUND.value, "message": "no such node"}
    }
    assert unknown not in response.text


# --------------------------------------------------------------- D16 in anger


@pytest.mark.asyncio
async def test_a_dead_stream_reads_suspect_while_connected_goes_false(
    settings: ControllerSettings,
    pg_settings: DatabaseSettings,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """The D16 disagreement, with a lease too long to expire during the test.

    Since G6 (D19) a stream that dies without a half-close moves the node to
    SUSPECT at once - not OFFLINE: the lease keeps running on a lingering entry,
    and with this lease it cannot expire during the test. So ``state`` is
    ``suspect`` while ``connected`` is false - still the case a single "online"
    boolean would get wrong, now with a state that says so. (Before G6 this read
    ``online``: NB-G5-1.)
    """
    from mf_agent_harness import make_controller_settings

    slow = make_controller_settings(pg_settings, liveness=SLOW_LIVENESS)
    async with ControllerRuntime.start(slow) as runtime:
        agent = make_agent(
            data_dir, f"127.0.0.1:{runtime.grpc_port}", identity, monitor=False
        )
        try:
            await agent.online()
            async with rest_for(runtime) as rest:
                before = (await rest.json("/v1/nodes"))["nodes"][0]
                assert (before["state"], before["connected"]) == (
                    NodeState.ONLINE.value,
                    True,
                )

                await agent.kill_stream()
                rows: list[dict[str, Any]] = []

                async def suspect() -> bool:
                    rows[:] = (await rest.json("/v1/nodes"))["nodes"]
                    return bool(rows) and rows[0]["state"] == "suspect"

                await poll_until(suspect, timeout_s=TIMEOUT_S)
                after = rows[0]
                assert after["connected"] is False
                assert after["rtt_ms"] is None
                assert runtime.manager is not None
                assert runtime.manager.get(str(identity.node_id)) is None
                assert runtime.manager.get_lingering(str(identity.node_id))
        finally:
            await agent.close()


@pytest.mark.asyncio
async def test_a_killed_stream_walks_suspect_then_offline_at_the_lease_edge(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """NB-G5-1 regression, abrupt half (D19). Before G6 a session that ended
    never moved ``state`` again (this test used to pin ``online`` after 3 leases).

    Now: the agent's task is cancelled, its channel closes without a half-close
    -> ``stream_lost``, SUSPECT at once, and - with nobody reconnecting - OFFLINE
    at the lease edge measured from the last heartbeat, all visible over REST.
    """
    liveness = runtime.settings.controller.liveness
    lease_s = liveness.heartbeat_interval_s * liveness.lease_multiplier
    node_id = str(identity.node_id)
    agent = make_agent(data_dir, grpc_target, identity, monitor=False)
    try:
        await agent.online()
        session_id = agent.client.session_id
        async with rest_for(runtime) as rest:
            await agent.kill_stream()
            loop = asyncio.get_running_loop()
            killed_at = loop.time()
            seen: list[str] = []

            async def offline() -> bool:
                row = (await rest.json("/v1/nodes"))["nodes"][0]
                if not seen or seen[-1] != row["state"]:
                    seen.append(row["state"])
                return bool(row["state"] == "offline")

            await poll_until(offline, timeout_s=TIMEOUT_S, interval_s=0.01)
            took = loop.time() - killed_at
            row = (await rest.json("/v1/nodes"))["nodes"][0]
            assert row["connected"] is False
            # ONLINE -> SUSPECT -> OFFLINE, never straight to OFFLINE.
            assert "suspect" in seen, seen
            assert seen[-1] == "offline"
            # At the lease edge: not before a lease has passed, and not long after
            # (generous bound for a loaded machine; the unit tests pin the edge).
            assert took <= lease_s + 5.0, took
            sessions = (await rest.json("/v1/sessions", node_id=node_id))["sessions"]
            (ours,) = [s for s in sessions if s["session_id"] == session_id]
            assert ours["end_reason"] == "stream_lost"
            assert runtime.manager is not None
            assert runtime.manager.get_lingering(node_id) is None  # resolved
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_a_node_this_process_never_held_shows_the_database_state(
    pg_settings: DatabaseSettings,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """A fresh controller over the same database: the row survives, the stream
    does not. ``state`` comes from the database, ``connected`` is false, and there
    is no live sample because the ring is in memory only."""
    from mf_agent_harness import make_controller_settings

    node_id = str(identity.node_id)
    first = make_controller_settings(pg_settings)
    async with ControllerRuntime.start(first) as runtime:
        agent = make_agent(data_dir, f"127.0.0.1:{runtime.grpc_port}", identity)
        try:
            await agent.online()
            await wait_for(
                lambda: (
                    runtime.resources is not None
                    and runtime.resources.latest(node_id) is not None
                )
            )
        finally:
            await agent.close()

    second = make_controller_settings(pg_settings)
    async with ControllerRuntime.start(second) as runtime2, rest_for(runtime2) as rest:
        row = (await rest.json("/v1/nodes"))["nodes"][0]
    assert row["node_id"] == node_id
    assert row["connected"] is False
    assert row["sample_age_s"] is None
    assert row["cpu_percent"] is None
    assert row["state"] in {s.value for s in NodeState}


# ------------------------------------------ GET /v1/nodes/{id}/resources


@pytest.mark.asyncio
async def test_resources_returns_flushed_history_and_the_live_ring(
    pg_settings: DatabaseSettings,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    from mf_agent_harness import make_controller_settings

    node_id = str(identity.node_id)
    settings = make_controller_settings(pg_settings, resources=dict(FAST_HISTORY))
    async with ControllerRuntime.start(settings) as runtime:
        agent = make_agent(data_dir, f"127.0.0.1:{runtime.grpc_port}", identity)
        try:
            await agent.online()
            async with rest_for(runtime) as rest:

                async def has_history() -> bool:
                    body = await rest.json(f"/v1/nodes/{node_id}/resources")
                    return bool(body["history_count"] >= 1 and body["live_count"] >= 1)

                # One 1-second bucket must close and be flushed (every 0.2 s).
                await poll_until(has_history, timeout_s=TIMEOUT_S)
                body = await rest.json(f"/v1/nodes/{node_id}/resources", live_limit=2)
                row = body["history"][0]
                assert row["samples"] >= 1
                assert 0.0 <= row["cpu_percent_mean"] <= 100.0
                assert row["cpu_percent_max"] >= row["cpu_percent_mean"]
                assert row["bucket_start"].endswith("+00:00")
                assert isinstance(row["disks"], dict)
                assert body["live_count"] <= 2
                live = body["live"][-1]
                assert live["age_s"] >= 0.0
                assert [s["sequence"] for s in body["live"]] == sorted(
                    s["sequence"] for s in body["live"]
                )
                # Bounds are enforced by the real app, not only by the fakes.
                assert (
                    await rest.get(f"/v1/nodes/{node_id}/resources", limit=0)
                ).status_code == 400
        finally:
            await agent.close()


# --------------------------------------------------------------- GET /v1/sessions


@pytest.mark.asyncio
async def test_sessions_lists_the_live_session_of_a_connected_agent(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    agent = make_agent(data_dir, grpc_target, identity, monitor=False)
    try:
        await agent.online()
        async with rest_for(runtime) as rest:
            body = await rest.json("/v1/sessions")
            assert body["count"] == 1
            row = body["sessions"][0]
            assert row["node_id"] == str(identity.node_id)
            assert row["connected"] is True
            assert row["ended_at"] is None
            assert row["end_reason"] is None
            filtered = await rest.json("/v1/sessions", node_id=str(identity.node_id))
            assert filtered["count"] == 1
    finally:
        await agent.close()


# --------------------------------------------------------------------- the CLI


@pytest.mark.asyncio
async def test_compute_nodes_json_against_the_real_controller(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``compute`` over a real socket, in-process, with its real httpx client."""
    monkeypatch.setenv(
        "MESHFORGE_CLI__CONTROLLER_URL", f"http://127.0.0.1:{runtime.http_port}"
    )
    # Rich folds a 31-character node_id across lines at the default 80 columns.
    monkeypatch.setenv("COLUMNS", "220")
    agent = make_agent(data_dir, grpc_target, identity)
    runner = CliRunner()
    try:
        await agent.online()
        result = await asyncio.to_thread(runner.invoke, cli_app, ["nodes", "--json"])
        assert result.exit_code == ExitCode.OK, result.output
        payload = json_line(result.stdout)
        assert payload["count"] == 1
        assert payload["nodes"][0]["node_id"] == str(identity.node_id)

        table = await asyncio.to_thread(runner.invoke, cli_app, ["nodes"])
        assert table.exit_code == ExitCode.OK, table.output
        assert str(identity.node_id) in table.stdout
        table.stdout.encode("cp437")  # no Unicode box-drawing

        status = await asyncio.to_thread(runner.invoke, cli_app, ["status", "--json"])
        assert status.exit_code == ExitCode.OK, status.output
        reported = json_line(status.stdout)
        assert reported["reachable"] is True
        assert reported["live_sessions"] == 1
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_compute_exits_unavailable_against_a_really_closed_port(
    runtime: ControllerRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely refused TCP connection, not a mocked transport: the controller
    is stopped first, so its port is actually gone."""
    url = f"http://127.0.0.1:{runtime.http_port}"
    monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", url)
    await runtime.stop()
    runner = CliRunner()
    result = await asyncio.to_thread(runner.invoke, cli_app, ["nodes"])
    assert result.exit_code == ExitCode.UNAVAILABLE
    assert "cannot reach the controller" in result.stderr
    assert "Traceback" not in result.output
