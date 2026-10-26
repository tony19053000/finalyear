"""Tester round 2 (V0.3 G7, D39 fix delta 97f8f8e..fe73d46): does ``declared_gpus``
follow the RIGHT session across a reconnect where the hardware changed, and does
the D28 pending-Hello claim leak one session's declared GPUs into another's?

``check_hardware_scores(scores, declared_gpus)`` (``session/limits.py``) is pure;
the session-level question is which ``declared_gpus`` a report is checked
against. The answer lives on ``LiveSession.declared_gpus``, set once at
admission from *that session's own* Hello (``manager.py::declared_gpus``) and
never touched again - so the fix's correctness here is really "is each report
validated against the ``live`` object it actually arrived on", which these
tests pin directly rather than assume from reading the diff.

No database, no sockets (unit tier): the fakes and helpers are the same ones
G2/G7 already use.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Mapping
from typing import Any

import pytest
from mf_bench_fakes import make_scoreset
from structlog.testing import capture_logs
from test_session_manager import (
    FakeClock,
    FakeRegistry,
    admit,
    drain,
    make_manager,
    mid,
)

from meshforge import __version__
from meshforge.agent.benchmarks.wire import to_proto
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import ScoresWrite
from meshforge.controller.session.manager import Accepted, LiveSession, declared_gpus

pytestmark = pytest.mark.unit


class ScoresRegistry(FakeRegistry):
    async def set_hardware_scores(
        self,
        node_id: str,
        document: Mapping[str, Any],
        *,
        session_id: str,
        measured_at: dt.datetime,
    ) -> ScoresWrite:
        self._record(
            "set_hardware_scores",
            node_id,
            dict(document),
            session_id=session_id,
            measured_at=measured_at,
        )
        return ScoresWrite.WRITTEN


class GatedRegistry(ScoresRegistry):
    """The real registry, with ``get_node`` held open until the test releases it.

    Models the D28 window: an admission that has validated a Hello (so its
    epoch is already claimed in ``_pending_hellos``) but has not yet returned,
    because it is waiting on the database's high-water-mark read.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def get_node(self, node_id: str) -> Any:
        self.entered.set()
        await self.gate.wait()
        return await super().get_node(node_id)


def _hello(node_id: str, epoch: int, *gpu_indices: int) -> agent_pb2.Hello:
    h = agent_pb2.Hello(
        node_id=node_id,
        protocol_version=1,
        agent_version=__version__,
        hostname="laptop",
        session_epoch=epoch,
        os="Ubuntu 24.04",
        platform="linux-x86_64",
        hardware=agent_pb2.HardwareSummary(cpu_model="cpu", cpu_cores=4),
    )
    for index in gpu_indices:
        h.hardware.gpus.append(agent_pb2.HardwareSummary.Gpu(index=index, model="g"))
    return h


def _report(live: LiveSession, label: str, scores: agent_pb2.HardwareScores) -> Any:
    return agent_pb2.AgentMessage(
        message_id=mid(label),
        session_id=live.session_id,
        hardware_scores=scores,
    )


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ------------------------------------------------------- reconnect, GPU changes


@pytest.mark.asyncio
async def test_reconnect_new_session_declares_its_own_gpus_not_the_olds() -> None:
    """A superseding Hello that declares no GPU gives the new session an empty
    ``declared_gpus`` - it must not inherit the old session's {0}."""
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    node_id = new_node_id()
    old = await admit(manager, _hello(node_id, 1, 0))
    assert old.declared_gpus == frozenset({0})

    new = await admit(manager, _hello(node_id, 2))  # GPU vanished on reconnect
    assert new.declared_gpus == frozenset()
    assert old.ended  # superseded


@pytest.mark.asyncio
async def test_report_naming_a_gpu_that_disappeared_is_rejected_on_the_new_session() -> (
    None
):
    """The scenario in the tester brief: a GPU disappears between sessions and a
    report for that GPU (however it was produced - a stale cache, a replayed
    body, a hostile peer) arrives *stamped with the new session_id*. It must be
    rejected against the new hardware, not silently accepted because some older
    session once declared that index."""
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    node_id = new_node_id()
    await admit(manager, _hello(node_id, 1, 0))
    new = await admit(manager, _hello(node_id, 2))  # no GPUs declared now
    assert new.declared_gpus == frozenset()

    old_hardware_run = to_proto(make_scoreset())  # names gpu_index=0
    with capture_logs() as logs:
        await manager.handle_message(new, _report(new, "phantom", old_hardware_run))
    await _settle()
    assert registry.calls_named("set_hardware_scores") == []
    assert new.hardware_scores_rejected == 1
    (rejected,) = [e for e in logs if e["event"] == "session.hardware_scores_rejected"]
    assert rejected["reason"].startswith("gpu_index_not_declared")
    assert rejected["session_id"] == new.session_id
    # still acked (dropped, not a protocol error) and the stream stays up
    acks = [m for m in drain(new) if m is not None and m.HasField("ack")]
    assert [a.ack.acked_message_id for a in acks] == [mid("phantom")]
    assert not new.ended


@pytest.mark.asyncio
async def test_legitimate_report_for_a_gpu_still_present_survives_reconnect() -> None:
    """The mirror case: the GPU is still there after the reconnect, so the same
    report must still be accepted on the new session - the fix must not
    overreject."""
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    node_id = new_node_id()
    await admit(manager, _hello(node_id, 1, 0))
    new = await admit(manager, _hello(node_id, 2, 0))  # same GPU, new session
    assert new.declared_gpus == frozenset({0})

    run = to_proto(make_scoreset())  # names gpu_index=0
    await manager.handle_message(new, _report(new, "ok-run", run))
    await _settle()
    calls = registry.calls_named("set_hardware_scores")
    assert len(calls) == 1
    assert calls[0][1]["session_id"] == new.session_id
    assert new.hardware_scores_rejected == 0


