"""Independent adversarial component tests for V0.3 G5 (roadmap 0.3.12, brief §5e).

Real sockets throughout - a raw ``socket``/``http.server`` standing in for "a
server that is not a MeshForge controller" - never ``httpx.MockTransport``. This
complements ``tests/component/agent/test_rest_api_and_cli.py``: same fixtures
(``runtime``, ``grpc_target``, ``identity``, ``data_dir``, real Postgres via
``pg_settings``), different edges - the CLI's failure paths against genuinely
hostile endpoints, and two more D16 shapes.

Deliberately self-contained (does not import the sibling test module): pytest's
import layout for two files with no ``__init__.py`` in the same directory is not a
stable place to depend on cross-module imports, so the small amount of harness code
this file needs (``Rest``/``rest_for``/``Agent``/``make_agent``) is duplicated
rather than shared.

Nothing here asserts this machine's hostname, IPs or GPUs.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import httpx
from mf_agent_harness import make_agent_settings, make_controller_settings
from mf_session_client import poll_until
from typer.testing import CliRunner

from meshforge.agent.hardware import NvmlSession
from meshforge.agent.identity import NodeIdentity
from meshforge.agent.resources import ResourceMonitor, ResourceSampler
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.state import AgentState
from meshforge.cli.app import app as cli_app
from meshforge.common.exit_codes import ExitCode
from meshforge.controller.liveness import NodeState
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

ONLINE_TIMEOUT_S = 10.0
TIMEOUT_S = 15.0
FAST_RESOURCES: dict[str, Any] = {
    "sample_interval_s": 0.1,
    "change_threshold_percent": 0.0,
    "max_report_interval_s": 0.1,
}
FAST_HISTORY: dict[str, Any] = {
    "bucket_s": 1.0,
    "bucket_grace_s": 0.5,  # D26: short, so rows appear fast; the grace still runs
    "flush_interval_s": 0.2,
    "retention_sweep_s": 3600.0,
    "live_ring_size": 200,
}


class Rest:
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
    """A real agent, optionally with its resource monitor, as tasks in this loop."""

    def __init__(
        self, client: AgentSessionClient, monitor: ResourceMonitor | None = None
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
    resources: dict[str, Any] | None = None,
) -> Agent:
    settings = make_agent_settings(
        data_dir, grpc_target, resources=dict(resources or FAST_RESOURCES)
    )
    client = AgentSessionClient(settings, identity)
    resource_monitor = (
        ResourceMonitor(
            client,
            ResourceSampler(mounts=[], nvml=NvmlSession()),
            settings.agent.resources,
        )
        if monitor
        else None
    )
    return Agent(client, resource_monitor)


# ------------------------------------------------------- a socket that never answers


@contextlib.contextmanager
def _accept_and_hang() -> Iterator[str]:
    """A real TCP listener that accepts the connection and then sends nothing,
    ever - the case a closed port cannot exercise: the client must time out, not
    hang forever and not treat "connected" as "answered"."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            with contextlib.suppress(OSError):
                while not stop.is_set():
                    time.sleep(0.05)
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2.0)


@pytest.mark.asyncio
async def test_compute_times_out_against_a_socket_that_accepts_and_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _accept_and_hang() as url:
        monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", url)
        monkeypatch.setenv("MESHFORGE_CLI__TIMEOUT_S", "1")
        runner = CliRunner()
        started = time.monotonic()
        result = await asyncio.to_thread(runner.invoke, cli_app, ["nodes"])
        elapsed = time.monotonic() - started
    assert result.exit_code == ExitCode.UNAVAILABLE, result.output
    assert "Traceback" not in result.output
    assert result.stderr.strip().count("\n") == 0
    assert elapsed < 5.0, f"the client waited {elapsed:.1f}s past its 1s timeout"


# ------------------------------------------------- a server that is not a controller


class _Canned(http.server.BaseHTTPRequestHandler):
    response_status = 200
    response_body = b""
    response_headers: tuple[tuple[str, str], ...] = ()
    truncate_content_length_by = 0

    def do_GET(self) -> None:
        body = self.response_body
        self.send_response(self.response_status)
        length = len(body) + self.truncate_content_length_by
        self.send_header("Content-Length", str(max(length, 0)))
        for key, value in self.response_headers:
            self.send_header(key, value)
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence stdlib access logs
        pass


@contextlib.contextmanager
def _canned_server(
    *,
    status: int = 200,
    body: bytes = b"",
    headers: tuple[tuple[str, str], ...] = (),
    truncate_by: int = 0,
) -> Iterator[str]:
    handler_cls = type(
        "_Handler",
        (_Canned,),
        {
            "response_status": status,
            "response_body": body,
            "response_headers": headers,
            "truncate_content_length_by": truncate_by,
        },
    )
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=2.0)
        server.server_close()


