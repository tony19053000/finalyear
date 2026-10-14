"""V0.8 G1 item 0 (D168 N1): a cross-node status report is counted on its own
counter (``attempt_status_wrong_node``), never on the fencing counter, and
both counters are where the fencing one always was: the session's
``summary()`` (the ``session.closed`` log line) and the dispatcher stats."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.common.ids import (
    new_attempt_id,
    new_command_id,
    new_node_id,
    new_session_id,
)
from meshforge.common.states import AttemptState
from meshforge.controller.jobs.dispatcher import Dispatcher, _Status
from meshforge.controller.jobs.execution_store import (
    StatusKind,
    StatusOutcome,
    StatusReport,
)
from meshforge.controller.liveness import LeaseTracker, LivenessConfig, NodeState
from meshforge.controller.session.live import LiveSession, SeenIds
from meshforge.controller.session.writer import WriteChain
from meshforge.controller.settings import JobApiSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio, pytest.mark.security]


class _Manager:
    def __init__(self) -> None:
        self.hooks = SimpleNamespace(
            on_command_acked=None, on_reconcile=None, on_node_offline=None
        )
        self.dispatch = SimpleNamespace(register=lambda *_a: None)
        self.commands: list[Any] = []
        self.acked: list[str] = []

    def defer_ack(self, _p: str) -> None:
        return None

    def send_command(self, node_id: str, name: str, payload: Any, **_kw: Any) -> str:
        self.commands.append((node_id, name, payload))
        return str(new_command_id())

    def ack_message(self, _live: Any, message_id: str) -> bool:
        self.acked.append(message_id)
        return True


class _Store:
    def __init__(self, kind: StatusKind) -> None:
        self.kind = kind

    async def apply_status(self, _r: StatusReport, *, node_id: str) -> StatusOutcome:
        return StatusOutcome(self.kind)

    async def live_attempts(self, _node_id: str | None = None) -> list[Any]:
        return []

    async def get_attempt(self, _attempt_id: str) -> Any:
        return None


def _live() -> LiveSession:
    node_id, session_id = str(new_node_id()), str(new_session_id())
    return LiveSession(
        node_id=node_id,
        session_id=session_id,
        session_epoch=1,
        peer_address="ipv4:127.0.0.1:1",
        tracker=LeaseTracker(LivenessConfig(), lambda: 0.0),
        state=NodeState.ONLINE,
        outbound=asyncio.Queue(),
        seen=SeenIds(4),
        next_ping_at=0.0,
        writer=WriteChain(node_id=node_id, session_id=session_id),
    )


def _dispatcher(kind: StatusKind) -> tuple[Dispatcher, _Manager]:
    manager = _Manager()
    dispatcher = Dispatcher(
        store=_Store(kind),  # type: ignore[arg-type]
        manager=manager,  # type: ignore[arg-type]
        fetch_tokens=SimpleNamespace(),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        clock=lambda: 0.0,
        blob_port=lambda: 1,
    )
    return dispatcher, manager


async def test_a_wrong_node_report_counts_on_its_own_counter_only() -> None:
    dispatcher, manager = _dispatcher(StatusKind.WRONG_NODE)
    live = _live()
    report = StatusReport(str(new_attempt_id()), 2, AttemptState.DONE)
    item = _Status(live, str(new_command_id()), report)  # type: ignore[arg-type]
    await dispatcher._handle(item)
    assert live.attempt_status_wrong_node == 1
    assert live.attempt_status_fenced == 0
    summary = live.summary()  # the session.closed log line
    assert summary["attempt_status_wrong_node"] == 1
    assert summary["attempt_status_fenced"] == 0
    assert dispatcher.stats.statuses_wrong_node == 1
    assert dispatcher.stats.statuses_fenced == 0
    assert manager.acked == [item.message_id] and manager.commands == []


async def test_a_fenced_report_still_counts_as_fenced_only() -> None:
    dispatcher, _ = _dispatcher(StatusKind.FENCED)
    live = _live()
    report = StatusReport(str(new_attempt_id()), 2, AttemptState.DONE)
    await dispatcher._handle(_Status(live, str(new_command_id()), report))  # type: ignore[arg-type]
    assert live.attempt_status_fenced == 1
    assert live.attempt_status_wrong_node == 0
    assert dispatcher.stats.statuses_wrong_node == 0
    assert live.summary()["attempt_status_wrong_node"] == 0
