"""V0.3 G3 (ADR-0002 Updates §4): replay ring bounds/order/ack semantics and the
controller-command dedupe window."""

from __future__ import annotations

import pytest

from meshforge.agent.session.replay import REPORT_PAYLOADS, RecentIds, ReplayRing
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


def report(seq: int) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(),
        resource_update=agent_pb2.ResourceUpdate(sequence=seq, cpu_percent=1.0),
    )


def test_report_payload_set_is_exactly_the_reports() -> None:
    assert {
        "resource_update",
        "hardware_scores",  # G7-B (D37): a report, buffered and replayed
        "cache_report",
        "net_probe_result",
        "attempt_status",
        "log_chunk",
        "prepare_env_status",  # V0.5 G5: acked, replayed
        "policy_update",  # V0.7 G5 (D142): acked, replayed
    } == REPORT_PAYLOADS
    # never buffered: heartbeat, pong, ack, hello, reconcile
    for name in ("heartbeat", "pong", "ack", "hello", "reconcile"):
        assert name not in REPORT_PAYLOADS


def test_order_and_first_sequence() -> None:
    ring = ReplayRing(10)
    msgs = [report(s) for s in (1, 2, 3)]
    for s, m in zip((1, 2, 3), msgs, strict=True):
        assert ring.append(s, m) is False
    assert [e.sequence for e in ring.pending()] == [1, 2, 3]
    assert ring.first_sequence() == 1
    assert len(ring) == 3


def test_overflow_drops_oldest_and_counts() -> None:
    ring = ReplayRing(3)
    msgs = [report(s) for s in range(1, 6)]
    dropped = [ring.append(s, m) for s, m in zip(range(1, 6), msgs, strict=True)]
    assert dropped == [False, False, False, True, True]
    assert ring.dropped == 2
    assert [e.sequence for e in ring.pending()] == [3, 4, 5]
    assert ring.first_sequence() == 3
    assert ring.ack(msgs[0].message_id) is False  # already dropped


def test_ack_removes_anywhere_and_duplicates_are_ignored() -> None:
    ring = ReplayRing(10)
    msgs = [report(s) for s in (1, 2, 3)]
    for s, m in zip((1, 2, 3), msgs, strict=True):
        ring.append(s, m)
    assert ring.ack(msgs[1].message_id) is True
    assert ring.ack(msgs[1].message_id) is False  # duplicate ack: no effect
    assert [e.sequence for e in ring.pending()] == [1, 3]
    assert ring.ack("cmd_unknown") is False


def test_empty_ring() -> None:
    ring = ReplayRing(1)
    assert ring.pending() == []
    assert ring.first_sequence() is None
    assert ring.ack("x") is False


def test_re_append_same_message_is_noop() -> None:
    ring = ReplayRing(2)
    m = report(1)
    ring.append(1, m)
    assert ring.append(1, m) is False
    assert len(ring) == 1


def test_only_reports_are_accepted() -> None:
    ring = ReplayRing(5)
    hb = agent_pb2.AgentMessage(
        message_id=new_command_id(), heartbeat=agent_pb2.Heartbeat()
    )
    with pytest.raises(ValueError, match="only reports"):
        ring.append(1, hb)
    pong = agent_pb2.AgentMessage(message_id=new_command_id(), pong=agent_pb2.Pong())
    with pytest.raises(ValueError, match="only reports"):
        ring.append(1, pong)
    with pytest.raises(ValueError, match="message_id"):
        ring.append(
            1, agent_pb2.AgentMessage(resource_update=agent_pb2.ResourceUpdate())
        )


def test_capacity_validation() -> None:
    with pytest.raises(ValueError, match="capacity"):
        ReplayRing(0)
    with pytest.raises(ValueError, match="capacity"):
        RecentIds(0)


def test_recent_ids_window_evicts_oldest() -> None:
    seen = RecentIds(2)
    assert seen.remember("a") is False
    assert seen.remember("a") is True
    assert seen.remember("b") is False
    assert seen.remember("c") is False  # evicts "a"
    assert seen.remember("a") is False  # forgotten, so new again
    assert seen.remember("c") is True
    assert len(seen) == 2
