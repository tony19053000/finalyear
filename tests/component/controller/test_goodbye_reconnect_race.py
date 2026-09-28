"""A clean close racing a reconnect's admission (was: the 0.25 s half-close
confirmation window, V0.3 G6 tester round; rewritten for V0.4 D40).

Since D40 the clean-close evidence is the agent's ``Goodbye``, decided on receipt,
and the confirmation window is gone. The scenarios the old window was attacked
with still exist - a Goodbye (the agent restarting) while its reconnect's
admission is parked on a slow ``get_node`` - so they are kept with the new
evidence: D28 still makes the pending Hello own the node's state, so a fast
restart logs no false ``online -> offline -> online`` dip, and a *stale* pending
Hello still claims nothing, so a genuine clean close is OFFLINE at once.

Drives :class:`SessionManager` directly over a real PostgreSQL registry (no gRPC,
the same pattern as the D20 race test in ``test_stream_end_liveness.py``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import pytest
from mf_session_client import make_hello

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.session.manager import Accepted, SessionManager

pytestmark = pytest.mark.component


class _SaysGoodbye:
    """A request stream whose only message is the agent's Goodbye (D40), after
    which it stays open (the reader must stop reading on its own)."""

    def __init__(self, session_id: str) -> None:
        self._sent = False
        self._session_id = session_id

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        if self._sent:
            await asyncio.Event().wait()  # open until cancelled
        self._sent = True
        return agent_pb2.AgentMessage(
            message_id=new_command_id(),
            session_id=self._session_id,
            goodbye=agent_pb2.Goodbye(reason=agent_pb2.Goodbye.REASON_SHUTDOWN),
        )


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
    logs: Sequence[Mapping[str, Any]], node_id: str
) -> list[tuple[str, str, str | None]]:
    return [
        (e["from"], e["to"], e.get("liveness_event"))
        for e in logs
        if e["event"] == "node.state_changed" and e.get("node_id") == node_id
    ]


@pytest.mark.asyncio
async def test_goodbye_while_the_reconnect_is_admitted_logs_no_false_offline(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """A fast restart (Goodbye, then the new process's Hello already being
    admitted) must never be recorded as a real OFFLINE dip. Pins D28 with D40's
    evidence: the Goodbye's end is deferred to the pending Hello, which owns the
    state; the old stream keeps its honest reason, ``agent_closed``."""
    import structlog

    gated = _GatedGetNode(registry)
    manager = SessionManager(
        gated,  # type: ignore[arg-type]
        LivenessConfig(heartbeat_interval_s=30.0),  # no lease events in this test
        clock=asyncio.get_running_loop().time,
    )
    node_id = new_node_id()
    try:
        first = await manager.admit(make_hello(node_id, 1), "ipv4:127.0.0.1:1")
        assert isinstance(first, Accepted)
        old = first.live

        with structlog.testing.capture_logs() as logs:
            # Gate the NEW Hello's get_node so it is still in flight when the old
            # stream's Goodbye arrives and closes it.
            gated.hold_node = node_id
            admit_task = asyncio.create_task(
                manager.admit(make_hello(node_id, 2), "ipv4:127.0.0.1:2")
            )
            await asyncio.wait_for(gated.held.wait(), 5.0)
            reader = manager.start_reader(old, _SaysGoodbye(old.session_id))
            await asyncio.wait_for(reader, 5.0)
            assert manager.get(node_id) is None, "old session should already be closed"
            assert old.end_reason is SessionEndReason.AGENT_CLOSED

            gated.release.set()
            second = await admit_task
        assert isinstance(second, Accepted)

        transitions = changes_for(logs, node_id)
        went_offline = ("online", "offline", "stream_closed") in transitions
        came_back_reconnected = ("offline", "online", "reconnected") in transitions
        # The node was reachable throughout (clean close immediately followed by
        # a successful re-Hello): a true self-loop must never surface a real
        # "offline" hop that a soak-counting reader (V0.3 criterion 5) would see.
        assert not went_offline and not came_back_reconnected, (
            "a Goodbye during the reconnect's admission produced an "
            f"online->offline->online round trip in the log: {transitions} "
            "(a false OFFLINE for a node that was never actually down)"
        )
    finally:
        await manager.shutdown(drain_s=1.0)


@pytest.mark.asyncio
async def test_a_stale_epoch_hello_pending_does_not_hide_a_goodbyes_offline(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """D28's claim is epoch-fenced: a Hello whose epoch is not above the ending
    session's (a stale process, a replayed connect) owns nothing, so a Goodbye
    still goes OFFLINE at once - not after that Hello's admission."""
    import structlog

    from meshforge.controller.session.manager import Rejected

    gated = _GatedGetNode(registry)
    manager = SessionManager(
        gated,  # type: ignore[arg-type]
        LivenessConfig(heartbeat_interval_s=30.0),
        clock=asyncio.get_running_loop().time,
    )
    node_id = new_node_id()
    try:
        first = await manager.admit(make_hello(node_id, 5), "ipv4:127.0.0.1:1")
        assert isinstance(first, Accepted)
        with structlog.testing.capture_logs() as logs:
            gated.hold_node = node_id
            admit_task = asyncio.create_task(
                manager.admit(make_hello(node_id, 3), "ipv4:127.0.0.1:2")
            )
            await asyncio.wait_for(gated.held.wait(), 5.0)
            reader = manager.start_reader(
                first.live, _SaysGoodbye(first.live.session_id)
            )
            await asyncio.wait_for(reader, 5.0)  # stale Hello still held
            assert ("online", "offline", "stream_closed") in changes_for(logs, node_id)
            gated.release.set()
            assert isinstance(await admit_task, Rejected)
        await manager.shutdown(drain_s=2.0)
        node = await registry.get_node(node_id)
        assert node is not None and node.state is NodeState.OFFLINE
    finally:
        await manager.shutdown(drain_s=1.0)
