"""V0.3 G6 tester round — attacking the 0.25 s half-close confirmation window
(``HALF_CLOSE_CONFIRM_S``, ``controller/session/manager.py``; tester brief priority 2).

The confirmation window is the ONLY way the controller tells a clean agent
half-close from a drop (ARCHITECTURE §12.3, 2026-09-24 amendment). This module
drives :class:`SessionManager` directly (no gRPC — the same pattern as the D20
race test in ``test_stream_end_liveness.py``) to attack the window's interaction
with admission:

A **reconnect Hello arriving inside the confirm window** races `_admit_locked`'s
first await (`registry.get_node`, *before* it reads `self._live.get(node_id)`)
against the confirm-sleep's completion. `_admit_locked` reads the OLD session's
in-memory `.state` to decide `prior` only when `self._live.get(node_id)` still
finds the old session; if the confirm-sleep wins the race and closes it first,
`old` is already gone from `self._live`, and the admission falls back to
`record.state` — the DB row read at the *start* of admission, i.e. BEFORE the
old session's own clean-close ever wrote OFFLINE. Depending on exactly how the
two interleave, this can make a session that never really left produce a
spurious ``offline -> online (reconnected)`` `node.state_changed` log line
instead of the true self-loop (``online -> online``, event ``heartbeat``) — a
false OFFLINE that the 24 h soak (V0.3 criterion 5) would count as a real one,
even though the row itself, once quiet, is correct (D20's fence protects that
part).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mf_session_client import make_hello

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.ids import new_node_id
from meshforge.controller.liveness import LivenessConfig, NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.session.manager import Accepted, SessionManager

pytestmark = pytest.mark.component


class _EndsNormally:
    """An async iterator that yields nothing and ends normally at once — the
    candidate half-close `_read_loop` treats as a clean end pending confirmation."""

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


class _GatedGetNode:
    """The real registry, with ``get_node`` for one node id held on an Event until
    released — the seam that lets the test place the new Hello's read exactly
    where a slow database round trip (or a loaded event loop) would."""

    def __init__(self, inner: Registry) -> None:
        self._inner = inner
        self.hold_node: str | None = None
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def get_node(self, node_id: str) -> Any:
        if node_id == self.hold_node:
            self.held.set()
            await self.release.wait()
        return await self._inner.get_node(node_id)


def changes_for(
    logs: list[dict[str, Any]], node_id: str
) -> list[tuple[str, str, str | None]]:
    return [
        (e["from"], e["to"], e.get("liveness_event"))
        for e in logs
        if e["event"] == "node.state_changed" and e.get("node_id") == node_id
    ]


@pytest.mark.asyncio
async def test_reconnect_hello_inside_confirm_window_can_log_a_false_offline(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """A clean, fast reconnect (half-close then immediate re-Hello, both well
    inside the confirm window) must never be recorded as a real OFFLINE dip if
    the node was continuously reachable. Pins D28 (a Hello that arrives before
    the previous session's end is decided owns the state): was a strict xfail
    until the G6 fix round made admission claim the node while it is pending."""
    import structlog

    gated = _GatedGetNode(registry)
    manager = SessionManager(
        gated,  # type: ignore[arg-type]
        LivenessConfig(heartbeat_interval_s=30.0),  # no lease events in this test
        clock=asyncio.get_running_loop().time,
        half_close_confirm_s=0.05,
    )
    node_id = new_node_id()
    try:
        first = await manager.admit(make_hello(node_id, 1), "ipv4:127.0.0.1:1")
        assert isinstance(first, Accepted)
        old = first.live

        with structlog.testing.capture_logs() as logs:
            manager.start_reader(old, _EndsNormally())

            # Gate the NEW Hello's get_node so it is still in flight when the old
            # session's 0.05 s confirm-sleep elapses and closes it.
            gated.hold_node = node_id
            admit_task = asyncio.create_task(
                manager.admit(make_hello(node_id, 2), "ipv4:127.0.0.1:2")
            )
            await asyncio.wait_for(gated.held.wait(), 5.0)

            # Let the confirm-sleep win: the old session closes (AGENT_CLOSED ->
            # STREAM_CLOSED -> OFFLINE) while the new Hello's read is still gated.
            await asyncio.sleep(0.2)
            assert manager.get(node_id) is None, "old session should already be closed"

            gated.release.set()
            second = await admit_task
        assert isinstance(second, Accepted)

        transitions = changes_for(logs, node_id)
        went_offline = ("online", "offline", "stream_closed") in transitions
        came_back_reconnected = ("offline", "online", "reconnected") in transitions
        # The node was reachable throughout (clean close immediately followed by
        # a successful re-Hello): a true self-loop must never surface a real
        # "offline" hop that a soak-counting reader (V0.3 criterion 5) would see.
        assert not (went_offline and came_back_reconnected), (
            "reconnect inside the confirm window produced a full "
            f"online->offline->online round trip in the log: {transitions} "
            "(a false OFFLINE for a node that was never actually down)"
        )
    finally:
        await manager.shutdown(drain_s=1.0)


@pytest.mark.asyncio
async def test_a_stale_epoch_hello_inside_the_confirm_window_does_not_hide_the_offline(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """D28's claim is epoch-fenced: a Hello whose epoch is not above the ending
    session's (a stale process, a replayed connect) owns nothing, so a genuine
    clean close still goes OFFLINE at once - not after that Hello's admission."""
    import structlog

    from meshforge.controller.session.manager import Rejected

    gated = _GatedGetNode(registry)
    manager = SessionManager(
        gated,  # type: ignore[arg-type]
        LivenessConfig(heartbeat_interval_s=30.0),
        clock=asyncio.get_running_loop().time,
        half_close_confirm_s=0.05,
    )
    node_id = new_node_id()
    try:
        first = await manager.admit(make_hello(node_id, 5), "ipv4:127.0.0.1:1")
        assert isinstance(first, Accepted)
        with structlog.testing.capture_logs() as logs:
            manager.start_reader(first.live, _EndsNormally())
            gated.hold_node = node_id
            admit_task = asyncio.create_task(
                manager.admit(make_hello(node_id, 3), "ipv4:127.0.0.1:2")
            )
            await asyncio.wait_for(gated.held.wait(), 5.0)
            await asyncio.sleep(0.2)  # the confirm window elapses; stale Hello held
            assert ("online", "offline", "stream_closed") in changes_for(logs, node_id)
            gated.release.set()
            assert isinstance(await admit_task, Rejected)
        await manager.shutdown(drain_s=2.0)
        node = await registry.get_node(node_id)
        assert node is not None and node.state is NodeState.OFFLINE
    finally:
        await manager.shutdown(drain_s=1.0)
