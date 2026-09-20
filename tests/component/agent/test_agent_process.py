"""V0.3 G3 component tests of the agent *process* and of process-level faults.

Real ``meshforge-agent run`` child processes against an in-process controller
runtime (signals, exit codes, a locked identity directory), and a controller
*child* that is SIGKILLed under a connected in-process agent, which must then
reconnect to a fresh runtime on the same port. Nothing is mocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mf_agent_harness import (
    AGENT_RECONNECT,
    FAST_LIVENESS,
    AgentFactory,
    make_agent_settings,
    make_controller_settings,
)
from mf_session_client import free_port_pair, poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent import identity as ident
from meshforge.agent.identity import load_or_create
from meshforge.agent.runtime import run_agent
from meshforge.agent.session.state import AgentState
from meshforge.common.errors import ExitCode
from meshforge.common.filelock import locked
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

ONLINE_TIMEOUT_S = 15.0


# ------------------------------------------------------------ agent child


class AgentProcess:
    """A ``meshforge-agent run`` child with its own data_dir and config file."""

    def __init__(self, data_dir: Path, controller_address: str, tmp_path: Path) -> None:
        self.data_dir = data_dir
        cfg = tmp_path / f"agent-{data_dir.name}.toml"
        cfg.write_text(
            f"data_dir = '{data_dir}'\n"  # top-level key: before any [table]
            "[log]\n"
            'format = "json"\n'
            'level = "INFO"\n'
            "[agent]\n"
            f'controller_address = "{controller_address}"\n'
            "hello_timeout_s = 2.0\n"
            "identity_lock_timeout_s = 0.5\n"
            "[agent.reconnect]\n"
            f"base_s = {AGENT_RECONNECT['base_s']}\n"
            f"cap_s = {AGENT_RECONNECT['cap_s']}\n",
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from meshforge.agent.main import main; main()",
                "run",
                "--config",
                str(cfg),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._output = ""

    def output(self) -> str:
        if self.proc.stdout is not None and not self._output:
            with contextlib.suppress(ValueError):
                self._output = self.proc.stdout.read()
        return self._output

    def events(self) -> list[dict[str, Any]]:
        return [
            json.loads(line)
            for line in self.output().splitlines()
            if line.startswith("{")
        ]

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)
        if self.proc.stdout is not None:
            self.proc.stdout.close()


@pytest.fixture
def agent_processes(tmp_path: Path) -> Iterator[Any]:
    made: list[AgentProcess] = []

    def factory(data_dir: Path, controller_address: str) -> AgentProcess:
        ap = AgentProcess(data_dir, controller_address, tmp_path)
        made.append(ap)
        return ap

    yield factory
    for ap in made:
        ap.close()


async def _wait_node_online(
    runtime: ControllerRuntime, child: AgentProcess, *, timeout_s: float
) -> str:
    """Wait for one node to be ONLINE in the runtime; return its node_id. Fails
    at once with the child's output if the agent process exits early."""
    assert runtime.manager is not None

    async def one_online() -> bool:
        if child.proc.poll() is not None:
            raise AssertionError(
                f"agent exited early ({child.proc.returncode}):\n{child.output()}"
            )
        live = runtime.manager.snapshot() if runtime.manager else []
        return any(s.state is NodeState.ONLINE for s in live)

    await poll_until(one_online, timeout_s=timeout_s, interval_s=0.05)
    live = runtime.manager.snapshot()
    return live[0].node_id


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32", reason="SIGTERM is not graceful on Windows"
)
async def test_sigterm_exits_zero_and_controller_records_agent_closed(
    runtime: ControllerRuntime,
    registry: Registry,
    grpc_target: str,
    data_dir: Path,
    agent_processes: Any,
) -> None:
    ap: AgentProcess = agent_processes(data_dir, grpc_target)
    node_id = await _wait_node_online(runtime, ap, timeout_s=ONLINE_TIMEOUT_S)
    assert runtime.manager is not None
    live = runtime.manager.get(node_id)
    assert live is not None
    session_id = live.session_id

    ap.proc.send_signal(signal.SIGTERM)
    returncode = await asyncio.to_thread(ap.proc.wait, 15)
    assert returncode == ExitCode.OK, ap.output()

    async def closed() -> bool:
        rec = await registry.get_session(session_id)
        return rec is not None and rec.end_reason is SessionEndReason.AGENT_CLOSED

    await poll_until(closed, timeout_s=5.0)
    events = [e["event"] for e in ap.events()]
    assert "agent.session.stop_requested" in events
    assert "agent.session.closed" in events
    assert "agent.exited" in events
    # Correlation fields on the session events; no key material anywhere.
    closed_evt = next(e for e in ap.events() if e["event"] == "agent.session.closed")
    assert closed_evt["node_id"] == node_id and closed_evt["session_id"] == session_id
    assert "PRIVATE KEY" not in ap.output()

    # Criterion 4 from the process side: the identity on disk is unchanged.
    assert str(load_or_create(data_dir / "identity").node_id) == node_id