@pytest.mark.asyncio
async def test_a_report_carrying_the_old_sessions_id_is_dropped_before_the_gpu_check() -> (
    None
):
    """A message stamped with a stale ``session_id`` is fenced by the
    session_id check in ``handle_message`` *before* it ever reaches
    ``check_hardware_scores`` - it must never count as a GPU-index rejection,
    and it gets no ack (D22/ADR-0002 stale-message handling, unchanged by D39)."""
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    node_id = new_node_id()
    old = await admit(manager, _hello(node_id, 1, 0))
    new = await admit(manager, _hello(node_id, 2))  # GPU vanished; old is superseded

    stale = agent_pb2.AgentMessage(
        message_id=mid("stale-session"),
        session_id=old.session_id,  # the dead session's id, replayed on the new stream
        hardware_scores=to_proto(make_scoreset()),
    )
    with capture_logs() as logs:
        await manager.handle_message(new, stale)
    await _settle()
    assert registry.calls_named("set_hardware_scores") == []
    assert new.hardware_scores_rejected == 0  # never reached the GPU check
    assert new.stale_messages == 1
    rejected = [e for e in logs if e["event"] == "session.hardware_scores_rejected"]
    assert rejected == []
    acks = [m for m in drain(new) if m is not None and m.HasField("ack")]
    assert acks == []  # a stale-session message is never acked


# --------------------------------------------------------- D28: pending Hello


@pytest.mark.asyncio
async def test_pending_hello_claim_does_not_change_what_the_old_sessions_reports_see() -> (
    None
):
    """While a superseding Hello (declaring no GPU - the GPU vanished) is stuck
    admitting (D28's window: its epoch is already claimed but ``get_node``
    has not returned), a report that arrives on the *old*, still-open stream
    must still be checked against the *old* session's declared GPUs, not some
    shared or half-updated state. Only once the new Hello's admission
    completes does a report on the *new* stream see the new (empty) set."""
    registry = GatedRegistry()
    registry.gate.set()  # let the first (uncontested) admission through freely
    manager = make_manager(registry, FakeClock())
    node_id = new_node_id()
    old = await admit(manager, _hello(node_id, 1, 0))
    assert old.declared_gpus == frozenset({0})

    registry.gate.clear()
    registry.entered.clear()
    admit_task = asyncio.create_task(manager.admit(_hello(node_id, 2), "ipv4:2"))
    await asyncio.wait_for(registry.entered.wait(), 5.0)
    assert manager.admission_pending(old)  # the D28 claim is active

    # The old session is still open; a legitimate report for its own GPU must
    # still be accepted while the newer Hello is stuck admitting.
    run = to_proto(make_scoreset())
    await manager.handle_message(old, _report(old, "still-old", run))
    await _settle()
    calls = registry.calls_named("set_hardware_scores")
    assert len(calls) == 1 and calls[0][1]["session_id"] == old.session_id
    assert old.hardware_scores_rejected == 0

    registry.gate.set()
    result = await asyncio.wait_for(admit_task, 5.0)
    assert isinstance(result, Accepted)
    new = result.live
    assert new.declared_gpus == frozenset()

    # Now the same body, on the new session, must be rejected.
    with capture_logs() as logs:
        await manager.handle_message(new, _report(new, "now-new", run))
    await _settle()
    assert new.hardware_scores_rejected == 1
    (rejected,) = [e for e in logs if e["event"] == "session.hardware_scores_rejected"]
    assert rejected["session_id"] == new.session_id
    assert rejected["reason"].startswith("gpu_index_not_declared")


# ---------------------------------------------------- duplicate / index edges


def test_declared_gpus_collapses_duplicate_hello_indices() -> None:
    """``Hello.hardware.gpus`` with a repeated index (a broken or hostile
    probe/peer) must not raise or duplicate membership - it is a set."""
    hardware = agent_pb2.HardwareSummary(cpu_model="cpu", cpu_cores=4)
    hardware.gpus.append(agent_pb2.HardwareSummary.Gpu(index=0, model="a"))
    hardware.gpus.append(agent_pb2.HardwareSummary.Gpu(index=0, model="a-again"))
    hardware.gpus.append(agent_pb2.HardwareSummary.Gpu(index=2, model="b"))
    assert declared_gpus(hardware) == frozenset({0, 2})


@pytest.mark.asyncio
async def test_index_0_is_rejected_when_only_index_1_is_declared() -> None:
    """The mirror of the existing 'only 0 declared, index 1 named' unit test:
    a node whose one GPU is index 1 (e.g. index 0 is an integrated/non-CUDA
    device NVML still enumerates) must not accept a score for index 0."""
    registry = ScoresRegistry()
    manager = make_manager(registry, FakeClock())
    node_id = new_node_id()
    live = await admit(manager, _hello(node_id, 1, 1))  # only GPU 1 declared
    assert live.declared_gpus == frozenset({1})

    run = to_proto(make_scoreset())  # names gpu_index=0
    with capture_logs() as logs:
        await manager.handle_message(live, _report(live, "idx0", run))
    await _settle()
    assert registry.calls_named("set_hardware_scores") == []
    assert live.hardware_scores_rejected == 1
    (rejected,) = [e for e in logs if e["event"] == "session.hardware_scores_rejected"]
    assert rejected["reason"].startswith("gpu_index_not_declared")
