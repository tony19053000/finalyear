"""V0.4 G4 carries two G1-reviewer follow-ups on the controller session (D61).

* D61-1: a gRPC handler cancelled at the very moment its admission *succeeded*
  must not leave a reader-less session in the live table (a phantom ONLINE until
  the lease heals it) - it is closed ``stream_lost``.
* D61-4: a Goodbye is never acknowledged - nothing but the end-of-stream sentinel
  is queued after it.
"""

from __future__ import annotations

import asyncio

import pytest
from test_session_admission_deadline import Hang
from test_session_linger import admit, hello, make, settle

from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import SessionEndReason

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_handler_cancelled_as_admission_succeeds_leaves_no_phantom_session() -> (
    None
):
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    hang = Hang(reg, "open_session")  # the last registry await of an admission
    handler = asyncio.create_task(manager.admit(hello(node, 1), "ipv4:1.2.3.4:5"))
    await asyncio.wait_for(hang.held.wait(), 1.0)
    # Same loop iteration: the admission's last await completes (its task runs
    # first and finishes) and the handler awaiting it is cancelled.
    hang.release.set()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    await settle()
    assert manager.get(node) is None, "phantom: admitted session left in _live"
    entry = manager.get_lingering(node)
    assert entry is not None and entry.end_reason is SessionEndReason.STREAM_LOST
    assert entry.state is NodeState.SUSPECT  # the lease decides OFFLINE, as usual


@pytest.mark.asyncio
async def test_no_ack_is_queued_after_a_goodbye() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    goodbye = agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=live.session_id,
        goodbye=agent_pb2.Goodbye(reason=agent_pb2.Goodbye.REASON_SHUTDOWN),
    )
    await manager.handle_message(live, goodbye)
    queued = []
    while not live.outbound.empty():
        queued.append(live.outbound.get_nowait())
    assert queued == [None], f"something besides the end sentinel was queued: {queued}"
    assert live.end_reason is SessionEndReason.AGENT_CLOSED
