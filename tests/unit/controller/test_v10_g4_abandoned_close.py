"""V1.0 G4 F-1: closing an abandoned admission's session is a quiet no-op.

An admission cut short after the node-row upsert (D41) may never have committed
its ``open_session``; the entry's ``close_session`` then finds no row and the real
registry raises :class:`UnknownSessionError`. That is the expected outcome of an
abandoned admission, so it is logged at info (``session.close_skipped_never_opened``)
and never as ``registry.write_failed`` with a traceback. A missing row for any
other session is still a failed write.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog
from test_session_admission_deadline import Hang, drain_chains, make_slow
from test_session_linger import CFG, NOW0, FencedRegistry, admit, hello, make

from meshforge.common.ids import new_node_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import SessionEndReason, UnknownSessionError
from meshforge.controller.session.manager import Rejected

pytestmark = pytest.mark.unit


class _StrictSessions:
    """Make the double's ``open_session``/``close_session`` behave like the real
    registry: ``close_session`` of an id never opened raises ``UnknownSessionError``."""

    def __init__(self, reg: FencedRegistry) -> None:
        self.opened: set[str] = set()
        self.closed: list[str] = []
        self._reg = reg
        reg.open_session = self.open_session  # type: ignore[method-assign]
        reg.close_session = self.close_session  # type: ignore[method-assign]

    async def open_session(self, **kw: Any) -> None:
        self.opened.add(kw["session_id"])

    async def close_session(self, session_id: str, reason: SessionEndReason) -> bool:
        if session_id not in self.opened:
            raise UnknownSessionError(
                f"no session {session_id}", details={"session_id": session_id}
            )
        self.closed.append(session_id)
        return True


def _events(logs: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == name]


@pytest.mark.asyncio
async def test_closing_a_never_opened_abandoned_session_is_quiet() -> None:
    manager, reg, clock, timer = make_slow()
    sessions = _StrictSessions(reg)
    node = new_node_id()
    await admit(manager, node, 1)
    hang = Hang(reg, "open_session")  # the open never commits (a hung database)
    with structlog.testing.capture_logs() as logs:
        result = await asyncio.wait_for(
            manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"), 1.0
        )
        await drain_chains()
        # The lease edge ends the linger: OFFLINE is written, the chain ends.
        clock.now = NOW0 + CFG.offline_after_s
        await timer.tick(clock.now)
        await drain_chains()
    assert isinstance(result, Rejected)
    assert hang.calls == 1
    entry_id = reg.nodes[node].last_session_id
    assert entry_id not in sessions.opened
    assert reg.nodes[node].state is NodeState.OFFLINE  # behaviour unchanged
    assert _events(logs, "registry.write_failed") == []
    (skipped,) = _events(logs, "session.close_skipped_never_opened")
    assert skipped["log_level"] == "info"
    assert skipped["session_id"] == entry_id
    assert skipped["node_id"] == node
    assert "exc_info" not in skipped


@pytest.mark.asyncio
async def test_an_abandoned_session_whose_open_committed_is_closed_normally() -> None:
    """The open committed, then the admission missed its deadline: the row
    exists and is closed as usual - nothing skipped, nothing failed."""
    manager, reg, _clock, _timer = make_slow()
    sessions = _StrictSessions(reg)
    node = new_node_id()
    await admit(manager, node, 1)

    inner_open = sessions.open_session
    gate = asyncio.Event()

    async def open_then_hang(**kw: Any) -> None:
        await inner_open(**kw)
        await gate.wait()  # committed, then the admission stalls past its deadline

    reg.open_session = open_then_hang  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as logs:
        result = await asyncio.wait_for(
            manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"), 1.0
        )
        await drain_chains()
    assert isinstance(result, Rejected)
    entry_id = reg.nodes[node].last_session_id
    assert entry_id in sessions.closed
    assert _events(logs, "session.close_skipped_never_opened") == []
    assert _events(logs, "registry.write_failed") == []


@pytest.mark.asyncio
async def test_a_missing_row_for_a_normal_session_is_still_a_failed_write() -> None:
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    live = await admit(manager, node, 1)
    sessions = _StrictSessions(reg)  # installed after the open: the id is unknown
    with structlog.testing.capture_logs() as logs:
        manager.end_session_nowait(live, SessionEndReason.AGENT_CLOSED)
        await drain_chains()
    assert sessions.closed == []
    assert _events(logs, "session.close_skipped_never_opened") == []
    (failed,) = _events(logs, "registry.write_failed")
    assert failed["log_level"] == "error"
    assert failed["failure_type"] == "not_found"
