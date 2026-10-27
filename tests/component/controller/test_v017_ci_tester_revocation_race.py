"""Tester (V0.17 CI-green fix): try to break the revocation snapshot on real
PostgreSQL - concurrent revoke/reinstate, sync vs REST revoke, report-once."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.ids import new_node_id
from meshforge.controller.revocation import Revocations, RevocationStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def _node(engine: AsyncEngine, serial: str) -> str:
    node = str(new_node_id())
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO node (node_id, hostname, os, platform, hardware, state, "
                "first_seen_at, last_seen_at, session_epoch, cert_serial, "
                "cert_fingerprint, cert_not_after, public_key_fingerprint) VALUES "
                "(:n, 'h', 'l', 'p', '{}', 'online', now(), now(), 1, :s, :f, "
                "now() + interval '30 d', :k)"
            ),
            {"n": node, "s": serial, "f": serial[:2] * 32, "k": "ef" * 32},
        )
    return node


async def _reinstate(engine: AsyncEngine, node: str) -> None:
    """An operator reinstating a node: ONE transaction (state + serial row)."""
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE node SET state='online' WHERE node_id=:n"), {"n": node}
        )
        await conn.execute(
            text("DELETE FROM revoked_cert WHERE node_id=:n"), {"n": node}
        )


class Sessions:
    def __init__(self) -> None:
        self.live: set[str] = set()
        self.node_cuts: list[str] = []
        self.cert_cuts: list[frozenset[str]] = []

    def has_session(self, node_id: str) -> bool:
        return node_id in self.live

    async def revoke_node(self, node_id: str) -> bool:
        await asyncio.sleep(0)
        self.live.discard(node_id)
        self.node_cuts.append(node_id)
        return True

    async def cut_revoked_certs(self, serials: Any) -> frozenset[str]:
        # a live node whose serial is listed but which is NOT cut as revoked
        # would be the "cert_superseded" mis-cut seen on CI
        self.cert_cuts.append(frozenset(serials))
        return frozenset()

    def broadcast(self, payload_name: str, payload: Any) -> int:
        return 0


async def test_snapshot_is_never_torn_under_concurrent_revoke_and_reinstate(
    engine: AsyncEngine,
) -> None:
    nodes = {await _node(engine, f"{i:02x}" * 20): f"{i:02x}" * 20 for i in range(1, 7)}
    store = RevocationStore(engine)
    stop = asyncio.Event()

    async def churn(node: str) -> None:
        while not stop.is_set():
            await store.revoke_node(node, actor="t")
            await _reinstate(engine, node)

    async def reader() -> int:
        n = 0
        while not stop.is_set():
            snap = await store.snapshot()
            for node, serial in nodes.items():
                assert (node in snap.nodes) == (serial in snap.serials), (node, snap)
            n += 1
        return n

    churners = [asyncio.create_task(churn(n)) for n in nodes]
    readers = [asyncio.create_task(reader()) for _ in range(4)]
    await asyncio.sleep(6)
    stop.set()
    counts = await asyncio.gather(*readers)
    await asyncio.gather(*churners)
    assert min(counts) > 20, counts


async def test_sync_racing_rest_revoke_never_cuts_as_superseded_and_reports_once(
    engine: AsyncEngine,
) -> None:
    sessions = Sessions()
    store = RevocationStore(engine)
    rev = Revocations(store, sessions)  # type: ignore[arg-type]
    await rev.sync()
    for rnd in range(25):
        serial = f"{0x40 + rnd:02x}" * 20
        node = await _node(engine, serial)
        sessions.live.add(node)
        sessions.cert_cuts.clear()
        syncers = [asyncio.create_task(rev.sync()) for _ in range(3)]
        outcomes = await asyncio.gather(
            *[rev.revoke(node, actor=f"r{i}") for i in range(3)]
        )
        await asyncio.gather(*syncers)
        assert node not in sessions.live
        assert sessions.node_cuts.count(node) == 1  # cut exactly once
        fresh = [o for o in outcomes if not o.already]
        assert len(fresh) == 1
        assert fresh[0].session_killed is True, (rnd, outcomes)  # reported
        assert not any(o.session_killed for o in outcomes if o.already), outcomes
        # a later revoke of the same node reports nothing cut
        again = await rev.revoke(node, actor="late")
        assert again.already and not again.session_killed


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT (minor): Revocations._revoke_cuts keeps a cut of an earlier "
    "revocation when the node is reinstated and re-revoked between two syncs; "
    "the new REST revoke is credited session_killed=true with no session cut",
)
async def test_reinstated_then_rerevoked_node_is_not_credited_a_stale_cut(
    engine: AsyncEngine,
) -> None:
    """Cut by sync (CLI revoke), reinstate + re-revoke WITHOUT a sync in
    between, then a REST revoke finds no session: must not claim a kill."""
    sessions = Sessions()
    store = RevocationStore(engine)
    rev = Revocations(store, sessions)  # type: ignore[arg-type]
    node = await _node(engine, "77" * 20)
    sessions.live.add(node)
    await store.revoke_node(node, actor="cli")  # other process
    assert await rev.sync() == frozenset({node})  # periodic sync cut it
    await _reinstate(engine, node)  # no sync saw the reinstate
    out = await rev.revoke(node, actor="rest")  # new revoke, no live session
    assert out.already is False
    assert out.session_killed is False, "stale cut credited to a new revoke"
