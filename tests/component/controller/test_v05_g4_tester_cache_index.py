"""V0.5 G4 tester round 1: cache location index under hostile / racy report streams.

A hand-driven agent (FakeAgent: a real gRPC stream) sends exact CacheReport
sequences to the real controller with PostgreSQL; the assertions read the real
``cache_location`` table. Derived from D101 and brief 0.5.5, not the code.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import delete, func, select

from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.models.cache import CacheLocation
from meshforge.controller.store.models.node import Node

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(90)]

BLOB = agent_pb2.CacheItem.KIND_BLOB
TREE = agent_pb2.CacheItem.KIND_TREE
ENV = agent_pb2.CacheItem.KIND_ENV


def d(c: str) -> str:
    return "sha256:" + c * 64


def item(kind: int, key: str, size: int = 1) -> agent_pb2.CacheItem:
    return agent_pb2.CacheItem(kind=kind, key=key, size=size)


def report(
    seq: int,
    *,
    snap: bool = False,
    last: bool = False,
    add: tuple[agent_pb2.CacheItem, ...] = (),
    rem: tuple[agent_pb2.CacheItem, ...] = (),
    page: int = 0,
) -> agent_pb2.CacheReport:
    # page = CacheReport.page (fix round 2): the page's index in its snapshot;
    # optional since fix round 3, so it is set on snapshot pages only (a delta
    # carrying it is refused, as the agent never sends one).
    rep = agent_pb2.CacheReport(
        seq=seq, snapshot=snap, snapshot_last=last, added=add, removed=rem
    )
    if snap:
        rep.page = page
    return rep


async def send(agent: Any, rep: agent_pb2.CacheReport) -> None:
    await agent.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(), session_id=agent.session_id, cache_report=rep
        )
    )


async def keys(runtime: ControllerRuntime, node: str, kind: str = "blob") -> set[str]:
    assert runtime.cache_index is not None
    store = runtime.cache_index.store
    sess = store._sessions  # the real table, read through the store's own engine
    async with sess() as db:
        rows = await db.scalars(
            select(CacheLocation.key).where(
                CacheLocation.node_id == node, CacheLocation.kind == kind
            )
        )
        return set(rows.all())


async def until(check: Callable[[], Awaitable[bool]], timeout_s: float = 10.0) -> None:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if await check():
            return
        await asyncio.sleep(0.03)
    raise AssertionError("condition not reached")


async def expect(
    runtime: ControllerRuntime, node: str, want: set[str], kind: str = "blob"
) -> None:
    """Poll until the node's rows equal ``want`` and hold for 0.3 s (no late write)."""
    last: set[str] = set()

    async def ok() -> bool:
        nonlocal last
        last = await keys(runtime, node, kind)
        return last == want

    try:
        await until(ok, 8.0)
    except AssertionError:
        raise AssertionError(f"rows {sorted(last)} != {sorted(want)}") from None
    await asyncio.sleep(0.3)
    assert await keys(runtime, node, kind) == want, "rows changed after converging"


async def settle(runtime: ControllerRuntime, reports: int) -> None:
    """Wait until the controller has *seen* ``reports`` reports and flushed writes."""
    assert runtime.cache_index is not None
    idx = runtime.cache_index

    async def seen() -> bool:
        return idx.stats.reports >= reports

    await until(seen)
    await asyncio.sleep(0.4)  # the write chain runs behind the reader


async def test_delta_before_snapshot_last_is_never_applied(
    runtime: ControllerRuntime, agents: Any
) -> None:
    a = agents()
    await a.connect()
    await send(a, report(1, snap=True, add=(item(BLOB, d("1")),)))  # page 1, not last
    await send(a, report(2, add=(item(BLOB, d("2")),)))  # a delta slips in
    await send(a, report(3, add=(item(BLOB, d("3")),), rem=(item(BLOB, d("1")),)))
    await send(a, report(4, snap=True, last=True, add=(item(BLOB, d("4")),)))
    await settle(runtime, 4)
    # seq 4 does not continue 1 (next expected 2): it starts a NEW snapshot, so
    # only its page remains - and in no case did a delta alter the rows.
    got = await keys(runtime, a.node_id)
    assert d("2") not in got and d("3") not in got
    state = runtime.cache_index.state(a.node_id)  # type: ignore[union-attr]
    assert state is not None and state.phase.value == "live"


