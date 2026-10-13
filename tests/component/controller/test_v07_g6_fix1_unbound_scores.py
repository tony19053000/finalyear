"""V0.7 G6 fix round 1 (D162, T-7): legacy ``measured_at`` values of any shape.

Real PostgreSQL. ``Registry.expire_unbound_scores`` parses ``measured_at``
itself instead of casting it inside the UPDATE:

* unparseable / missing / null / non-text values never stop the others from
  expiring, and are cleared themselves (no provable age - fail closed);
* fresh unbound documents and **bound** documents (also bound ones with a
  garbage date) are never touched;
* a value without an offset is read as UTC.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.ids import new_node_id, new_session_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry, _parse_measured_at
from meshforge.controller.scores import expire_unbound_scores_once
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import upgrade_to_head
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

NOW = dt.datetime(2026, 10, 12, 12, 0, 0, tzinfo=dt.UTC)
OLD = (NOW - dt.timedelta(days=30)).isoformat()
FRESH = (NOW - dt.timedelta(days=1)).isoformat()
WEEK = 7 * 86_400.0


@pytest.fixture
def migrated(pg_settings: DatabaseSettings) -> DatabaseSettings:
    upgrade_to_head(pg_settings)
    return pg_settings


@pytest_asyncio.fixture
async def env(
    migrated: DatabaseSettings,
) -> AsyncIterator[tuple[Registry, AsyncEngine]]:
    engine = create_engine(migrated)
    try:
        yield Registry(engine), engine
    finally:
        await engine.dispose()


def _doc(measured_at: str, node_id: str | None = None) -> dict[str, Any]:
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


async def _node(
    store: Registry, doc: dict[str, Any], node_id: str | None = None
) -> str:
    node_id = node_id or str(new_node_id())
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


async def _set_measured_at(engine: AsyncEngine, node_id: str, value: Any) -> None:
    """Legacy garbage straight into the JSON (``...`` = remove the key)."""
    async with engine.begin() as conn:
        if value is ...:
            await conn.execute(
                text(
                    "UPDATE node SET hardware_scores = hardware_scores - 'measured_at' "
                    "WHERE node_id = :n"
                ),
                {"n": node_id},
            )
        else:
            await conn.execute(
                text(
                    "UPDATE node SET hardware_scores = jsonb_set(hardware_scores, "
                    "'{measured_at}', CAST(:v AS jsonb)) WHERE node_id = :n"
                ),
                {"n": node_id, "v": json.dumps(value)},
            )


async def _has_scores(store: Registry, node_id: str) -> bool:
    record = await store.get_node(node_id)
    assert record is not None
    return record.hardware_scores is not None


GARBAGE: list[Any] = [
    "not a time",
    "",
    "2026-13-45T99:99:99+00:00",
    "0000-00-00",
    "yesterday",
    1_700_000_000,
    None,
    {"nested": "object"},
    ...,
]


async def test_garbage_never_blocks_the_others_and_is_cleared_itself(
    env: tuple[Registry, AsyncEngine],
) -> None:
    store, engine = env
    old = [await _node(store, _doc(OLD)) for _ in range(5)]
    fresh = [await _node(store, _doc(FRESH)) for _ in range(3)]
    garbage = []
    for value in GARBAGE:
        node_id = await _node(store, _doc(FRESH))
        await _set_measured_at(engine, node_id, value)
        garbage.append(node_id)
    bound_old = [
        await _node(store, _doc(OLD, node_id), node_id)
        for node_id in (str(new_node_id()) for _ in range(3))
    ]
    bound_garbage = str(new_node_id())
    await _node(store, _doc(OLD, bound_garbage), bound_garbage)
    await _set_measured_at(engine, bound_garbage, "not a time")

    cleared = await expire_unbound_scores_once(store, WEEK, wall=lambda: NOW)
    assert cleared == len(old) + len(garbage)
    for node_id in old + garbage:
        assert not await _has_scores(store, node_id)
    for node_id in [*fresh, *bound_old, bound_garbage]:
        assert await _has_scores(store, node_id)
    # Idempotent: nothing left to clear.
    assert await expire_unbound_scores_once(store, WEEK, wall=lambda: NOW) == 0


async def test_a_value_without_an_offset_is_read_as_utc(
    env: tuple[Registry, AsyncEngine],
) -> None:
    store, _engine = env
    cutoff = NOW - dt.timedelta(days=7)
    older = await _node(
        store, _doc((cutoff - dt.timedelta(seconds=1)).replace(tzinfo=None).isoformat())
    )
    newer = await _node(
        store, _doc((cutoff + dt.timedelta(seconds=1)).replace(tzinfo=None).isoformat())
    )
    assert await store.expire_unbound_scores(cutoff) == 1
    assert not await _has_scores(store, older)
    assert await _has_scores(store, newer)


async def test_parse_measured_at() -> None:
    assert _parse_measured_at("2026-10-05T12:00:00+00:00") == NOW - dt.timedelta(days=7)
    assert _parse_measured_at("2026-10-05T12:00:00Z") == NOW - dt.timedelta(days=7)
    assert _parse_measured_at(" 2026-10-05T17:30:00+05:30 ") == (
        NOW - dt.timedelta(days=7)
    )
    assert _parse_measured_at("2026-10-05T12:00:00") == NOW - dt.timedelta(days=7)
    for bad in ("", "x", None, 5, 5.0, [], {}, "2026-13-01"):
        assert _parse_measured_at(bad) is None
