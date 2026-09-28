"""G7-B (0.3.4, D37/D38) component tests: hardware scores on the fabric, for real.

Real PostgreSQL, the real :class:`ControllerRuntime` (gRPC + uvicorn on loopback),
the real :class:`AgentSessionClient`, real HTTP. Failures are injected for real: the
controller is stopped and restarted, a session is ended under the agent, a registry
write is held on an ``asyncio.Event`` while a newer session is admitted.

Lives in ``tests/component/agent`` because it needs the real agent fixtures. Nothing
asserts this machine's hardware: score sets are fabricated with the agent's own
model and sent through the agent's own converter.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import math
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from mf_agent_harness import RunningAgent, make_agent_settings, make_controller_settings
from mf_bench_fakes import make_scoreset
from mf_session_client import make_hello, poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import httpx
import structlog
from sqlalchemy import text

from meshforge.agent.benchmarks.model import ScoreSet
from meshforge.agent.benchmarks.service import session_reporter
from meshforge.agent.benchmarks.wire import to_proto
from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.hello import basic_hardware
from meshforge.agent.session.state import AgentState
from meshforge.common.capabilities import HARDWARE_SCORES_CAPABILITY
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, NodeState
from meshforge.controller.registry import Registry, ScoresWrite, SessionEndReason
from meshforge.controller.scores import scores_document
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.session.manager import Accepted, SessionManager
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

ONLINE_TIMEOUT_S = 10.0
TIMEOUT_S = 10.0
T0 = dt.datetime(2026, 9, 24, 12, 0, 0, 123456, tzinfo=dt.UTC)


def _hardware(*, scores_capable: bool = True) -> agent_pb2.HardwareSummary:
    hardware = basic_hardware()
    # D39: make_scoreset() names GPU 0, so the node must declare it.
    hardware.gpus.append(agent_pb2.HardwareSummary.Gpu(index=0, model="test-gpu"))
    if scores_capable:
        hardware.capabilities.append(HARDWARE_SCORES_CAPABILITY)
    return hardware


def _agent(
    target: str,
    identity: NodeIdentity,
    data_dir: Any,
    *,
    hello_scores: ScoreSet | None = None,
    scores_capable: bool = True,
) -> RunningAgent:
    settings = make_agent_settings(data_dir, target)
    client = AgentSessionClient(
        settings,
        identity,
        hardware=_hardware(scores_capable=scores_capable),
        hardware_scores=None if hello_scores is None else to_proto(hello_scores),
    )
    return RunningAgent(client)


@contextlib.asynccontextmanager
async def _rest(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}", timeout=10.0
    ) as client:
        yield client


async def _scores(rest: httpx.AsyncClient, node_id: str) -> dict[str, Any]:
    response = await rest.get(f"/v1/nodes/{node_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body["scores"]


async def _wait_scores(
    rest: httpx.AsyncClient, node_id: str, status: str, run_id: str | None = None
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def reached() -> bool:
        seen.update(await _scores(rest, node_id))
        return seen["status"] == status and (run_id is None or seen["run_id"] == run_id)

    await poll_until(reached, timeout_s=TIMEOUT_S)
    return seen


async def _stored(registry: Registry, node_id: str) -> Mapping[str, Any] | None:
    node = await registry.get_node(node_id)
    assert node is not None
    return node.hardware_scores


async def _stop(running: RunningAgent) -> None:
    with contextlib.suppress(BaseException):
        await running.stop()


# ------------------------------------------------------------ pending -> measured


@pytest.mark.asyncio
async def test_fresh_agent_is_pending_then_a_report_makes_it_measured(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Any,
    registry: Registry,
) -> None:
    running = _agent(grpc_target, identity, data_dir)
    node_id = str(identity.node_id)
    try:
        await running.client.wait_for_state(
            AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S
        )
        async with _rest(runtime) as rest:
            assert (await _scores(rest, node_id))["status"] == "pending"
            listed = (await rest.get("/v1/nodes")).json()["nodes"]
            assert [n["scores"]["status"] for n in listed] == ["pending"]

            run = make_scoreset()
            await session_reporter(running.client)(run)
            scores = await _wait_scores(rest, node_id, "measured", run.run_id)
            assert scores["measured_at"] == "2026-09-24T12:00:00.123456+00:00"
            assert [m["name"] for m in scores["metrics"]] == [
                "cpu_fp32_gflops_1t",
                "mem_triad_gb_s",
                "gpu_fp32_tflops",
            ]
            assert scores["metrics"][0]["mean"] == 97.31354540848592  # bit-exact
            assert scores["metrics"][2]["mean"] is None  # unknown stays null
            listed = (await rest.get("/v1/nodes")).json()["nodes"]
            assert listed[0]["scores"]["run_id"] == run.run_id

        async def acked() -> bool:
            return len(running.client.ring) == 0

        await poll_until(acked, timeout_s=TIMEOUT_S)
    finally:
        await _stop(running)


@pytest.mark.asyncio
async def test_a_node_that_does_not_score_is_none(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Any,
) -> None:
    running = _agent(grpc_target, identity, data_dir, scores_capable=False)
    try:
        await running.client.wait_for_state(
            AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S
        )
        async with _rest(runtime) as rest:
            assert (await _scores(rest, str(identity.node_id)))["status"] == "none"
    finally:
        await _stop(running)


# ------------------------------------------------ controller restart -> Hello restores


@pytest.mark.asyncio
async def test_controller_restart_hello_restores_scores_and_no_scores_clears(
    pg_settings: DatabaseSettings,
    identity: NodeIdentity,
    data_dir: Any,
    registry: Registry,
) -> None:
    first = ControllerRuntime(make_controller_settings(pg_settings))
    await first._start()
    port = first.grpc_port
    target = f"127.0.0.1:{port}"
    node_id = str(identity.node_id)
    run = make_scoreset()
    running = _agent(target, identity, data_dir, hello_scores=run)
    engine = create_engine(pg_settings)
    try:
        await running.client.wait_for_state(
            AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S
        )
        async with _rest(first) as rest:
            await _wait_scores(rest, node_id, "measured", run.run_id)
        await first.stop()
        await running.client.wait_for_state(
            AgentState.BACKOFF,
            AgentState.DISCOVERING,
            AgentState.CONNECTING,
            timeout_s=5.0,
        )
        # Wipe the stored copy while no controller runs: only the Hello can restore it.
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE node SET hardware_scores = NULL WHERE node_id = :n"),
                {"n": node_id},
            )
        assert await _stored(registry, node_id) is None
        async with ControllerRuntime.start(
            make_controller_settings(pg_settings, grpc_bind=target)
        ) as second:
            await running.client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)
            async with _rest(second) as rest:
                await _wait_scores(rest, node_id, "measured", run.run_id)

            # The same node restarts with no valid cache (new hardware): the Hello
            # without scores clears the column - pending, never the old numbers.
            await _stop(running)
            running = _agent(
                target,
                load_or_create(data_dir / "identity"),
                data_dir,
                hello_scores=None,
            )
            await running.client.wait_for_state(
                AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S
            )
            async with _rest(second) as rest:
                await _wait_scores(rest, node_id, "pending")
            async with engine.connect() as conn:
                is_sql_null = await conn.scalar(
                    text("SELECT hardware_scores IS NULL FROM node WHERE node_id = :n"),
                    {"n": node_id},
                )
            assert is_sql_null is True  # SQL NULL, not the JSON document 'null'

    finally:
        await _stop(running)
        await first.stop()
        await engine.dispose()


# ---------------------------- replay: scores between two resource updates (G4 R1)


@pytest.mark.asyncio
async def test_replayed_scores_between_resource_updates_lose_and_duplicate_nothing(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Any,
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The report sequence is shared: RU(seq 1), HS(seq 2), RU(seq 3). The acks are
    lost (the agent ignores them), the stream ends under the agent with the
    controller up, the agent reconnects and replays all three. The resource
    cursor must dedupe RU 1 and RU 3 (no duplicate sample), accept RU 4 (nothing
    lost behind the scores' sequence gap) and the scores must be stored as the
    same single run."""
    running = _agent(grpc_target, identity, data_dir)
    client = running.client
    node_id = str(identity.node_id)
    manager = runtime.manager
    tracker = runtime.resources
    assert manager is not None and tracker is not None
    try:
        await client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
        first_session = client.session_id
        acks_lost = [True]
        real_ack = client.ring.ack

        def ack(message_id: str) -> bool:
            return False if acks_lost[0] else real_ack(message_id)

        monkeypatch.setattr(client.ring, "ack", ack)  # the controller's acks "lost"

        await client.send_report(
            "resource_update", agent_pb2.ResourceUpdate(cpu_percent=1.0)
        )
        run = make_scoreset()
        await session_reporter(client)(run)
        await client.send_report(
            "resource_update", agent_pb2.ResourceUpdate(cpu_percent=3.0)
        )
        assert [e.sequence for e in client.ring.pending()] == [1, 2, 3]
        assert [e.message.WhichOneof("payload") for e in client.ring.pending()] == [
            "resource_update",
            "hardware_scores",
            "resource_update",
        ]

        def ring_sequences() -> list[int]:
            ring = tracker.ring(node_id)
            return [] if ring is None else [s.sequence for s in ring.snapshot()]

        async def first_pass_landed() -> bool:
            stored = await _stored(registry, node_id)
            return ring_sequences() == [1, 3] and stored is not None

        await poll_until(first_pass_landed, timeout_s=TIMEOUT_S)
        duplicates_before = tracker.duplicates
        acks_lost[0] = False  # acks work again from here on

        live = manager.get(node_id)
        assert live is not None
        await manager.end_session(live, SessionEndReason.STREAM_LOST)

        async def reconnected() -> bool:
            return (
                client.state is AgentState.ONLINE
                and client.session_id not in ("", first_session)
                and client.stats.replayed >= 3
            )

        await poll_until(reconnected, timeout_s=15.0)
        await client.send_report(
            "resource_update", agent_pb2.ResourceUpdate(cpu_percent=4.0)
        )

        async def settled() -> bool:
            return ring_sequences() == [1, 3, 4] and len(client.ring) == 0

        await poll_until(settled, timeout_s=TIMEOUT_S)
        assert tracker.duplicates == duplicates_before + 2  # RU 1 and RU 3 deduped
        stored = await _stored(registry, node_id)
        assert stored is not None and stored["run_id"] == run.run_id
        node = await registry.get_node(node_id)
        assert node is not None and node.state is NodeState.ONLINE
    finally:
        await _stop(running)


