"""V0.3 G4 component tests: reports -> live ring -> ``resource_snapshot`` rows.

Real ControllerRuntime (gRPC + uvicorn) on ephemeral loopback ports against a real
PostgreSQL from ``pg_settings``, driven by the hand-rolled ``FakeAgent`` so a test
can send exactly the report it wants (including one no well-behaved agent would
send). Buckets are 1 s and the flusher runs every 0.2 s so the whole file stays
seconds, not minutes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from collections.abc import Callable
from typing import Any

import grpc
import pytest
from mf_controller_harness import SHUTDOWN_DRAIN_S, make_settings
from mf_session_client import FakeAgent, poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.ids import new_command_id, new_node_id, new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.resources import SnapshotRow
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

FAST_HISTORY: dict[str, Any] = {
    "bucket_s": 1.0,
    "flush_interval_s": 0.2,
    "retention_sweep_s": 3600.0,
    "live_ring_size": 50,
}
TIMEOUT_S = 15.0


async def send_update(
    agent: FakeAgent,
    *,
    sequence: int,
    cpu: float = 10.0,
    observed_at: dt.datetime | None = None,
    disks: int = 1,
    message_id: str | None = None,
) -> str:
    update = agent_pb2.ResourceUpdate(
        sequence=sequence,
        cpu_percent=cpu,
        ram_used_bytes=1_000 * sequence,
        ram_total_bytes=8_000,
    )
    update.observed_at.FromDatetime(observed_at or dt.datetime.now(dt.UTC))
    for index in range(disks):
        update.disks.add(mount=f"/m{index}", free_bytes=500, total_bytes=1_000)
    envelope = agent_pb2.AgentMessage(
        message_id=message_id or new_command_id(),
        session_id=agent.session_id,
        resource_update=update,
    )
    await agent.write(envelope)
    return envelope.message_id


def snapshot_row(node_id: str, bucket: dt.datetime, samples: int = 2) -> SnapshotRow:
    return SnapshotRow(
        node_id=node_id,
        bucket_start=bucket,
        samples=samples,
        cpu_percent_mean=12.5,
        cpu_percent_max=20.0,
        ram_used_bytes_mean=100,
        ram_used_bytes_max=200,
        ram_total_bytes=8_000,
        gpu={"0": {"util_mean": 1.0, "util_max": 2.0, "vram_total": 3}},
        disks={"/": {"free_min": 1, "free_mean": 2, "total": 3}},
    )


@pytest.fixture
def history_settings(pg_settings: DatabaseSettings) -> Any:
    return make_settings(pg_settings, resources=dict(FAST_HISTORY))


# ------------------------------------------------------------------ live ring


@pytest.mark.asyncio
async def test_reports_land_in_the_live_ring_in_order_and_a_replay_is_not_counted_twice(
    history_settings: Any,
) -> None:
    async with ControllerRuntime.start(history_settings) as runtime:
        assert runtime.resources is not None
        async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch:
            agent = FakeAgent(ch, node_id=new_node_id())
            await agent.connect()
            tracker = runtime.resources
            try:
                ids = [await send_update(agent, sequence=n) for n in (1, 2, 3)]
                assert len(set(ids)) == 3
                await wait_for(lambda: _ring_len(tracker, agent.node_id) == 3)
                ring = tracker.ring(agent.node_id)
                assert ring is not None
                assert [s.sequence for s in ring.snapshot()] == [1, 2, 3]
                assert ring.snapshot()[0].received_at <= ring.snapshot()[2].received_at
            finally:
                await agent.close()

        # A *real* reconnect: the agent process is the same (its report sequence
        # keeps counting) but `identity.next_epoch` bumps per connection attempt,
        # so the replay arrives under a higher epoch with the same sequences and
        # brand new message_ids - nothing but the tracker's cursor can catch it.
        async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch:
            again = FakeAgent(ch, node_id=agent.node_id, epoch=agent.epoch + 1)
            await again.connect()
            try:
                await again.write(
                    agent_pb2.AgentMessage(
                        message_id=new_command_id(),
                        session_id=again.session_id,
                        reconcile=agent_pb2.Reconcile(replay_from_sequence=2),
                    )
                )
                for sequence in (2, 3):  # the un-acked tail of the replay ring
                    await send_update(again, sequence=sequence)
                await wait_for(lambda: tracker.duplicates == 2)
                assert _ring_len(tracker, again.node_id) == 3, (
                    "a replayed report must not occupy a second ring slot"
                )
                # ... and the first genuinely new report still gets through.
                await send_update(again, sequence=4)
                await wait_for(lambda: _ring_len(tracker, again.node_id) == 4)
                ring = tracker.ring(again.node_id)
                assert ring is not None
                assert [s.sequence for s in ring.snapshot()] == [1, 2, 3, 4]
                assert tracker.duplicates == 2
            finally:
                await again.close()


def _ring_len(tracker: Any, node_id: str) -> int:
    ring = tracker.ring(node_id)
    return 0 if ring is None else len(ring)


async def wait_for(check: Callable[[], bool], timeout_s: float = TIMEOUT_S) -> None:
    """``poll_until`` for a synchronous predicate (it awaits its argument)."""

    async def predicate() -> bool:
        return check()

    await poll_until(predicate, timeout_s=timeout_s)


@pytest.mark.asyncio
async def test_a_wild_report_is_clamped_counted_once_and_the_session_survives(
    history_settings: Any,
) -> None:
    async with ControllerRuntime.start(history_settings) as runtime:
        assert runtime.resources is not None and runtime.manager is not None
        async with grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch:
            agent = FakeAgent(ch, node_id=new_node_id())
            await agent.connect()
            try:
                for sequence in (1, 2):
                    await send_update(
                        agent, sequence=sequence, cpu=900.0, disks=MANY_DISKS
                    )
                tracker = runtime.resources
                await wait_for(lambda: _ring_len(tracker, agent.node_id) == 2)
                live = runtime.manager.get(agent.node_id)
                assert live is not None
                assert live.resource_updates == 2
                assert live.resource_updates_clamped == 2
                assert not live.ended
                latest = tracker.latest(agent.node_id)
                assert latest is not None
                assert latest.cpu_percent == 100.0
                assert len(latest.disks) == 16
            finally:
                await agent.close()


MANY_DISKS = 20


# --------------------------------------------------------------- persistence


@pytest.mark.asyncio
async def test_closed_buckets_become_rows_whose_sample_count_matches(
    history_settings: Any, registry: Registry
) -> None:
    async with (
        ControllerRuntime.start(history_settings) as runtime,
        grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch,
    ):
        agent = FakeAgent(ch, node_id=new_node_id())
        await agent.connect()
        try:
            bucket = dt.datetime.now(dt.UTC).replace(microsecond=0)
            for sequence in (1, 2, 3):
                await send_update(
                    agent,
                    sequence=sequence,
                    cpu=float(sequence * 10),
                    observed_at=bucket + dt.timedelta(milliseconds=sequence),
                )

            async def has_rows() -> bool:
                return bool(await registry.list_snapshots(agent.node_id))

            await poll_until(has_rows, timeout_s=TIMEOUT_S)
            rows = await registry.list_snapshots(agent.node_id)
            assert len(rows) == 1
            assert rows[0].samples == 3
            assert rows[0].cpu_percent_mean == pytest.approx(20.0, abs=0.01)
            assert rows[0].cpu_percent_max == pytest.approx(30.0, abs=0.01)
            assert rows[0].ram_total_bytes == 8_000
            assert rows[0].disks["/m0"]["total"] == 1_000
            assert rows[0].bucket_start.tzinfo is not None
        finally:
            await agent.close()


@pytest.mark.asyncio
async def test_writing_the_same_bucket_twice_updates_the_row(
    history_settings: Any, registry: Registry
) -> None:
    """The primary key is the bucket, so a re-flush upserts (roadmap 0.3.11)."""
    async with (
        ControllerRuntime.start(history_settings) as runtime,
        grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch,
    ):
        agent = FakeAgent(ch, node_id=new_node_id())
        await agent.connect()
        await send_update(agent, sequence=1)
        await wait_for(lambda: _ring_len(runtime.resources, agent.node_id) == 1)
        await agent.close()

        bucket = dt.datetime.now(dt.UTC).replace(microsecond=0)
        first = snapshot_row(agent.node_id, bucket, samples=2)
        assert await registry.upsert_resource_snapshots([first]) == 1
        second = snapshot_row(agent.node_id, bucket, samples=9)
        assert await registry.upsert_resource_snapshots([second]) == 1

        rows = await registry.list_snapshots(agent.node_id)
        assert len(rows) == 1
        assert rows[0].samples == 9


@pytest.mark.asyncio
async def test_a_poorer_re_flush_of_a_bucket_never_replaces_a_richer_row(
    history_settings: Any, registry: Registry
) -> None:
    """NB-3: after a controller restart a replayed report can re-accumulate a
    bucket from a *partial* sample set. That row must not overwrite the complete
    one already written (``samples`` 30 -> 2); an equal or better one must."""
    async with (
        ControllerRuntime.start(history_settings) as runtime,
        grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch,
    ):
        agent = FakeAgent(ch, node_id=new_node_id())
        await agent.connect()
        await send_update(agent, sequence=1)
        await wait_for(lambda: _ring_len(runtime.resources, agent.node_id) == 1)
        await agent.close()

        bucket = dt.datetime.now(dt.UTC).replace(microsecond=0)
        rich = snapshot_row(agent.node_id, bucket, samples=30)
        rich = dataclasses.replace(rich, cpu_percent_max=99.0)
        assert await registry.upsert_resource_snapshots([rich]) == 1

        poor = snapshot_row(agent.node_id, bucket, samples=2)
        poor = dataclasses.replace(poor, cpu_percent_max=1.0)
        assert await registry.upsert_resource_snapshots([poor]) == 1  # rows *sent*

        rows = await registry.list_snapshots(agent.node_id)
        assert len(rows) == 1
        assert rows[0].samples == 30, "the partial re-flush overwrote a good row"
        assert rows[0].cpu_percent_max == 99.0

        # The other direction: an equal-or-better row still wins.
        better = dataclasses.replace(
            snapshot_row(agent.node_id, bucket, samples=30), cpu_percent_max=42.0
        )
        assert await registry.upsert_resource_snapshots([better]) == 1
        rows = await registry.list_snapshots(agent.node_id)
        assert len(rows) == 1 and rows[0].cpu_percent_max == 42.0

        richer = dataclasses.replace(
            snapshot_row(agent.node_id, bucket, samples=31), cpu_percent_max=7.0
        )
        assert await registry.upsert_resource_snapshots([richer]) == 1
        rows = await registry.list_snapshots(agent.node_id)
        assert len(rows) == 1
        assert rows[0].samples == 31 and rows[0].cpu_percent_max == 7.0


@pytest.mark.asyncio
async def test_retention_deletes_the_old_rows_and_keeps_the_new_ones(
    history_settings: Any, registry: Registry
) -> None:
    node_id = new_node_id()
    async with (
        ControllerRuntime.start(history_settings) as runtime,
        grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch,
    ):
        assert runtime.flusher is not None
        agent = FakeAgent(ch, node_id=node_id)
        await agent.connect()
        await send_update(agent, sequence=1)
        await wait_for(lambda: _ring_len(runtime.resources, node_id) == 1)
        await agent.close()

        now = dt.datetime.now(dt.UTC).replace(microsecond=0)
        old = now - dt.timedelta(days=30)
        await registry.upsert_resource_snapshots(
            [snapshot_row(node_id, old), snapshot_row(node_id, now)]
        )
        deleted = await registry.delete_snapshots_before(now - dt.timedelta(days=7))
        assert deleted == 1
        remaining = await registry.list_snapshots(node_id)
        assert [row.bucket_start for row in remaining] == [now]

        # And the flusher's own sweep uses retention_days from the settings.
        assert await runtime.flusher.sweep_once() == 0


@pytest.mark.asyncio
async def test_list_snapshots_filters_by_window(
    history_settings: Any, registry: Registry
) -> None:
    node_id = new_node_id()
    async with (
        ControllerRuntime.start(history_settings) as runtime,
        grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch,
    ):
        agent = FakeAgent(ch, node_id=node_id)
        await agent.connect()
        await send_update(agent, sequence=1)
        await wait_for(lambda: _ring_len(runtime.resources, node_id) == 1)
        await agent.close()

        base = dt.datetime.now(dt.UTC).replace(microsecond=0)
        buckets = [base + dt.timedelta(minutes=i) for i in range(3)]
        await registry.upsert_resource_snapshots(
            [snapshot_row(node_id, bucket) for bucket in buckets]
        )
        window = await registry.list_snapshots(
            node_id, since=buckets[1], until=buckets[2]
        )
        assert [row.bucket_start for row in window] == [buckets[1]]
        assert len(await registry.list_snapshots(node_id, limit=2)) == 2
        assert await registry.list_snapshots(new_node_id()) == []


# ------------------------------------------------------------------- shutdown


@pytest.mark.asyncio
async def test_a_stop_mid_stream_flushes_the_closed_buckets_inside_the_deadline(
    history_settings: Any, registry: Registry
) -> None:
    node_id = new_node_id()
    async with (
        ControllerRuntime.start(history_settings) as runtime,
        grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch,
    ):
        agent = FakeAgent(ch, node_id=node_id)
        await agent.connect()
        past = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5)
        for sequence in (1, 2):
            await send_update(agent, sequence=sequence, observed_at=past)
        await wait_for(lambda: _ring_len(runtime.resources, node_id) == 2)
        loop = asyncio.get_running_loop()
        started = loop.time()
        # Stop while the stream is still open; the context manager's stop() is
        # idempotent, so the second call at exit is a no-op.
        await runtime.stop()
        took = loop.time() - started
    assert took < SHUTDOWN_DRAIN_S + 2.0
    rows = await registry.list_snapshots(node_id)
    assert len(rows) == 1
    assert rows[0].samples == 2


# ---------------------------------------------------------------------- hello


@pytest.mark.asyncio
async def test_an_oversize_hello_hardware_is_refused_and_nothing_is_persisted(
    history_settings: Any, registry: Registry
) -> None:
    async with (
        ControllerRuntime.start(history_settings) as runtime,
        grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch,
    ):
        hostile = agent_pb2.HardwareSummary(cpu_model="c" * 5_000, cpu_cores=4)
        agent = FakeAgent(ch, node_id=new_node_id())
        reply = await agent.hello(hardware=hostile)
        assert reply is None
        assert agent.end_code is grpc.StatusCode.INVALID_ARGUMENT
        assert "cpu_model" in agent.end_details
        assert "ccccc" not in agent.end_details
        assert await registry.get_node(agent.node_id) is None
        await agent.close()


@pytest.mark.asyncio
async def test_a_hello_with_sixteen_gpus_is_accepted_and_stored(
    history_settings: Any, registry: Registry
) -> None:
    async with (
        ControllerRuntime.start(history_settings) as runtime,
        grpc.aio.insecure_channel(f"127.0.0.1:{runtime.grpc_port}") as ch,
    ):
        summary = agent_pb2.HardwareSummary(
            cpu_model="a-test-cpu",
            cpu_cores=8,
            cpu_threads=16,
            ram_bytes=2**34,
            capabilities=["v0.3", "cuda"],
        )
        for index in range(16):
            summary.gpus.add(
                index=index,
                model="a-test-gpu",
                vram_bytes=2**32,
                driver_version="555.01",
                compute_capability="8.6",
            )
        agent = FakeAgent(ch, node_id=new_node_id())
        await agent.connect(hardware=summary)
        node = await registry.get_node(agent.node_id)
        assert node is not None
        assert len(node.hardware["gpus"]) == 16
        assert node.hardware["capabilities"] == ["v0.3", "cuda"]
        assert int(node.hardware["ram_bytes"]) == 2**34
        await agent.close()


# ------------------------------------------------------- internal_error reason


@pytest.mark.asyncio
async def test_the_internal_error_end_reason_is_accepted_by_the_schema(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """0003 widened ``node_session.end_reason``; the CHECK must allow the new value."""
    node_id = new_node_id()
    session_id = new_session_id()
    await registry.upsert_node_on_hello(
        node_id=node_id,
        hostname="component-test-laptop",
        os="TestOS 1.0",
        platform="test-x86_64",
        hardware={"cpu_model": "test-cpu"},
        session_epoch=1,
        session_id=session_id,
        state=NodeState.ENROLLING,
    )
    await registry.open_session(
        session_id=session_id,
        node_id=node_id,
        session_epoch=1,
        peer_address="ipv4:127.0.0.1:1",
        agent_version="0.1.0.dev0",
        protocol_version=1,
    )
    assert await registry.close_session(session_id, SessionEndReason.INTERNAL_ERROR)
    record = await registry.get_session(session_id)
    assert record is not None
    assert record.end_reason is SessionEndReason.INTERNAL_ERROR
