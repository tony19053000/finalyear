"""V0.5 G5 fix round 1 (N2): a node lost mid-prepare never leaves the request
pending forever - OFFLINE ends it ``node_lost``; a new session re-sends it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessEvent
from meshforge.controller.prepare import PrepareTracker

pytestmark = [pytest.mark.unit]


class FakeManager:
    def __init__(self, live: set[str]) -> None:
        self.live = live
        self.sent: list[tuple[str, str, Any]] = []

    def send_command(self, node_id: str, name: str, payload: Any) -> str | None:
        if node_id not in self.live:
            return None
        self.sent.append((node_id, name, payload))
        return "cmd"

    def get(self, node_id: str) -> Any:
        """The node's live session (``s1``), as ``SessionManager.get``."""
        return SimpleNamespace(session_id="s1") if node_id in self.live else None


def _live(node_id: str, session_id: str = "s1") -> Any:
    return SimpleNamespace(node_id=node_id, session_id=session_id)


def _running(tracker: PrepareTracker, request_id: str) -> None:
    tracker.on_status(
        _live("node_a"),
        agent_pb2.PrepareEnvStatus(
            request_id=request_id,
            items=[
                agent_pb2.PrepareItem(kind="image", name="python-cpu", state="hit"),
                agent_pb2.PrepareItem(kind="env", name="0", state="running"),
            ],
        ),
    )


def test_offline_ends_unfinished_requests_node_lost_and_keeps_finished_items() -> None:
    tracker = PrepareTracker(FakeManager({"node_a", "node_b"}))  # type: ignore[arg-type]
    record = tracker.start("node_a", images=["python-cpu"], envs=[])
    other = tracker.start("node_b", images=[], envs=[])
    assert record is not None and other is not None
    _running(tracker, record.request_id)
    tracker.on_node_offline(_live("node_a"), LivenessEvent.LEASE_EXPIRED)
    body = record.as_dict()
    assert body["done"] is True and body["ok"] is False
    image, env, request = body["items"]
    assert image["state"] == "hit"  # finished work stays as reported
    for item in (env, request):
        assert item["state"] == "failed"
        assert item["failure_type"] == "node_lost"
        assert item["reason"] == "node_offline"
    assert request["kind"] == "request"
    assert not other.done  # another node's request is untouched
    # A late status replayed from the old session never revives it.
    _running(tracker, record.request_id)
    assert record.done and record.items[1]["state"] == "failed"


def test_a_finished_request_is_not_touched_by_offline() -> None:
    tracker = PrepareTracker(FakeManager({"node_a"}))  # type: ignore[arg-type]
    record = tracker.start("node_a", images=[], envs=[])
    assert record is not None
    tracker.on_status(
        _live("node_a"),
        agent_pb2.PrepareEnvStatus(request_id=record.request_id, done=True),
    )
    tracker.on_node_offline(_live("node_a"), LivenessEvent.STREAM_CLOSED)
    assert record.as_dict()["ok"] is True and record.items == []


def test_a_new_session_gets_the_same_request_again() -> None:
    manager = FakeManager({"node_a"})
    tracker = PrepareTracker(manager)  # type: ignore[arg-type]
    record = tracker.start("node_a", images=["python-cpu"], envs=[])
    assert record is not None
    tracker.on_reconcile(_live("node_a", "s2"))
    assert len(manager.sent) == 2
    first, again = manager.sent[0][2], manager.sent[1][2]
    assert again.request_id == first.request_id == record.request_id
    assert list(again.base_images) == ["python-cpu"]
    assert not record.done
    # Finished requests are not re-sent.
    tracker.on_status(
        _live("node_a"),
        agent_pb2.PrepareEnvStatus(request_id=record.request_id, done=True),
    )
    tracker.on_reconcile(_live("node_a", "s3"))
    assert len(manager.sent) == 2


def test_a_resend_that_cannot_be_queued_ends_node_lost() -> None:
    manager = FakeManager({"node_a"})
    tracker = PrepareTracker(manager)  # type: ignore[arg-type]
    record = tracker.start("node_a", images=[], envs=[])
    assert record is not None
    manager.live.clear()
    tracker.on_reconcile(_live("node_a", "s2"))
    assert record.done
    assert record.items[-1]["reason"] == "resend_failed"
    assert record.items[-1]["failure_type"] == "node_lost"


def test_a_request_is_sent_at_most_once_per_session() -> None:
    """G5-R2-3: a Reconcile repeated within one session queues no copy."""
    manager = FakeManager({"node_a"})
    tracker = PrepareTracker(manager)  # type: ignore[arg-type]
    record = tracker.start("node_a", images=[], envs=[])
    assert record is not None
    for _ in range(5):
        tracker.on_reconcile(_live("node_a", "s1"))  # the session it went on
    assert len(manager.sent) == 1
    for _ in range(5):
        tracker.on_reconcile(_live("node_a", "s2"))
    assert len(manager.sent) == 2  # once to the new session
    tracker.on_reconcile(_live("node_a", "s3"))
    assert len(manager.sent) == 3
