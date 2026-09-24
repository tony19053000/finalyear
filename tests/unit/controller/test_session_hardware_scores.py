"""G7-B (0.3.4, D37/D38): the session manager's side of hardware scores.

* a Hello's valid scores are stored with the admission; invalid ones are dropped
  (logged ``session.hardware_scores_rejected``, counted) and the Hello is still
  accepted with ``NULL`` - a bad optional field never refuses enrolment;
* a ``hardware_scores`` report is validated, acked either way, and a valid one is
  queued on the session's write chain as a fenced write carrying *this* session id
  and the run's ``measured_at``; a duplicate ``message_id`` is written once.

Fakes only (no database); the fence and the ordering against real PostgreSQL are in
tests/component/controller/test_hardware_scores.py.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from typing import Any

import pytest
from mf_bench_fakes import make_scoreset
from structlog.testing import capture_logs
from test_session_manager import (
    FakeClock,
    FakeRegistry,
    admit,
    drain,
    hello,
    make_manager,
    mid,
    settle,
)

from meshforge.agent.benchmarks.wire import to_proto
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import ScoresWrite
from meshforge.controller.session.manager import LiveSession

pytestmark = pytest.mark.unit


class ScoresRegistry(FakeRegistry):
    async def set_hardware_scores(
        self,
        node_id: str,
        document: Mapping[str, Any],
        *,
        session_id: str,
        measured_at: dt.datetime,
    ) -> ScoresWrite:
        self._record(
            "set_hardware_scores",
            node_id,
            dict(document),
            session_id=session_id,
            measured_at=measured_at,
        )
        return ScoresWrite.WRITTEN


def _report(live: LiveSession, label: str, scores: agent_pb2.HardwareScores) -> Any:
    return agent_pb2.AgentMessage(
        message_id=mid(label),
        session_id=live.session_id,
        hardware_scores=scores,
    )


def _bad() -> agent_pb2.HardwareScores:
    scores = to_proto(make_scoreset())
    scores.scores[0].mean = math.nan
    return scores


@pytest.mark.asyncio
async def test_hello_scores_are_stored_with_the_admission() -> None:
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    run = make_scoreset()
    h = hello(new_node_id())
    h.hardware_scores.CopyFrom(to_proto(run))
    live = await admit(manager, h)
    stored = registry.nodes[live.node_id].hardware_scores
    assert stored is not None and stored["run_id"] == run.run_id
    assert live.hardware_scores_received == 1


@pytest.mark.asyncio
async def test_hello_without_scores_stores_null() -> None:
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    live = await admit(manager, hello(new_node_id()))
    assert registry.nodes[live.node_id].hardware_scores is None


@pytest.mark.asyncio
async def test_hello_with_invalid_scores_is_accepted_with_null() -> None:
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    h = hello(new_node_id())
    h.hardware_scores.CopyFrom(_bad())
    with capture_logs() as logs:
        live = await admit(manager, h)
    assert registry.nodes[live.node_id].hardware_scores is None
    assert live.hardware_scores_rejected == 1
    assert manager.hardware_scores_rejected_total == 1
    rejected = [e for e in logs if e["event"] == "session.hardware_scores_rejected"]
    assert rejected and rejected[0]["source"] == "hello"
    assert rejected[0]["node_id"] == live.node_id
    assert rejected[0]["session_id"] == live.session_id
    assert "scores[0].mean" in rejected[0]["reason"]


@pytest.mark.asyncio
async def test_a_valid_report_is_a_fenced_ordered_write_and_acked() -> None:
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    live = await admit(manager, hello(new_node_id()))
    run = make_scoreset()
    msg = _report(live, "s-1", to_proto(run))
    await manager.handle_message(live, msg)
    await manager.handle_message(live, msg)  # a replayed duplicate
    await settle()
    calls = registry.calls_named("set_hardware_scores")
    assert len(calls) == 1, "a duplicate message_id must be written once"
    args, kwargs = calls[0]
    assert args[0] == live.node_id and args[1]["run_id"] == run.run_id
    assert kwargs["session_id"] == live.session_id
    assert kwargs["measured_at"] == run.measured_at
    acks = [m for m in drain(live) if m is not None and m.HasField("ack")]
    assert [a.ack.acked_message_id for a in acks] == [mid("s-1")] * 2
    assert live.hardware_scores_received == 1


@pytest.mark.asyncio
async def test_an_invalid_report_is_dropped_acked_and_the_stream_stays() -> None:
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    live = await admit(manager, hello(new_node_id()))
    with capture_logs() as logs:
        await manager.handle_message(live, _report(live, "bad-1", _bad()))
    await settle()
    assert registry.calls_named("set_hardware_scores") == []
    assert not live.ended
    assert manager.get(live.node_id) is live
    assert live.hardware_scores_rejected == 1
    acks = [m for m in drain(live) if m is not None and m.HasField("ack")]
    assert [a.ack.acked_message_id for a in acks] == [mid("bad-1")]
    rejected = [e for e in logs if e["event"] == "session.hardware_scores_rejected"]
    assert rejected and rejected[0]["source"] == "report"
    assert rejected[0]["session_id"] == live.session_id
