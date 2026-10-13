# ruff: noqa: F811, RUF001, S112  # tester test file: seeded RNG, report prints, re-imported fixtures
"""V0.7 G6 tester r2 (T-7): more garbage `measured_at`, far-future values,
non-object score documents - on real PostgreSQL. Expiry of the others is never
blocked; a bound document is never touched, whatever it holds."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from sqlalchemy import text
from test_v07_g6_fix1_unbound_scores import (  # noqa: F401
    FRESH,
    NOW,
    OLD,
    WEEK,
    _doc,
    _has_scores,
    _node,
    _set_measured_at,
    env,
    migrated,
)

from meshforge.common.ids import new_node_id
from meshforge.controller.scores import expire_unbound_scores_once

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

GARBAGE: list[Any] = [
    "",
    "   ",
    "NaN",
    "2026-10-12T12:00:00+25:00",
    "２０２６-10-12",
    "2026-02-30",
    "x" * 1_000_000,
    True,
    False,
    3.14,
    [],
    ["2026-10-12T12:00:00Z"],
    {},
    "\u0000",
    "0001-01-01T00:00:00",
    "0001-01-01T00:00:00+14:00",
]


async def test_exotic_garbage_clears_and_blocks_nothing(env: Any) -> None:
    store, engine = env
    old = [await _node(store, _doc(OLD)) for _ in range(4)]
    fresh = [await _node(store, _doc(FRESH)) for _ in range(2)]
    garbage = []
    for value in GARBAGE:
        node_id = await _node(store, _doc(FRESH))
        try:
            await _set_measured_at(engine, node_id, value)
        except Exception:  # noqa: BLE001 - PostgreSQL refuses \u0000 in jsonb
            continue
        garbage.append(node_id)
    assert len(garbage) >= len(GARBAGE) - 1
    cleared = await expire_unbound_scores_once(store, WEEK, wall=lambda: NOW)
    assert cleared == len(old) + len(garbage)
    for n in old + garbage:
        assert not await _has_scores(store, n)
    for n in fresh:
        assert await _has_scores(store, n)


async def test_far_future_and_boundary_values_are_kept_or_expired_exactly(
    env: Any,
) -> None:
    store, _ = env
    cutoff = NOW - dt.timedelta(seconds=WEEK)
    future = await _node(store, _doc("9999-12-31T23:59:59+00:00"))
    future_naive = await _node(store, _doc("9999-12-31T23:59:59"))
    at_cutoff = await _node(store, _doc(cutoff.isoformat()))
    just_before = await _node(
        store, _doc((cutoff - dt.timedelta(microseconds=1)).isoformat())
    )
    # +14:00 offset, same instant as the cutoff -> kept; one second older -> gone
    off = cutoff.astimezone(dt.timezone(dt.timedelta(hours=14)))
    same_instant = await _node(store, _doc(off.isoformat()))
    older_offset = await _node(store, _doc((off - dt.timedelta(seconds=1)).isoformat()))
    assert await expire_unbound_scores_once(store, WEEK, wall=lambda: NOW) == 2
    for n in (future, future_naive, at_cutoff, same_instant):
        assert await _has_scores(store, n)
    for n in (just_before, older_offset):
        assert not await _has_scores(store, n)


async def test_non_object_documents_do_not_break_the_sweep(env: Any) -> None:
    store, engine = env
    good_old = await _node(store, _doc(OLD))
    kinds = ["[]", '"text"', "7", "true", "null", '[{"node_id": "x"}]']
    ids = []
    for k in kinds:
        n = await _node(store, _doc(FRESH))
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE node SET hardware_scores = CAST(:v AS jsonb) WHERE node_id=:n"
                ),
                {"v": k, "n": n},
            )
        ids.append(n)
    # must not raise; the old one is gone whatever the others do
    await expire_unbound_scores_once(store, WEEK, wall=lambda: NOW)
    assert not await _has_scores(store, good_old)


async def test_a_bound_document_is_never_touched_whatever_it_holds(env: Any) -> None:
    store, engine = env
    bound = []
    for value in ("not a time", OLD, 5, None, ...):
        node_id = str(new_node_id())
        await _node(store, _doc(OLD, node_id), node_id)
        await _set_measured_at(engine, node_id, value)
        bound.append(node_id)
    for _ in range(2):
        assert await expire_unbound_scores_once(store, WEEK, wall=lambda: NOW) == 0
    for n in bound:
        assert await _has_scores(store, n)