async def _run_nodes(monkeypatch: pytest.MonkeyPatch, url: str) -> Any:
    monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", url)
    monkeypatch.setenv("MESHFORGE_CLI__TIMEOUT_S", "5")
    runner = CliRunner()
    return await asyncio.to_thread(runner.invoke, cli_app, ["nodes"])


@pytest.mark.asyncio
async def test_compute_handles_an_html_body_from_a_non_meshforge_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _canned_server(
        status=200,
        body=b"<html><body>Not a MeshForge controller</body></html>",
        headers=(("Content-Type", "text/html"),),
    ) as url:
        result = await _run_nodes(monkeypatch, url)
    assert result.exit_code != ExitCode.OK
    assert "Traceback" not in result.output
    assert result.stderr.strip().count("\n") == 0


@pytest.mark.asyncio
async def test_compute_handles_a_truncated_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Content-Length`` promises more than the socket ever delivers."""
    with _canned_server(
        status=200, body=b'{"nodes": [truncated here', truncate_by=5_000
    ) as url:
        monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", url)
        monkeypatch.setenv("MESHFORGE_CLI__TIMEOUT_S", "2")
        runner = CliRunner()
        result = await asyncio.to_thread(runner.invoke, cli_app, ["nodes"])
    assert result.exit_code != ExitCode.OK
    assert "Traceback" not in result.output


@pytest.mark.asyncio
async def test_compute_handles_a_bare_500_with_no_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _canned_server(status=500, body=b"") as url:
        result = await _run_nodes(monkeypatch, url)
    assert result.exit_code != ExitCode.OK
    assert "Traceback" not in result.output


@pytest.mark.asyncio
async def test_compute_handles_a_well_formed_envelope_with_an_unknown_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future build's error code this ``compute`` has never heard of."""
    body = b'{"error": {"code": "a_future_code_v99", "message": "from tomorrow"}}'
    with _canned_server(
        status=400, body=body, headers=(("Content-Type", "application/json"),)
    ) as url:
        result = await _run_nodes(monkeypatch, url)
    # Not trusted as UNAVAILABLE (a polling script must not retry forever on an
    # answer it cannot interpret) - see client.py's `_failure_type` docstring.
    assert result.exit_code == ExitCode.FAILURE, result.output
    assert "Traceback" not in result.output


@pytest.mark.asyncio
async def test_compute_handles_a_huge_body_without_hanging_or_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    huge = b"[" + b"1," * 2_000_000 + b"1]"  # ~8 MB, not a JSON object
    with _canned_server(status=200, body=huge) as url:
        monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", url)
        monkeypatch.setenv("MESHFORGE_CLI__TIMEOUT_S", "10")
        runner = CliRunner()
        started = time.monotonic()
        result = await asyncio.to_thread(runner.invoke, cli_app, ["nodes"])
        elapsed = time.monotonic() - started
    assert result.exit_code != ExitCode.OK
    assert "Traceback" not in result.output
    assert elapsed < 10.0


@pytest.mark.asyncio
async def test_compute_does_not_follow_a_redirect_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ControllerClient`` sets ``follow_redirects=False``; a "controller" that
    redirects must not be chased forever.

    Points ``Location`` at a *second* canned server that would answer with a
    genuinely valid, successful payload if it were ever reached - so this test
    fails (not just "passes vacuously") if redirect-following is ever turned on:
    a followed redirect would exit ``OK``, an unfollowed one cannot.
    """
    good_body = b'{"nodes": [], "count": 0}'
    with (
        _canned_server(status=200, body=good_body) as target_url,
        _canned_server(
            status=302,
            body=b"",
            headers=(("Location", f"{target_url}/v1/nodes"),),
        ) as url,
    ):
        started = time.monotonic()
        result = await _run_nodes(monkeypatch, url)
        elapsed = time.monotonic() - started
    assert result.exit_code != ExitCode.OK, (
        "a 302 was followed to a successful response - follow_redirects must "
        "stay False (D15's client.py)"
    )
    assert "Traceback" not in result.output
    assert elapsed < 10.0


@pytest.mark.asyncio
async def test_compute_json_on_failure_is_still_one_parseable_line_or_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--json`` must never emit a half object on failure - the error goes to
    stderr as prose, stdout stays either empty or one clean JSON line."""
    with _canned_server(status=500, body=b"") as url:
        monkeypatch.setenv("MESHFORGE_CLI__CONTROLLER_URL", url)
        monkeypatch.setenv("MESHFORGE_CLI__TIMEOUT_S", "5")
        runner = CliRunner()
        result = await asyncio.to_thread(runner.invoke, cli_app, ["nodes", "--json"])
    assert result.exit_code != ExitCode.OK
    stdout = result.stdout.strip()
    if stdout:
        json.loads(stdout)  # must not raise


