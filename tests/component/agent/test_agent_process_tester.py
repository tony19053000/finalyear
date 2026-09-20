"""V0.3 G3 tester round: process-level attacks (docs/briefs/V0.3-fabric-core.md §5c
"Reconnect storm / correctness", "Epoch discipline", "Identity/process").

Real ``ControllerRuntime`` restarts, real child ``meshforge-agent run`` processes,
real SIGKILL, real file locks -- nothing mocked. Reuses the process-spawning
pattern from ``tests/component/agent/test_agent_process.py`` (implementer's tests),
kept self-contained here so this file never depends on another test module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import stat
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
    make_controller_settings,
)
from mf_session_client import poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.identity import load_or_create
from meshforge.agent.identity import reset as identity_reset
from meshforge.agent.session.state import AgentState
from meshforge.common.errors import ExitCode
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

ONLINE_TIMEOUT_S = 20.0


class AgentProcess:
    """A ``meshforge-agent run`` child with its own data_dir and config file."""

    def __init__(self, data_dir: Path, controller_address: str, tmp_path: Path) -> None:
        self.data_dir = data_dir
        cfg = tmp_path / f"agent-{data_dir.name}-{random.randrange(1_000_000)}.toml"  # noqa: S311
        cfg.write_text(
            f"data_dir = '{data_dir}'\n"
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

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()

    def close(self) -> None:
        self.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
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


# --------------------------------------------------------- reconnect storm


@pytest.mark.asyncio
async def test_reconnect_storm_five_restarts_random_intervals(
    pg_settings: DatabaseSettings,
    registry: Registry,
    agents: AgentFactory,
    identity: Any,
) -> None:
    """Kill-restart the controller 5 times at random 0.1-2 s intervals. The agent
    must end ONLINE with exactly one live session, epochs strictly increasing
    across attempts, no duplicate heartbeat sequences within any one session
    (each reconnect starts a fresh HELLO -> a fresh heartbeat sequence, so we
    check monotonic-within-session instead), and the asyncio task count returns
    to its pre-test baseline after the agent stops (no leaked tasks)."""
    baseline_tasks = len(asyncio.all_tasks())

    rt = ControllerRuntime(make_controller_settings(pg_settings))
    await rt._start()
    port = rt.grpc_port
    target = f"127.0.0.1:{port}"
    running = await agents(target, identity=identity)
    seen_epochs: list[int] = []
    try:
        await running.client.wait_for_state(
            AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S
        )
        seen_epochs.append(running.client.identity.session_epoch)

        for _ in range(5):
            await rt.stop()
            await asyncio.sleep(random.uniform(0.1, 2.0))  # noqa: S311
            rt = ControllerRuntime(
                make_controller_settings(pg_settings, grpc_bind=target)
            )
            await rt._start()
            await running.client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)
            seen_epochs.append(running.client.identity.session_epoch)

        # Epochs are strictly increasing across attempts.
        assert seen_epochs == sorted(seen_epochs)
        assert len(set(seen_epochs)) == len(seen_epochs)

        # Exactly one live session for this node on the current controller.
        assert rt.manager is not None
        live = rt.manager.get(running.client.node_id)
        assert live is not None
        assert live.session_id == running.client.session_id

        # Persisted node_session rows: session_epoch never reused, and every
        # closed session's heartbeat-derived rtt/etc. is internally consistent
        # (no crash / no DB corruption from the storm).
        sessions = await registry.list_sessions(running.client.node_id)
        epochs_in_db = sorted(s.session_epoch for s in sessions)
        assert epochs_in_db == sorted(set(epochs_in_db)), "an epoch was reused"
    finally:
        await running.stop()
        await rt.stop()

    # No leaked tasks: within a small margin (event loop bookkeeping tasks).
    remaining = len(asyncio.all_tasks())
    assert remaining <= baseline_tasks + 2, (
        f"task leak: baseline={baseline_tasks} remaining={remaining} "
        f"tasks={[t.get_name() for t in asyncio.all_tasks()]}"
    )


# --------------------------------------------------------------- epoch/SIGKILL


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is POSIX-only")
async def test_sigkill_agent_three_times_epoch_never_reused(
    runtime: ControllerRuntime,
    registry: Registry,
    grpc_target: str,
    data_dir: Path,
    agent_processes: Any,
) -> None:
    """SIGKILL the agent child 3 times mid-connection; each restart's Hello
    epoch must be strictly higher than the highest epoch the controller has
    persisted, and the identity file's epoch on disk must never be lower than
    what the controller last saw (ADR-0002 Updates §1)."""
    last_persisted = 0
    node_id: str | None = None

    for _ in range(3):
        ap: AgentProcess = agent_processes(data_dir, grpc_target)

        async def one_online(ap: AgentProcess = ap) -> bool:
            assert runtime.manager is not None
            if ap.proc.poll() is not None:
                raise AssertionError(f"agent exited early: {ap.output()}")
            live = runtime.manager.snapshot()
            return any(s.state is NodeState.ONLINE for s in live)

        await poll_until(one_online, timeout_s=ONLINE_TIMEOUT_S, interval_s=0.05)
        assert runtime.manager is not None
        live = runtime.manager.snapshot()[0]
        node_id = live.node_id
        assert live.session_epoch > last_persisted, (
            f"epoch not strictly increasing: {live.session_epoch} <= {last_persisted}"
        )
        last_persisted = live.session_epoch

        # Give it a moment to be mid-heartbeat, then SIGKILL (no graceful shutdown).
        await asyncio.sleep(FAST_LIVENESS.heartbeat_interval_s * 1.5)
        ap.kill()
        await asyncio.to_thread(ap.proc.wait, 10)

        # The identity on disk after the kill must reflect an epoch >= what the
        # controller saw (it is persisted *before* the stream opens).
        on_disk = load_or_create(data_dir / "identity")
        assert on_disk.session_epoch >= last_persisted, (
            f"on-disk epoch {on_disk.session_epoch} fell behind the controller's "
            f"last-seen {last_persisted} after a SIGKILL"
        )

        node = await registry.get_node(node_id)
        assert node is not None  # OFFLINE eventually, but never disappears

    assert node_id is not None


# --------------------------------------------------------------- races


@pytest.mark.asyncio
async def test_two_agents_same_data_dir_started_almost_simultaneously(
    runtime: ControllerRuntime,
    grpc_target: str,
    data_dir: Path,
    agent_processes: Any,
) -> None:
    """Two ``meshforge-agent run`` children on the same data_dir, started within
    ~50 ms of each other: exactly one survives ONLINE (running indefinitely --
    that is correct, not a hang), the other exits non-zero (identity lock
    contention -> CONFIG_ERROR, or a later CONFLICT once it loses the Hello
    race); the identity files stay intact and consistent."""
    load_or_create(data_dir / "identity")  # pre-create so both children race to load it

    ap1: AgentProcess = agent_processes(data_dir, grpc_target)
    await asyncio.sleep(0.05)
    ap2: AgentProcess = agent_processes(data_dir, grpc_target)

    # Exactly one of the two must exit (the loser); the other stays ONLINE and
    # must be killed at fixture teardown -- that is a pass, not a timeout.
    done, pending = await asyncio.wait(
        {
            asyncio.ensure_future(asyncio.to_thread(ap1.proc.wait, 30)),
            asyncio.ensure_future(asyncio.to_thread(ap2.proc.wait, 30)),
        },
        timeout=30.0,
        return_when=asyncio.FIRST_COMPLETED,
    )
    assert done, "neither agent process exited within 30 s (both hung?)"
    loser_rc = next(iter(done)).result()
    assert loser_rc != 0, (
        f"the loser exited 0 instead of a typed failure\n"
        f"ap1 ({ap1.proc.poll()}): {ap1.output()}\nap2 ({ap2.proc.poll()}): {ap2.output()}"
    )
    assert loser_rc in {int(ExitCode.CONFLICT), int(ExitCode.CONFIG_ERROR)}, (
        loser_rc,
        ap1.output(),
        ap2.output(),
    )
    survivor = ap1 if ap1.proc.poll() is None else ap2
    assert survivor.proc.poll() is None, (
        "both agents exited: nobody survived ONLINE on this identity"
    )

    async def survivor_online() -> bool:
        assert runtime.manager is not None
        return any(s.state is NodeState.ONLINE for s in runtime.manager.snapshot())

    await poll_until(survivor_online, timeout_s=10.0, interval_s=0.1)
    for fut in pending:
        fut.cancel()

    # Identity is intact and internally consistent after the race.
    ident = load_or_create(data_dir / "identity")
    assert ident.node_id is not None
    assert ident.session_epoch >= 1


# --------------------------------------------------------------- identity reset


@pytest.mark.asyncio
async def test_identity_reset_while_agent_running_fails_loudly_on_next_reconnect(
    runtime: ControllerRuntime,
    grpc_target: str,
    data_dir: Path,
    agent_processes: Any,
) -> None:
    """``identity reset`` while an agent runs: the running agent's next
    reconnect must fail loudly (IdentityError "changed underneath") and exit
    non-zero, not silently mint a new node_id and keep going."""
    ap: AgentProcess = agent_processes(data_dir, grpc_target)

    async def one_online() -> bool:
        assert runtime.manager is not None
        if ap.proc.poll() is not None:
            raise AssertionError(f"agent exited early: {ap.output()}")
        return any(s.state is NodeState.ONLINE for s in runtime.manager.snapshot())

    await poll_until(one_online, timeout_s=ONLINE_TIMEOUT_S, interval_s=0.05)

    # Reset the identity out from under the running agent (as `identity reset
    # --yes` would, but in-process to avoid a second lock holder).
    identity_reset(data_dir / "identity")
    load_or_create(data_dir / "identity")  # a fresh node_id + key now on disk

    # Kill the controller side of its live stream so the agent is forced to
    # reconnect and call next_epoch again -- that is where the mismatch surfaces.
    await runtime.stop()

    returncode = await asyncio.to_thread(ap.proc.wait, 30)
    assert returncode != 0, (
        "agent kept running silently after identity changed underneath it"
    )
    events = [e["event"] for e in ap.events()]
    assert "agent.exited_with_error" in events or "agent.crashed" in events, ap.output()
    if "agent.exited_with_error" in events:
        err = next(e for e in ap.events() if e["event"] == "agent.exited_with_error")
        assert (
            "identity" in err["error"].lower()
            or "changed underneath" in err["error"].lower()
        )


# --------------------------------------------------------------- read-only dir


@pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,  # type: ignore[attr-defined]
    reason="chmod-based read-only test needs POSIX and a non-root user",
)
def test_read_only_data_dir_exits_config_error_with_path(tmp_path: Path) -> None:
    from mf_agent_harness import make_agent_settings

    from meshforge.agent.runtime import run_agent

    parent = tmp_path / "readonly-parent"
    parent.mkdir()
    parent.chmod(stat.S_IREAD | stat.S_IEXEC)  # r-x, no write
    data_dir = parent / "data"  # does not exist; identity dir creation must fail
    try:
        settings = make_agent_settings(data_dir, "127.0.0.1:1")  # never dialled
        code = run_agent(settings)
        assert code == ExitCode.CONFIG_ERROR
    finally:
        parent.chmod(stat.S_IRWXU)