async def test_replayed_old_session_reports_cannot_resurrect_removed_items(
    runtime: ControllerRuntime, agents: Any
) -> None:
    a1 = agents(epoch=1)
    await a1.connect()
    await send(
        a1,
        report(1, snap=True, last=True, add=(item(BLOB, d("a")), item(BLOB, d("b")))),
    )
    await send(a1, report(2, rem=(item(BLOB, d("b")),)))
    await send(a1, report(3, add=(item(BLOB, d("c")),)))
    await settle(runtime, 3)
    assert await keys(runtime, a1.node_id) == {d("a"), d("c")}
    await a1.abort()
    # Same agent process reconnects (seq keeps counting), replays unacked old
    # reports, then sends its new snapshot. The old replay says "b was added".
    a2 = agents(node_id=a1.node_id, epoch=2)
    await a2.connect()
    await send(a2, report(2, add=(item(BLOB, d("b")),)))  # replay: must be dropped
    await send(a2, report(3, add=(item(BLOB, d("4")),)))
    await send(a2, report(4, snap=True, last=True, add=(item(BLOB, d("a")),)))
    await send(a2, report(5, add=(item(BLOB, d("e")),)))
    await expect(runtime, a1.node_id, {d("a"), d("e")})


async def test_duplicate_snapshot_page_and_duplicate_delta_are_idempotent(
    runtime: ControllerRuntime, agents: Any
) -> None:
    a = agents()
    await a.connect()
    page = report(1, snap=True, last=True, add=(item(BLOB, d("a")),))
    await send(a, page)
    await send(a, page)  # exact duplicate
    delta = report(2, add=(item(BLOB, d("b")),))
    await send(a, delta)
    await send(a, delta)
    await send(a, report(3, rem=(item(BLOB, d("b")),)))
    await send(a, delta)  # duplicate of an OLD delta must not re-add b
    await settle(runtime, 6)
    assert await keys(runtime, a.node_id) == {d("a")}


async def test_gap_makes_stale_then_next_snapshot_heals_and_deltas_resume(
    runtime: ControllerRuntime, agents: Any
) -> None:
    a = agents()
    await a.connect()
    await send(a, report(1, snap=True, last=True, add=(item(BLOB, d("a")),)))
    await send(a, report(3, add=(item(BLOB, d("3")),)))  # seq 2 missing
    await send(a, report(4, add=(item(BLOB, d("1")),)))  # dropped: stale
    await settle(runtime, 3)
    assert await keys(runtime, a.node_id) == {d("a")}
    assert runtime.cache_index.state(a.node_id).phase.value == "stale"  # type: ignore[union-attr]
    await send(
        a, report(5, snap=True, last=True, add=(item(BLOB, d("a")), item(BLOB, d("1"))))
    )
    await send(a, report(6, add=(item(BLOB, d("2")),)))
    await expect(runtime, a.node_id, {d("a"), d("1"), d("2")})


async def test_a_lost_snapshot_page_must_not_leave_the_node_live(
    runtime: ControllerRuntime, agents: Any
) -> None:
    a = agents()
    await a.connect()
    await send(a, report(1, snap=True, add=(item(BLOB, d("8")),)))
    # page seq 2 never arrives (e.g. it was refused as invalid and dropped)
    await send(a, report(3, snap=True, last=True, page=2, add=(item(BLOB, d("9")),)))
    await settle(runtime, 2)
    state = runtime.cache_index.state(a.node_id)  # type: ignore[union-attr]
    assert state is not None and state.phase.value != "live"


async def test_invalid_reports_do_not_corrupt_and_do_not_kill_the_session(
    runtime: ControllerRuntime, agents: Any
) -> None:
    a = agents()
    await a.connect()
    await send(a, report(1, snap=True, last=True, add=(item(BLOB, d("a")),)))
    bad = [
        report(0, add=(item(BLOB, d("0")),)),
        report(2, add=(item(BLOB, "sha256:" + "G" * 64),)),
        report(2, add=(item(BLOB, "../../etc/passwd"),)),
        report(2, add=(item(BLOB, d("a") + "\x00"),)),
        report(2, add=(item(99, d("a")),)),
        report(2, add=(item(ENV, d("a")),)),  # env key must be bare hex
        report(2, add=tuple(item(BLOB, d("f")) for _ in range(1025))),
        report(2, snap=True, last=False, rem=(item(BLOB, d("a")),)),
    ]
    for r in bad:
        await send(a, r)
    await settle(runtime, 1 + len(bad))
    assert a.ended.is_set() is False
    assert await keys(runtime, a.node_id) == {d("a")}
    stats = runtime.cache_index.stats  # type: ignore[union-attr]
    assert sum(stats.dropped.values()) >= len(bad) - 1


