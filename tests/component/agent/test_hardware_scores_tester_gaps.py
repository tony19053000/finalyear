"""Independent tester round, G7 (0.3.4): two gaps the priority attack list called
out that the implementer's own suite does not cover.

1. **Truthfulness / GPU index cross-check** - D31 says a GPU metric's
   ``gpu_index`` "matches ``HardwareSummary.Gpu.index``", but
   ``session/limits.py::check_hardware_scores`` never reads ``Hello.hardware`` -
   it only bounds ``gpu_index <= 1023``. This reproduces an ``OK`` GPU score for
   an index the node never declared in its own ``Hello.hardware.gpus`` (here:
   no GPUs at all) being accepted, stored and served by REST as "measured".
   Documents current (permissive) behaviour - report as a defect, do not silently
   treat it as a guard test.

2. **The DB-refused write gap the G7-B handoff flagged as known**: a
   ``set_hardware_scores`` failure on the registry is logged by the
   ``WriteChain`` (``registry.write_failed``) and dropped - the report was
   already acked, so the agent will not resend it until its *next* ``Hello``
   (a fresh session). Confirms the node is stuck "pending" in the meantime, even
   though the agent believes the run was delivered.

Real controller + real PostgreSQL + a real agent client, like the sibling file.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from mf_agent_harness import RunningAgent, make_agent_settings
from mf_bench_fakes import make_scoreset, ok_metric
from mf_session_client import poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import httpx
import structlog
from mf_session_client import make_hello

from meshforge.agent.benchmarks.model import MetricName
from meshforge.agent.benchmarks.service import session_reporter
from meshforge.agent.benchmarks.wire import to_proto
from meshforge.agent.identity import NodeIdentity
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.hello import basic_hardware
from meshforge.agent.session.state import AgentState
from meshforge.common.capabilities import HARDWARE_SCORES_CAPABILITY
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.session.manager import Accepted, SessionManager

pytestmark = [pytest.mark.component, pytest.mark.docker]

ONLINE_TIMEOUT_S = 10.0
TIMEOUT_S = 10.0


def _hardware() -> Any:
    # basic_hardware() reports zero GPUs - the node never claims to own a GPU.
    hardware = basic_hardware()
    hardware.capabilities.append(HARDWARE_SCORES_CAPABILITY)
    return hardware


def _agent(target: str, identity: NodeIdentity, data_dir: Any) -> RunningAgent:
    settings = make_agent_settings(data_dir, target)
    client = AgentSessionClient(settings, identity, hardware=_hardware())
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
    rest: httpx.AsyncClient, node_id: str, status: str
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def reached() -> bool:
        seen.update(await _scores(rest, node_id))
        return seen["status"] == status

    await poll_until(reached, timeout_s=TIMEOUT_S)
    return seen


async def _stored(registry: Registry, node_id: str) -> Mapping[str, Any] | None:
    node = await registry.get_node(node_id)
    assert node is not None
    return node.hardware_scores


async def _stop(running: RunningAgent) -> None:
    with contextlib.suppress(BaseException):
        await running.stop()


# ---------------------------------------- gap 1: GPU index never cross-checked


@pytest.mark.asyncio
async def test_a_gpu_score_for_a_gpu_the_node_never_declared_is_accepted_as_measured(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Any,
    registry: Registry,
) -> None:
    """DEFECT REPRODUCTION (not a guard test): ``Hello.hardware.gpus`` is empty
    (``basic_hardware()``), yet an ``OK`` ``gpu_fp32_tflops`` score for
    ``gpu_index=0`` is accepted by ``check_hardware_scores``, stored, and served
    by ``/v1/nodes/{id}`` as ``measured`` with a real-looking TFLOP/s number -
    for a GPU this node's own enrolment says it does not have. Report severity:
    see the tester's final report; this asserts *today's* behaviour so a future
    fix is a visible test change, not a silent regression.
    """
    running = _agent(grpc_target, identity, data_dir)
    node_id = str(identity.node_id)
    try:
        await running.client.wait_for_state(
            AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S
        )
        assert basic_hardware().gpus == []  # sanity: this node has no GPUs

        phantom_gpu_run = make_scoreset(
            metrics=(
                ok_metric(MetricName.CPU_FP32_GFLOPS_1T, 97.0),
                ok_metric(MetricName.MEM_TRIAD_GB_S, 19.0),
                ok_metric(
                    MetricName.GPU_FP32_TFLOPS, mean=12.3, gpu_index=0
                ),  # a phantom GPU 0
            )
        )
        async with _rest(runtime) as rest:
            await session_reporter(running.client)(phantom_gpu_run)
            scores = await _wait_scores(rest, node_id, "measured")
            gpu_metrics = [m for m in scores["metrics"] if m["gpu_index"] is not None]
            assert len(gpu_metrics) == 1
            # Today: accepted as a genuine measurement of a GPU that does not
            # exist on this node, per Hello.hardware.
            assert gpu_metrics[0]["status"] == "ok"
            assert gpu_metrics[0]["mean"] == 12.3
        node = await registry.get_node(node_id)
        assert node is not None and node.hardware.get("gpus", []) == []
    finally:
        await _stop(running)


# ------------------------------------ gap 2: a DB-refused write is lost until Hello


class _FailingOnceRegistry:
    """The real registry, whose ``set_hardware_scores`` always raises once armed."""

    def __init__(self, inner: Registry) -> None:
        self._inner = inner
        self.fail = False
        self.attempts = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def set_hardware_scores(
        self,
        node_id: str,
        document: Mapping[str, Any],
        *,
        session_id: str,
        measured_at: dt.datetime,
    ) -> Any:
        self.attempts += 1
        if self.fail:
            raise RuntimeError("simulated database outage")
        return await self._inner.set_hardware_scores(
            node_id, document, session_id=session_id, measured_at=measured_at
        )


@pytest.mark.asyncio
async def test_a_db_refused_score_write_is_lost_until_the_next_hello(
    runtime: ControllerRuntime,  # unused directly: forces migrations to run first
    registry: Registry,
) -> None:
    """Confirms the G7-B handoff's stated known gap: the report is acked (the
    controller's contract with the wire protocol is "I received this", not "I
    persisted this" - see ``WriteChain`` module docstring), but the write itself
    fails and is only logged - the node is stuck without a stored run until a
    fresh ``Hello`` (the agent's own cache is the only thing that can re-deliver
    it, and only on a new session). A standalone :class:`SessionManager` over a
    failing registry wrapper, driven directly (no real socket) - the same shape
    as the sibling file's ``_GatedRegistry`` fence test.
    """
    failing = _FailingOnceRegistry(registry)
    manager = SessionManager(
        failing,  # type: ignore[arg-type]
        LivenessConfig(heartbeat_interval_s=30.0),
        clock=asyncio.get_running_loop().time,
        half_close_confirm_s=0.0,
    )
    node_id = new_node_id()
    run = make_scoreset()
    try:
        accepted = await manager.admit(make_hello(node_id, 1), "ipv4:127.0.0.1:1")
        assert isinstance(accepted, Accepted)
        live = accepted.live
        failing.fail = True

        message_id = "cmd_01JQZ00000000000000000000B"
        with structlog.testing.capture_logs() as logs:
            await manager.handle_message(
                live,
                agent_pb2.AgentMessage(
                    message_id=message_id,
                    session_id=live.session_id,
                    hardware_scores=to_proto(run),
                ),
            )
            # handle_message returned without raising: the message is
            # considered received and would be acked on the real wire (the
            # write itself is fire-and-forget on the session's WriteChain).
            # Replaying the identical message_id must be a no-op duplicate,
            # never a second attempt - proving the message really was accepted.
            await manager.handle_message(
                live,
                agent_pb2.AgentMessage(
                    message_id=message_id,
                    session_id=live.session_id,
                    hardware_scores=to_proto(run),
                ),
            )
            assert live.duplicates == 1

            async def written_or_failed() -> bool:
                return failing.attempts >= 1

            await poll_until(written_or_failed, timeout_s=TIMEOUT_S)

        failed = [e for e in logs if e["event"] == "registry.write_failed"]
        assert len(failed) == 1
        assert failed[0]["op"] == "set_hardware_scores"

        stored = await _stored(registry, node_id)
        assert stored is None  # the real row was never touched
        assert failing.attempts == 1  # exactly one attempt: no retry exists
    finally:
        await manager.shutdown(drain_s=1.0)