# ------------------------------------------------------------- malformed -> dropped


@pytest.mark.asyncio
async def test_malformed_scores_are_dropped_and_the_node_stays_online(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Any,
    registry: Registry,
) -> None:
    running = _agent(grpc_target, identity, data_dir)
    client = running.client
    node_id = str(identity.node_id)
    manager = runtime.manager
    assert manager is not None
    try:
        await client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
        session = client.session_id
        bad = to_proto(make_scoreset())
        bad.scores[0].mean = math.nan
        with structlog.testing.capture_logs() as logs:
            await client.send_report("hardware_scores", bad)

            async def acked() -> bool:
                return len(client.ring) == 0

            await poll_until(acked, timeout_s=TIMEOUT_S)
        rejected = [e for e in logs if e["event"] == "session.hardware_scores_rejected"]
        assert len(rejected) == 1 and "scores[0].mean" in rejected[0]["reason"]
        assert rejected[0]["node_id"] == node_id
        assert rejected[0]["session_id"] == session
        await asyncio.sleep(0.5)  # > 2 heartbeats of FAST_LIVENESS
        assert client.state is AgentState.ONLINE and client.session_id == session
        live = manager.get(node_id)
        assert live is not None and live.hardware_scores_rejected == 1
        node = await registry.get_node(node_id)
        assert node is not None and node.state is NodeState.ONLINE
        assert node.hardware_scores is None

        # A good report on the same stream still lands afterwards.
        good = make_scoreset()
        await session_reporter(client)(good)
        async with _rest(runtime) as rest:
            await _wait_scores(rest, node_id, "measured", good.run_id)
    finally:
        await _stop(running)


