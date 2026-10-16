"""V0.8 G3 item 1 (D180): the live ``alert`` for a WRONG_NODE refusal carries
the audit row's id (and the job / placed node it names), so the dashboard can
merge it with ``GET /v1/alerts`` by id. The G1 keys stay as they were."""

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


# The doubles of test_v08_g1_wrong_node_counter.py (tests/unit is not a package).
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


class _StoreWithAlert(_Store):
    async def apply_status(self, _r: StatusReport, *, node_id: str) -> StatusOutcome:
        return StatusOutcome(
            self.kind,
            alert={"id": "42", "job_id": "job_x", "placed_node_id": "node_a"},
        )


async def test_the_live_alert_carries_the_audit_row_id() -> None:
    alerts: list[dict[str, str]] = []
    dispatcher = Dispatcher(
        store=_StoreWithAlert(StatusKind.WRONG_NODE),  # type: ignore[arg-type]
        manager=_Manager(),  # type: ignore[arg-type]
        fetch_tokens=SimpleNamespace(),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        clock=lambda: 0.0,
        blob_port=lambda: 1,
        on_alert=alerts.append,
    )
    live = _live()
    report = StatusReport(str(new_attempt_id()), 2, AttemptState.FAILED)
    await dispatcher._handle(_Status(live, str(new_command_id()), report))  # type: ignore[arg-type]
    assert alerts == [
        {
            "kind": "wrong_node",
            "node_id": live.node_id,
            "attempt_id": report.attempt_id,
            "reported": "failed",
            "id": "42",
            "job_id": "job_x",
            "placed_node_id": "node_a",
        }
    ]