# --------------------------------------------------------------- more D16 shapes


@pytest.mark.asyncio
async def test_a_node_with_a_live_session_but_no_resource_sample_ever(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """``connected`` true, ``state`` ONLINE, ``sample_age_s`` null, no cpu/ram -
    the agent enrolled but its monitor has literally never sent one report."""
    agent = make_agent(data_dir, grpc_target, identity, monitor=False)
    try:
        await agent.online()
        async with rest_for(runtime) as rest:
            row = (await rest.json("/v1/nodes"))["nodes"][0]
        assert row["connected"] is True
        assert row["state"] == NodeState.ONLINE.value
        assert row["sample_age_s"] is None
        assert row["cpu_percent"] is None
        assert row["gpus"] == []
    finally:
        await agent.close()


@pytest.mark.asyncio
async def test_history_and_live_ring_disagree_in_resolution_as_documented(
    pg_settings: DatabaseSettings,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """1-minute-class history buckets vs 2-second-class live samples: over one
    short window the live ring must show strictly more, finer-grained points than
    the (at most one, still-open) history bucket - the two paths disagree on
    resolution on purpose (``NodeResources`` docstring)."""
    settings = make_controller_settings(pg_settings, resources=dict(FAST_HISTORY))
    async with ControllerRuntime.start(settings) as runtime:
        node_id = str(identity.node_id)
        agent = make_agent(
            data_dir,
            f"127.0.0.1:{runtime.grpc_port}",
            identity,
            resources={
                "sample_interval_s": 0.05,
                "change_threshold_percent": 0.0,
                "max_report_interval_s": 0.05,
            },
        )
        try:
            await agent.online()
            async with rest_for(runtime) as rest:

                async def enough_live_samples() -> bool:
                    body = await rest.json(
                        f"/v1/nodes/{node_id}/resources", live_limit=100
                    )
                    return bool(body["live_count"] >= 10)

                await poll_until(enough_live_samples, timeout_s=TIMEOUT_S)
                body = await rest.json(f"/v1/nodes/{node_id}/resources", live_limit=100)
                # The live ring has fine-grained samples; at most a couple of
                # 1-second history buckets can possibly have closed in this
                # window, so live resolution must be visibly finer.
                assert body["live_count"] >= 10
                assert body["history_count"] <= body["live_count"]
        finally:
            await agent.close()


@pytest.mark.asyncio
async def test_nb_g5_1_regression_a_graceful_stop_is_offline_within_one_lease(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    """NB-G5-1 regression (G6, D19), on the same tight ``FAST_LIVENESS`` clock the
    G5 tester used to *confirm* the defect: a graceful ``request_stop`` kept
    ``state = online`` for 6x the would-be OFFLINE deadline.

    Now the agent's Goodbye (V0.4 D40) is ``STREAM_CLOSED``: the row must read
    ``offline`` within one lease of the agent's stop returning, ``connected``
    false, and the session's ``end_reason`` must be ``agent_closed``.
    """
    liveness = runtime.settings.controller.liveness
    lease_s = liveness.heartbeat_interval_s * liveness.lease_multiplier
    node_id = str(identity.node_id)
    agent = make_agent(data_dir, grpc_target, identity, monitor=False)
    try:
        await agent.online()
        session_id = agent.client.session_id
        agent.client.request_stop()
        await asyncio.wait_for(agent.client_task, 10.0)  # Goodbye + OK status
        async with rest_for(runtime) as rest:
            started = time.monotonic()
            row: dict[str, Any] = {}

            async def offline() -> bool:
                row.update((await rest.json("/v1/nodes"))["nodes"][0])
                return bool(row["state"] == NodeState.OFFLINE.value)

            await poll_until(offline, timeout_s=lease_s * 6, interval_s=0.01)
            elapsed = time.monotonic() - started
            assert elapsed <= lease_s, (
                f"OFFLINE took {elapsed:.2f}s after a graceful stop; one lease "
                f"is {lease_s:.2f}s (NB-G5-1 regression)"
            )
            assert row["connected"] is False
            sessions = (await rest.json("/v1/sessions", node_id=node_id))["sessions"]
            (ours,) = [s for s in sessions if s["session_id"] == session_id]
            assert ours["end_reason"] == "agent_closed"
    finally:
        await agent.close()
