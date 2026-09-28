"""NB-1: ``Reconcile`` must be the first ``AgentMessage`` of every reconnect.

The R1 duplicate fix rests on an ordering invariant inside
:meth:`AgentSessionClient._online`: the ONLINE transition (which is what lets
``send_report`` write, and what wakes the resource monitor) is followed by
``_reconcile_and_replay()`` with **no suspension point in between**, so the
``Reconcile`` takes ``_write_lock`` before any freshly produced report can.

If that invariant is ever broken by adding an ``await`` in that window, a new
report overtakes the ``Reconcile``; the controller's armed cursor reset is then
consumed by that report, the cursor jumps to its sequence and every replayed
report below it is dropped - telemetry lost with no error anywhere. This test
makes that edit fail loudly: it writes reports as fast as it can across a real
controller restart and asserts the order of what actually went on the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import pytest
from mf_agent_harness import AgentFactory, make_controller_settings
from mf_session_client import poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.identity import NodeIdentity
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.state import AgentState
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

ONLINE_TIMEOUT_S = 10.0


def _record_writes(
    client: AgentSessionClient, sent: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Append every payload name **in the order it reached the wire**."""
    original = client._write

    async def write(msg: agent_pb2.AgentMessage, **kw: Any) -> None:
        await original(msg, **kw)
        sent.append(msg.WhichOneof("payload") or "?")

    monkeypatch.setattr(client, "_write", write)


@pytest.mark.asyncio
async def test_reconcile_is_the_first_message_of_every_reconnect(
    pg_settings: DatabaseSettings,
    agents: AgentFactory,
    identity: NodeIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = ControllerRuntime(make_controller_settings(pg_settings))
    await first._start()
    port = first.grpc_port
    running = await agents(f"127.0.0.1:{port}", identity=identity)
    sent: list[str] = []
    _record_writes(running.client, sent, monkeypatch)

    async def report_forever() -> None:
        """Stand in for the resource monitor, made deterministic: a task that is
        *always runnable* (``sleep(0)``), so any suspension point added between
        the ONLINE transition and the Reconcile hands control to it. Bounded to a
        few reports per session so the replay ring is not flooded."""
        per_session: dict[str, int] = {}
        while True:
            client = running.client
            key = client.session_id
            if client.state is AgentState.ONLINE and per_session.get(key, 0) < 3:
                per_session[key] = per_session.get(key, 0) + 1
                with contextlib.suppress(Exception):
                    await client.send_report(
                        "resource_update", agent_pb2.ResourceUpdate(cpu_percent=1.0)
                    )
            await asyncio.sleep(0)

    reporter: asyncio.Task[Any] = asyncio.create_task(report_forever())
    try:
        await running.client.wait_for_state(
            AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S
        )
        for _round in range(2):
            accepted = running.client.stats.sessions_accepted
            await first.stop()
            await running.client.wait_for_state(
                AgentState.BACKOFF,
                AgentState.DISCOVERING,
                AgentState.CONNECTING,
                timeout_s=5.0,
            )
            first = ControllerRuntime(
                make_controller_settings(pg_settings, grpc_bind=f"127.0.0.1:{port}")
            )
            await first._start()
            await running.client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)

            async def reconnected(want: int = accepted + 1) -> bool:
                return running.client.stats.sessions_accepted >= want

            await poll_until(reconnected, timeout_s=5.0)
            await asyncio.sleep(0.1)  # let a few post-reconnect reports through
    finally:
        reporter.cancel()
        await asyncio.gather(reporter, return_exceptions=True)
        await first.stop()

    # A Hello directly followed by another Hello was an attempt that never became
    # a session (e.g. it reached the old controller while it was stopping: the
    # write went out, the call then failed). Nothing but Hello can follow such an
    # attempt - send_report writes only while ONLINE - so it cannot hide a broken
    # ordering; it is skipped (V0.4 G1: seen once under a loaded machine).
    hellos = [
        index
        for index, which in enumerate(sent)
        if which == "hello" and sent[index + 1 : index + 2] != ["hello"]
    ]
    assert len(hellos) >= 3, f"expected 3 sessions, wire was {sent[:40]}"
    for index in hellos[1:]:
        after = sent[index + 1 :]
        assert after, f"nothing followed the reconnect Hello at {index}: {sent}"
        assert after[0] == "reconcile", (
            "ORDERING INVARIANT BROKEN (client.py _online): the first message of "
            f"a reconnect was {after[0]!r}, not 'reconcile'. A report overtaking "
            "the Reconcile makes the controller consume its armed cursor reset "
            "with the new sequence and silently drop the replayed reports. Do "
            "not add an await before _reconcile_and_replay()."
        )
