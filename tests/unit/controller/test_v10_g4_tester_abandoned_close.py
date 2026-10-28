"""V1.0 G4 F-1 tester: the never-opened suppression must not leak."""

from __future__ import annotations

import asyncio

import pytest
import structlog
from test_session_admission_deadline import Hang, drain_chains, make_slow
from test_session_linger import admit, hello
from test_v10_g4_abandoned_close import _events, _StrictSessions

from meshforge.common.errors import MeshForgeError
from meshforge.common.ids import new_node_id
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.live import LiveSession
from meshforge.controller.session.manager import Rejected

pytestmark = pytest.mark.unit


def test_the_flag_defaults_off_for_every_ordinary_session() -> None:
    assert LiveSession.__dataclass_fields__["open_uncertain"].default is False


@pytest.mark.asyncio
async def test_an_abandoned_entrys_other_errors_are_still_failed_writes() -> None:
    """Only UnknownSessionError is quiet; a database failure on the abandoned
    entry's close is still ``registry.write_failed``."""
    manager, reg, _clock, _timer = make_slow()
    sessions = _StrictSessions(reg)
    node = new_node_id()
    await admit(manager, node, 1)
    Hang(reg, "open_session")

    async def broken(session_id: str, reason: SessionEndReason) -> bool:
        raise MeshForgeError("db down")

    reg.close_session = broken  # type: ignore[method-assign]
    with structlog.testing.capture_logs() as logs:
        result = await asyncio.wait_for(
            manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"), 1.0
        )
        await drain_chains()
    assert isinstance(result, Rejected)
    assert _events(logs, "session.close_skipped_never_opened") == []
    assert len(_events(logs, "registry.write_failed")) >= 1
    assert sessions.closed == []


@pytest.mark.asyncio
async def test_a_genuine_session_next_to_an_abandoned_one_still_errors() -> None:
    """An abandoned admission for node A must not silence the missing row of a
    normal session for node B."""
    manager, reg, _clock, _timer = make_slow()
    node_b = new_node_id()
    live_b = await admit(manager, node_b, 1)
    sessions = _StrictSessions(reg)  # B's row is unknown to the strict double
    node_a = new_node_id()
    await admit(manager, node_a, 1)
    Hang(reg, "open_session")
    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(manager.admit(hello(node_a, 2), "ipv4:1.2.3.4:5"), 1.0)
        await drain_chains()
        manager.end_session_nowait(live_b, SessionEndReason.AGENT_CLOSED)
        await drain_chains()
    failed = _events(logs, "registry.write_failed")
    assert len(failed) == 1, failed  # B's missing row, loud as before
    assert live_b.session_id not in sessions.closed
    assert not any(
        e.get("node_id") == node_b
        for e in _events(logs, "session.close_skipped_never_opened")
    )