@pytest.mark.asyncio
async def test_superseded_process_exits_conflict(
    runtime: ControllerRuntime,
    registry: Registry,
    grpc_target: str,
    data_dir: Path,
    agents: AgentFactory,
    agent_processes: Any,
) -> None:
    ap: AgentProcess = agent_processes(data_dir, grpc_target)
    node_id = await _wait_node_online(runtime, ap, timeout_s=ONLINE_TIMEOUT_S)

    # A second instance on the same identity directory (in-process) wins.
    second = await agents(grpc_target, identity=load_or_create(data_dir / "identity"))
    await second.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
    assert second.client.node_id == node_id

    returncode = await asyncio.to_thread(ap.proc.wait, 15)
    assert returncode == ExitCode.CONFLICT, ap.output()
    errors = [e for e in ap.events() if e["event"] == "agent.exited_with_error"]
    assert errors and errors[0]["failure_type"] == "conflict"
    assert "another meshforge-agent" in errors[0]["error"]
    assert second.client.state is AgentState.ONLINE


def _hold_lock(lock_path: str, held: Any, release: Any) -> None:
    with locked(Path(lock_path), timeout_s=5.0):
        held.set()
        release.wait(timeout=60)


def test_locked_identity_dir_exits_config_error(data_dir: Path) -> None:
    """Another process holds ``identity.lock``: ``run_agent`` fails with the
    IdentityError message and exit CONFIG_ERROR, without touching the network."""
    identity_dir = data_dir / "identity"
    load_or_create(identity_dir)
    ctx = multiprocessing.get_context("spawn")
    held, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(
        target=_hold_lock,
        args=(str(identity_dir / ident.LOCK_FILE_NAME), held, release),
    )
    proc.start()
    try:
        assert held.wait(timeout=30), "child never acquired the lock"
        settings = make_agent_settings(data_dir, "127.0.0.1:1")  # never dialled
        code = run_agent(settings)
        assert code == ExitCode.CONFIG_ERROR
    finally:
        release.set()
        proc.join(timeout=10)


# -------------------------------------------------------- controller child


class ControllerProcess:
    """A ``meshforge-controller`` child bound to two loopback ports (same pattern
    as tests/component/controller/test_controller_process.py)."""

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
            "[controller.discovery]\n"
            "advertise = false\n"
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


@pytest.mark.asyncio
async def test_controller_sigkill_then_fresh_runtime_agent_reconnects(
    controller_process: ControllerProcess,
    pg_settings: DatabaseSettings,
    registry: Registry,
    agents: AgentFactory,
) -> None:
    """The controller dies without running any shutdown code; the agent sees the
    transport fail, backs off, and joins the fresh controller on the same port."""
    running = await agents(controller_process.grpc_target)
    # The child needs a moment to migrate + bind; the agent retries until then.
    await running.client.wait_for_state(AgentState.ONLINE, timeout_s=30.0)
    first_session = running.client.session_id

    controller_process.kill()
    await running.client.wait_for_state(
        AgentState.BACKOFF,
        AgentState.DISCOVERING,
        AgentState.CONNECTING,
        timeout_s=10.0,
    )
    ends = running.client.stats.stream_ends
    assert any(e.code is not None and e.code.name == "UNAVAILABLE" for e in ends)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    async with ControllerRuntime.start(
        make_controller_settings(
            pg_settings, grpc_bind=f"127.0.0.1:{controller_process.grpc_port}"
        )
    ):
        await running.client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)
        assert loop.time() - t0 < 15.0
        assert running.client.session_id != first_session
        swept = await registry.get_session(first_session)
        assert swept is not None
        assert swept.end_reason is SessionEndReason.CONTROLLER_RESTART
        node = await registry.get_node(running.client.node_id)
        assert node is not None and node.state is NodeState.ONLINE
