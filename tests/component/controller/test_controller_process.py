"""V0.3 G2 component tests of the controller *process* (0.3.6 E: one process).

The real ``meshforge-controller`` entry point runs as a child process against the
test's PostgreSQL; a hand-rolled agent connects over a real socket; then the process
is killed (SIGKILL) or asked to stop (SIGTERM) and the database is inspected. These
are the failure injections the in-process tests cannot do: a controller that dies
without running any shutdown code, and a real signal.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import grpc
import pytest
from mf_session_client import FakeAgent, free_port_pair, poll_until
from pydantic import SecretStr

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_controller_harness import FAST_LIVENESS, make_settings

from meshforge.common.errors import ExitCode
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.server import ControllerRuntime, run_controller
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

START_TIMEOUT_S = 30.0  # first start on a fresh DB runs the migrations too


class ControllerProcess:
    """A ``meshforge-controller`` child bound to two loopback ports."""

    def __init__(self, database: DatabaseSettings, tmp_path: Path) -> None:
        self.grpc_port, self.http_port = free_port_pair()
        cfg = tmp_path / "controller.toml"
        cfg.write_text(
            "[log]\n"
            'format = "json"\n'
            'level = "INFO"\n'
            "[controller]\n"
            f'grpc_bind = "127.0.0.1:{self.grpc_port}"\n'
            f'http_bind = "127.0.0.1:{self.http_port}"\n'
            "shutdown_drain_s = 2.0\n"
            "[controller.liveness]\n"
            f"heartbeat_interval_s = {FAST_LIVENESS.heartbeat_interval_s}\n"
            f"miss_allowance_s = {FAST_LIVENESS.miss_allowance_s}\n"
            f"lease_multiplier = {FAST_LIVENESS.lease_multiplier}\n",
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
        env.update(
            {
                "MESHFORGE_DATABASE__HOST": database.host,
                "MESHFORGE_DATABASE__PORT": str(database.port),
                "MESHFORGE_DATABASE__NAME": database.name,
                "MESHFORGE_DATABASE__USER": database.user,
                "MESHFORGE_DATABASE__PASSWORD": database.password.get_secret_value(),
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from meshforge.controller.app import main; main()",
                "--config",
                str(cfg),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    @property
    def grpc_target(self) -> str:
        return f"127.0.0.1:{self.grpc_port}"

    def healthz(self) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{self.http_port}/healthz", timeout=1
            ) as resp:
                return dict(json.loads(resp.read()))
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            return None

    async def wait_ready(self) -> None:
        async def ready() -> bool:
            if self.proc.poll() is not None:
                raise AssertionError(
                    f"controller exited early ({self.proc.returncode}):\n{self.output()}"
                )
            return (await asyncio.to_thread(self.healthz)) is not None

        await poll_until(ready, timeout_s=START_TIMEOUT_S, interval_s=0.1)

    def output(self) -> str:
        if self.proc.stdout is None:
            return ""
        try:
            return self.proc.stdout.read()
        except ValueError:
            return ""

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)

    def close(self) -> None:
        self.kill()
        if self.proc.stdout is not None:
            self.proc.stdout.close()


@pytest.fixture
def controller_process(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> Iterator[ControllerProcess]:
    cp = ControllerProcess(pg_settings, tmp_path)
    try:
        yield cp
    finally:
        cp.close()


async def _connect_agent(target: str) -> tuple[grpc.aio.Channel, FakeAgent, str]:
    channel = grpc.aio.insecure_channel(target)
    agent = FakeAgent(channel)
    ack = await agent.connect()
    agent.start_heartbeats(FAST_LIVENESS.heartbeat_interval_s)
    return channel, agent, ack.session_id


@pytest.mark.asyncio
async def test_sigkill_then_restart_sweeps_old_sessions(
    controller_process: ControllerProcess,
    pg_settings: DatabaseSettings,
    registry: Registry,
) -> None:
    await controller_process.wait_ready()
    channel, agent, session_id = await _connect_agent(controller_process.grpc_target)
    try:
        assert (await asyncio.to_thread(controller_process.healthz)) == {
            "status": "ok",
            "sessions": 1,
        }
        node = await registry.get_node(agent.node_id)
        assert node is not None and node.state is NodeState.ONLINE

        # The controller dies without running a single line of shutdown code.
        controller_process.kill()
        await agent.wait_ended(timeout_s=10)
        assert agent.end_code is grpc.StatusCode.UNAVAILABLE

        # Nobody closed the row: this is exactly the state a restart must repair.
        stale = await registry.get_session(session_id)
        assert stale is not None and stale.is_open
        node = await registry.get_node(agent.node_id)
        assert node is not None and node.state is NodeState.ONLINE  # a lie, now
    finally:
        await agent.close()
        await channel.close()

    # Restart (in-process this time) against the same database.
    async with ControllerRuntime.start(make_settings(pg_settings)):
        swept = await registry.get_session(session_id)
        assert swept is not None
        assert swept.end_reason is SessionEndReason.CONTROLLER_RESTART
        assert swept.ended_at is not None
        node = await registry.get_node(agent.node_id)
        assert node is not None and node.state is NodeState.OFFLINE


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM is not graceful on Windows"
)
async def test_sigterm_is_graceful_exit_zero_and_sessions_closed(
    controller_process: ControllerProcess, registry: Registry
) -> None:
    await controller_process.wait_ready()
    channel, agent, session_id = await _connect_agent(controller_process.grpc_target)
    try:
        controller_process.proc.send_signal(signal.SIGTERM)
        await agent.wait_ended(timeout_s=10)
        returncode = await asyncio.to_thread(controller_process.proc.wait, 15)
        assert returncode == ExitCode.OK, controller_process.output()
        assert agent.end_code is grpc.StatusCode.OK  # closed, not torn down
        rec = await registry.get_session(session_id)
        assert rec is not None
        assert rec.end_reason is SessionEndReason.CONTROLLER_SHUTDOWN
        output = controller_process.output()
        events = [
            json.loads(line)["event"]
            for line in output.splitlines()
            if line.startswith("{")
        ]
        assert "controller.stop_requested" in events
        assert "controller.stopped" in events
    finally:
        await agent.close()
        await channel.close()


def test_unreachable_database_exits_with_typed_code(tmp_path: Path) -> None:
    """``run_controller`` on a dead DB: typed error, no traceback, its exit code."""
    dead = DatabaseSettings(
        host="127.0.0.1",
        port=free_port_pair()[0],  # nothing listens here
        name="nobody",
        user="nobody",
        password=SecretStr("nobody"),
        connect_timeout_s=2,
    )
    code = run_controller(make_settings(dead))
    assert code == ExitCode.FAILURE  # DatabaseUnavailableError (retryable) -> FAILURE
