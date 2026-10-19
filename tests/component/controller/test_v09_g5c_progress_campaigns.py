"""V0.9 G5c item 0.6 (D252.3): ``BenchStore.progress_campaigns`` on real
PostgreSQL - started ``active`` / ``paused`` campaigns plus the ``also`` ids,
never a newest-N page (an old running campaign behind 60 newer ones is found).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_v09_g5c_runner_cooldown import (  # type: ignore[import-not-found]
    NODES,
    SPEC,
    SPEC_YAML,
    _setup,
)

from meshforge.bench.campaign import expand, parse_spec, spec_digest
from meshforge.common.ids import new_campaign_id
from meshforge.controller.bench.control import CampaignControl
from meshforge.controller.bench.store import BenchStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def _unstarted(store: BenchStore) -> str:
    spec = parse_spec(SPEC)
    cid = new_campaign_id()
    await store.create_campaign(
        campaign_id=cid,
        name=spec.name,
        spec_yaml=SPEC_YAML,
        spec_digest=spec_digest(spec),
        seed=spec.seed,
        expansion=expand(spec, NODES),
        nodes=[n.node_id for n in NODES],
        created_by=None,
    )
    return cid


async def test_started_open_campaigns_and_also_ids_only(engine: AsyncEngine) -> None:
    store, control = BenchStore(engine), CampaignControl(engine)
    _s, _c, _j, old_active = await _setup(engine, cooldown_s=0.0)
    _s, _c, _j, paused = await _setup(engine, cooldown_s=0.0)
    assert await control.pause(paused)
    _s, _c, _j, finished = await _setup(engine, cooldown_s=0.0)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE bench_campaign SET state = 'done' WHERE id = :c"),
            {"c": finished},
        )
    for _ in range(60):  # newer, never started: a newest-50 page would miss
        await _unstarted(store)
    found = [r.id for r in await store.progress_campaigns()]
    assert found == sorted([old_active, paused])
    with_final = [r.id for r in await store.progress_campaigns({finished})]
    assert with_final == sorted([old_active, paused, finished])
    rec = next(r for r in await store.progress_campaigns() if r.id == old_active)
    assert rec.counts and rec.started_at is not None
    unstarted = await _unstarted(store)
    assert unstarted not in {r.id for r in await store.progress_campaigns({unstarted})}
