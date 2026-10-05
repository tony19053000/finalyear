"""V0.5 G5 (0.5.8): the controller keeps prepare progress from the right node only."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.prepare import MAX_ITEMS, PrepareTracker

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


def _live(node_id: str) -> Any:
    return SimpleNamespace(node_id=node_id, session_id="s")


def _status(request_id: str, *items: agent_pb2.PrepareItem, done: bool = False) -> Any:
    return agent_pb2.PrepareEnvStatus(
        request_id=request_id, done=done, items=list(items)
    )


def test_start_sends_and_tracks_only_for_live_nodes() -> None:
    manager = FakeManager({"node_a"})
    tracker = PrepareTracker(manager)  # type: ignore[arg-type]
    assert tracker.start("node_b", images=["python-cpu"], envs=[]) is None
    record = tracker.start("node_a", images=["python-cpu"], envs=[])
    assert record is not None and manager.sent[0][1] == "prepare_env"
    assert manager.sent[0][2].request_id == record.request_id


def test_status_from_another_node_or_unknown_request_is_dropped() -> None:
    tracker = PrepareTracker(FakeManager({"node_a"}))  # type: ignore[arg-type]
    record = tracker.start("node_a", images=[], envs=[])
    assert record is not None
    item = agent_pb2.PrepareItem(kind="env", name="0", state="built", key="k" * 64)
    tracker.on_status(_live("node_b"), _status(record.request_id, item, done=True))
    tracker.on_status(_live("node_a"), _status("unknown", item, done=True))
    assert tracker.dropped == 2 and not record.done
    tracker.on_status(_live("node_a"), _status(record.request_id, item, done=True))
    assert record.done and record.items[0]["state"] == "built"
    # A replayed older (not done) report never turns a finished request back.
    tracker.on_status(
        _live("node_a"),
        _status(
            record.request_id,
            agent_pb2.PrepareItem(kind="env", name="0", state="running"),
        ),
    )
    assert record.done and record.items[0]["state"] == "built"
    assert tracker.get("node_b", record.request_id) is None


def test_items_are_bounded_and_text_checked() -> None:
    tracker = PrepareTracker(FakeManager({"node_a"}))  # type: ignore[arg-type]
    record = tracker.start("node_a", images=[], envs=[])
    assert record is not None
    ok = agent_pb2.PrepareItem(kind="env", name="0", state="pending")
    tracker.on_status(_live("node_a"), _status(record.request_id, *[ok] * MAX_ITEMS))
    assert len(record.items) == MAX_ITEMS
    tracker.on_status(
        _live("node_a"), _status(record.request_id, *[ok] * (MAX_ITEMS + 1))
    )
    assert tracker.dropped == 1
    for bad in (
        agent_pb2.PrepareItem(kind="vm", name="0", state="pending"),
        agent_pb2.PrepareItem(kind="env", name="0", state="weird"),
        agent_pb2.PrepareItem(kind="env", name="\x1b[2J", state="pending"),
        agent_pb2.PrepareItem(kind="env", name="0", state="failed", detail="d" * 301),
    ):
        tracker.on_status(_live("node_a"), _status(record.request_id, bad))
    assert tracker.dropped == 5
