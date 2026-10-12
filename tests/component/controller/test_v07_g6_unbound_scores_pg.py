"""V0.7 G6 item 0 (D161 N3): unbound (pre-V0.7-G5) score documents expire.

Real PostgreSQL: ``Registry.expire_unbound_scores(cutoff)`` clears exactly the
documents with no ``node_id`` binding measured before ``cutoff`` - never a bound
one, never a fresh unbound one, never another column - and the controller-side
helper applies ``[controller.planner] unbound_scores_max_age_s`` to it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import pytest_asyncio

from meshforge.common.ids import new_node_id, new_session_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.scores import expire_unbound_scores_once, measured_at_iso
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


def _doc(measured: dt.datetime, node_id: str | None) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "run_id": "r",
        "measured_at": measured_at_iso(measured),
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


async def test_only_old_unbound_documents_are_cleared(store: Registry) -> None:
    old = NOW - dt.timedelta(days=8)
    fresh = NOW - dt.timedelta(days=1)
    old_unbound = await _node(store, _doc(old, None))
    fresh_unbound = await _node(store, _doc(fresh, None))
    bound_id = str(new_node_id())
    old_bound = await _node(store, None)
    await store.upsert_node_on_hello(
        node_id=old_bound,
        hostname="laptop",
        os="TestOS",
        platform="x86_64",
        hardware={"cpu_threads": 8},
        session_epoch=2,
        session_id=str(new_session_id()),
        state=NodeState.OFFLINE,
        hardware_scores=_doc(old, old_bound),
    )
    misbound = await _node(store, _doc(old, bound_id))  # bound elsewhere: kept
    none = await _node(store, None)

    cleared = await store.expire_unbound_scores(NOW - dt.timedelta(days=7))
    assert cleared == 1

    async def scores(node_id: str) -> Any:
        record = await store.get_node(node_id)
        assert record is not None
        return record.hardware_scores

    assert await scores(old_unbound) is None
    assert await scores(fresh_unbound) is not None
    assert (await scores(old_bound))["node_id"] == old_bound
    assert (await scores(misbound))["node_id"] == bound_id  # bound_to refuses it
    assert await scores(none) is None
    hardware = await store.get_node(old_unbound)
    assert hardware is not None and hardware.hardware == {"cpu_threads": 8}

    # Idempotent: nothing else matches.
    assert await store.expire_unbound_scores(NOW - dt.timedelta(days=7)) == 0


async def test_the_boundary_is_strictly_older(store: Registry) -> None:
    cutoff = NOW - dt.timedelta(days=7)
    at = await _node(store, _doc(cutoff, None))
    before = await _node(store, _doc(cutoff - dt.timedelta(seconds=1), None))
    assert await store.expire_unbound_scores(cutoff) == 1
    at_record = await store.get_node(at)
    before_record = await store.get_node(before)
    assert at_record is not None and at_record.hardware_scores is not None
    assert before_record is not None and before_record.hardware_scores is None


async def test_the_helper_applies_the_max_age(store: Registry) -> None:
    eight = await _node(store, _doc(NOW - dt.timedelta(days=8), None))
    six = await _node(store, _doc(NOW - dt.timedelta(days=6), None))
    assert await expire_unbound_scores_once(store, 7 * 86_400.0, wall=lambda: NOW) == 1
    eight_record = await store.get_node(eight)
    six_record = await store.get_node(six)
    assert eight_record is not None and eight_record.hardware_scores is None
    assert six_record is not None and six_record.hardware_scores is not None
    # max age 0 clears every unbound document measured before now.
    assert await expire_unbound_scores_once(store, 0.0, wall=lambda: NOW) == 1


async def test_the_controller_expires_them_at_start(migrated: DatabaseSettings) -> None:
    """Wiring: a real controller start clears an old unbound document (and the
    running loop is configured from ``[controller.planner]``)."""
    from mf_jobs_harness import g2_settings, running_controller

    engine = create_engine(migrated)
    try:
        registry = Registry(engine)
        real_now = dt.datetime.now(dt.UTC)
        old = await _node(registry, _doc(real_now - dt.timedelta(days=30), None))
        fresh = await _node(registry, _doc(real_now - dt.timedelta(hours=1), None))
        settings = g2_settings(migrated, planner={"unbound_scores_max_age_s": 86_400.0})
        async with running_controller(settings):
            old_record = await registry.get_node(old)
            fresh_record = await registry.get_node(fresh)
        assert old_record is not None and old_record.hardware_scores is None
        assert fresh_record is not None and fresh_record.hardware_scores is not None
    finally:
        await engine.dispose()