# ----------------------------------------------- the fence and the ordering (D38)


def _gpu_hello(node_id: str, epoch: int) -> agent_pb2.Hello:
    """A Hello declaring GPU 0 (D39: the score runs here name it)."""
    hello = make_hello(node_id, epoch)
    hello.hardware.gpus.append(agent_pb2.HardwareSummary.Gpu(index=0))
    return hello


class _GatedRegistry:
    """The real registry, with one session's ``set_hardware_scores`` held."""

    def __init__(self, inner: Registry) -> None:
        self._inner = inner
        self.hold_session: str | None = None
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.results: list[tuple[str, ScoresWrite]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def set_hardware_scores(
        self,
        node_id: str,
        document: Mapping[str, Any],
        *,
        session_id: str,
        measured_at: dt.datetime,
    ) -> ScoresWrite:
        if session_id == self.hold_session:
            self.held.set()
            await self.release.wait()
        outcome = await self._inner.set_hardware_scores(
            node_id, document, session_id=session_id, measured_at=measured_at
        )
        self.results.append((session_id, outcome))
        return outcome


@pytest.mark.asyncio
async def test_an_old_sessions_queued_scores_write_is_fenced_by_a_newer_hello(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """Hold session 1's scores write, admit session 2 whose Hello carries an
    *older* run, release. The held run is newer, so only the session fence can
    stop it: the row must keep session 2's run. Drop ``last_session_id`` from
    ``Registry.set_hardware_scores`` and this fails (mutation-checked)."""
    gated = _GatedRegistry(registry)
    manager = SessionManager(
        gated,  # type: ignore[arg-type]
        LivenessConfig(heartbeat_interval_s=30.0),
        clock=asyncio.get_running_loop().time,
    )
    node_id = new_node_id()
    newer = make_scoreset(measured_at=T0 + dt.timedelta(hours=1))
    older = make_scoreset(measured_at=T0)
    try:
        first = await manager.admit(_gpu_hello(node_id, 1), "ipv4:127.0.0.1:1")
        assert isinstance(first, Accepted)
        old = first.live
        gated.hold_session = old.session_id
        await manager.handle_message(
            old,
            agent_pb2.AgentMessage(
                message_id="cmd_01JQZ00000000000000000000A",
                session_id=old.session_id,
                hardware_scores=to_proto(newer),
            ),
        )
        await asyncio.wait_for(gated.held.wait(), 5.0)

        hello = _gpu_hello(node_id, 2)
        hello.hardware_scores.CopyFrom(to_proto(older))
        second = await manager.admit(hello, "ipv4:127.0.0.1:2")
        assert isinstance(second, Accepted)
        stored = await _stored(registry, node_id)
        assert stored is not None and stored["run_id"] == older.run_id

        with structlog.testing.capture_logs() as logs:
            gated.release.set()
            await poll_until(_results_len(gated, 1), timeout_s=5.0)
        assert gated.results == [(old.session_id, ScoresWrite.FENCED)]
        (skipped,) = [e for e in logs if e["event"] == "registry.scores_write_skipped"]
        assert skipped["outcome"] == "fenced"
        assert skipped["session_id"] == old.session_id
        stored = await _stored(registry, node_id)
        assert stored is not None and stored["run_id"] == older.run_id
    finally:
        await manager.shutdown(drain_s=1.0)


def _results_len(gated: _GatedRegistry, n: int) -> Any:
    async def check() -> bool:
        return len(gated.results) >= n

    return check


@pytest.mark.asyncio
async def test_an_older_run_never_replaces_a_newer_one_and_equal_is_idempotent(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """The ``measured_at`` guard against the real database. Invert or drop the
    comparison in ``Registry.set_hardware_scores`` and this fails."""
    manager = runtime.manager
    assert manager is not None
    node_id = new_node_id()
    accepted = await manager.admit(make_hello(node_id, 1), "ipv4:127.0.0.1:1")
    assert isinstance(accepted, Accepted)
    session_id = accepted.live.session_id

    async def write(run: ScoreSet) -> ScoresWrite:
        document, measured_at = scores_document(to_proto(run))
        return await registry.set_hardware_scores(
            node_id, document, session_id=session_id, measured_at=measured_at
        )

    newer = make_scoreset(measured_at=T0 + dt.timedelta(seconds=1))
    older = make_scoreset(measured_at=T0)
    micro_older = make_scoreset(
        measured_at=newer.measured_at - dt.timedelta(microseconds=1)
    )
    assert await write(newer) is ScoresWrite.WRITTEN
    assert await write(older) is ScoresWrite.OLDER
    assert await write(micro_older) is ScoresWrite.OLDER
    assert await write(newer) is ScoresWrite.WRITTEN  # a replay: same run again
    stored = await _stored(registry, node_id)
    assert stored is not None and stored["run_id"] == newer.run_id
    later = make_scoreset(measured_at=T0 + dt.timedelta(days=1))
    assert await write(later) is ScoresWrite.WRITTEN
    stored = await _stored(registry, node_id)
    assert stored is not None and stored["run_id"] == later.run_id
    # A session that never owned the node is fenced, not "older".
    document, measured_at = scores_document(
        to_proto(make_scoreset(measured_at=T0 + dt.timedelta(days=2)))
    )
    assert (
        await registry.set_hardware_scores(
            node_id,
            document,
            session_id="sess_01JQZ00000000000000000000A",
            measured_at=measured_at,
        )
        is ScoresWrite.FENCED
    )
