"""V0.7 G6 tester r1, item 0 (D161 N3): unbound score expiry, boundary + hostile data.

Real PostgreSQL. The implementer's tests cover the 1-second boundary; these add
microsecond precision, offsets in ``measured_at``, many rows, and a legacy row
whose ``measured_at`` is garbage (one bad row must not stop every other expiry).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import pytest_asyncio
from sqlalchemy import text

from meshforge.common.ids import new_node_id, new_session_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.scores import expire_unbound_scores_once
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

NOW = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
def migrated(pg_settings: DatabaseSettings) -> DatabaseSettings:
    upgrade_to_head(pg_settings)
    return pg_settings


@pytest_asyncio.fixture
async def store(migrated: DatabaseSettings) -> AsyncIterator[Registry]:
    engine = create_engine(migrated)
    try:
        yield Registry(engine)
    finally:
        await engine.dispose()


def _doc(measured_at: Any, node_id: str | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "run_id": "r",
        "measured_at": measured_at,
        "score_version": 1,
        "fingerprint": "f",
        "conditions": {},
        "metrics": [],
    }
    if node_id is not None:
        doc["node_id"] = node_id
    return doc


async def _node(store: Registry, doc: dict[str, Any] | None) -> str:
    node_id = str(new_node_id())
    await store.upsert_node_on_hello(
        node_id=node_id,
        hostname="laptop",
        os="TestOS",
        platform="x86_64",
        hardware={"cpu_threads": 8},
        session_epoch=1,
        session_id=str(new_session_id()),
        state=NodeState.OFFLINE,
        hardware_scores=doc,
    )
    return node_id


async def _has_scores(store: Registry, node_id: str) -> bool:
    record = await store.get_node(node_id)
    assert record is not None
    return record.hardware_scores is not None


async def test_microsecond_precision_and_utc_offsets(store: Registry) -> None:
    cutoff = NOW - dt.timedelta(days=7)
    exactly = await _node(store, _doc(cutoff.isoformat()))
    one_us_before = await _node(
        store, _doc((cutoff - dt.timedelta(microseconds=1)).isoformat())
    )
    one_us_after = await _node(
        store, _doc((cutoff + dt.timedelta(microseconds=1)).isoformat())
    )
    # the same instant written with a +05:30 offset (older by 1 s) and a Z suffix
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    offset_old = await _node(
        store, _doc((cutoff - dt.timedelta(seconds=1)).astimezone(ist).isoformat())
    )
    z_new = await _node(
        store,
        _doc((cutoff + dt.timedelta(seconds=1)).isoformat().replace("+00:00", "Z")),
    )
    assert await store.expire_unbound_scores(cutoff) == 2
    assert await _has_scores(store, exactly)
    assert not await _has_scores(store, one_us_before)
    assert await _has_scores(store, one_us_after)
    assert not await _has_scores(store, offset_old)
    assert await _has_scores(store, z_new)


async def test_bulk_expiry_in_one_statement_leaves_bound_documents_alone(
    store: Registry,
) -> None:
    old = (NOW - dt.timedelta(days=30)).isoformat()
    unbound = [await _node(store, _doc(old)) for _ in range(40)]
    bound: list[str] = []
    for _ in range(10):
        node_id = str(new_node_id())
        bound.append(await _node(store, _doc(old, node_id)))
    assert await expire_unbound_scores_once(store, 7 * 86_400.0, wall=lambda: NOW) == 40
    for node_id in unbound:
        assert not await _has_scores(store, node_id)
    for node_id in bound:
        assert await _has_scores(store, node_id)


async def test_one_garbage_legacy_row_does_not_block_every_other_expiry(
    store: Registry, pg_settings: DatabaseSettings
) -> None:
    old = (NOW - dt.timedelta(days=30)).isoformat()
    good = await _node(store, _doc(old))
    bad = await _node(store, _doc(old))
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE node SET hardware_scores = jsonb_set(hardware_scores, "
                    "'{measured_at}', '\"not a time\"') WHERE node_id = :n"
                ),
                {"n": bad},
            )
    finally:
        await engine.dispose()
    cleared = await expire_unbound_scores_once(store, 7 * 86_400.0, wall=lambda: NOW)
    assert cleared >= 1 and not await _has_scores(store, good), (
        "T-7: one unparseable measured_at makes the whole expiry statement fail"
    )
