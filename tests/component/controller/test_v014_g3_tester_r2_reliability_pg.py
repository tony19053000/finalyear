"""V0.14 G3 tester round 2 (real PostgreSQL): N3 with MORE historical nodes
than ``MAX_NODES`` - the reliability read must give every snapshot node its
exact counts and must not mention a node outside the snapshot."""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
from sqlalchemy.ext.asyncio import AsyncSession
from test_v014_g3_risk_pg import _ended, _history_job, _node, store  # noqa: F401

from meshforge.controller.jobs import queue_reads
from meshforge.controller.jobs.queue_reads import read_fabric
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.component, pytest.mark.asyncio]


async def test_v014_g3_tester_r2_reliability_with_more_nodes_than_the_cap(
    pg_settings,
    store,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    nodes = [await _node(pg_settings, 50.0 + i) for i in range(7)]
    job = await _history_job(store, pg_settings)
    token = [0]
    want: dict[str, tuple[int, int]] = {}
    for i, node in enumerate(nodes):
        # node i: i node-caused ends and 2 successes
        ends = [("lost", "node_lost", 60.0 * (k + 1)) for k in range(i)]
        ends += [("done", None, 10.0), ("done", None, 20.0)]
        await _ended(pg_settings, job, node, ends, token)
        want[node] = (i, i + 2)
    monkeypatch.setattr(queue_reads, "MAX_NODES", 3)
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn, AsyncSession(bind=conn) as db:
            since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
            fabric = await read_fabric(
                db, [], history_runs=20, keys_of=lambda _j: [], risk_since=since
            )
            seen = set(fabric.nodes)
            assert len(seen) == 3
            assert set(fabric.reliability) == seen  # every snapshot node, no other
            for node in seen:
                assert fabric.reliability[node] == want[node], node
    finally:
        await engine.dispose()