async def test_negative_and_huge_sizes_never_break_the_table_or_the_summary(
    runtime: ControllerRuntime, agents: Any
) -> None:
    a = agents()
    await a.connect()
    await send(
        a,
        report(
            1,
            snap=True,
            last=True,
            add=(
                item(BLOB, d("a"), (1 << 63) - 1),
                item(BLOB, d("b"), (1 << 63) - 1),
                item(BLOB, d("c"), 0),
            ),
        ),
    )
    await settle(runtime, 1)
    summary = await runtime.cache_index.store.summary(a.node_id)  # type: ignore[union-attr]
    assert summary["blob"][0] == 3  # counts survive; bytes may be huge but exact
    assert summary["blob"][1] == 2 * ((1 << 63) - 1)


async def test_node_removed_while_connected_late_reports_do_not_crash_or_resurrect(
    runtime: ControllerRuntime, agents: Any, pg_settings: Any
) -> None:
    a = agents()
    await a.connect()
    await send(a, report(1, snap=True, last=True, add=(item(BLOB, d("a")),)))
    await settle(runtime, 1)
    engine = create_engine(pg_settings)
    try:
        factory = create_session_factory(engine)
        async with factory() as db, db.begin():
            await db.execute(delete(Node).where(Node.node_id == a.node_id))
        async with factory() as db:
            left = await db.scalar(
                select(func.count())
                .select_from(CacheLocation)
                .where(CacheLocation.node_id == a.node_id)
            )
            assert left == 0  # FK cascade
        await send(a, report(2, add=(item(BLOB, d("5")),)))
        await send(a, report(3, snap=True, last=True, add=(item(BLOB, d("6")),)))
        await settle(runtime, 3)
        async with factory() as db:
            left = await db.scalar(
                select(func.count())
                .select_from(CacheLocation)
                .where(CacheLocation.node_id == a.node_id)
            )
            assert left == 0, "a removed node's rows were resurrected"
    finally:
        await engine.dispose()
    # The controller still serves other nodes.
    b = agents()
    await b.connect()
    await send(b, report(1, snap=True, last=True, add=(item(BLOB, d("7")),)))
    await expect(runtime, b.node_id, {d("7")})


async def test_two_nodes_do_not_see_each_others_rows_and_holders_filters(
    runtime: ControllerRuntime, agents: Any
) -> None:
    a, b = agents(), agents()
    await a.connect()
    await b.connect()
    await send(
        a, report(1, snap=True, last=True, add=(item(TREE, d("7")), item(BLOB, d("7"))))
    )
    await send(b, report(1, snap=True, last=True, add=(item(BLOB, d("7")),)))
    await settle(runtime, 2)
    store = runtime.cache_index.store  # type: ignore[union-attr]
    both = await store.holders("blob", d("7"), among=[a.node_id, b.node_id])
    assert {n for n, _ in both} == {a.node_id, b.node_id}
    assert {
        n for n, _ in await store.holders("tree", d("7"), among=[a.node_id, b.node_id])
    } == {a.node_id}
    assert await store.holders("tree", d("7"), among=[]) == []
    # A tree key is never confused with a blob key of the same digest.
    assert await store.holders("blob", d("8"), among=[a.node_id]) == []


async def test_snapshot_of_many_pages_replaces_exactly(
    runtime: ControllerRuntime, agents: Any
) -> None:
    a = agents()
    await a.connect()
    first = tuple(item(BLOB, f"sha256:{n:064x}") for n in range(1024))
    await send(a, report(1, snap=True, last=True, add=first))
    await settle(runtime, 1)
    assert len(await keys(runtime, a.node_id)) == 1024
    pages = [
        tuple(item(BLOB, f"sha256:{5000 + p * 1024 + n:064x}") for n in range(1024))
        for p in range(3)
    ]
    await send(a, report(2, snap=True, add=pages[0]))
    await send(a, report(3, snap=True, page=1, add=pages[1]))
    await send(a, report(4, snap=True, last=True, page=2, add=pages[2]))
    await settle(runtime, 4)
    got = await keys(runtime, a.node_id)
    assert len(got) == 3 * 1024 and not (got & {i.key for i in first})
