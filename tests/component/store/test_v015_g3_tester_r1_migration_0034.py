"""V0.15 G3 tester r1 - migration 0034 with data and under concurrency."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]

INSERT = (
    "INSERT INTO ml_model_set (set_version, status, reason, gate, dataset_digest, "
    "feature_version, inputs) VALUES (:v, :s, :r, '{}'::jsonb, 'sha256:d', 1, '{}'::jsonb)"
)


async def _race(settings: DatabaseSettings, status: str, n: int) -> list[object]:
    engine = create_engine(settings, pool_size=n + 1)

    async def one(i: int) -> object:
        async with engine.connect() as conn:
            trans = await conn.begin()
            try:
                await conn.execute(
                    text(INSERT),
                    {"v": f"sha256:race{status}{i}", "s": status, "r": None},
                )
                await asyncio.sleep(0.5)  # hold the first insert open
                await trans.commit()
                return "ok"
            except (IntegrityError, DBAPIError) as exc:
                await trans.rollback()
                return exc

    try:
        return list(await asyncio.gather(*(one(i) for i in range(n))))
    finally:
        await engine.dispose()


async def _scalar(settings: DatabaseSettings, sql: str, **p: Any) -> Any:
    engine = create_engine(settings)
    try:
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), p)).scalar()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "pinned"])
async def test_the_partial_unique_index_holds_under_concurrent_inserts(
    pg_settings: DatabaseSettings, status: str
) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    results = await asyncio.wait_for(_race(pg_settings, status, 6), 60)
    assert results.count("ok") == 1, results
    count = await _scalar(
        pg_settings, "SELECT count(*) FROM ml_model_set WHERE status = :s", s=status
    )
    assert count == 1


@pytest.mark.asyncio
async def test_downgrade_with_data_then_upgrade_is_clean(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(INSERT), {"v": "sha256:a", "s": "active", "r": None}
            )
            await conn.execute(
                text(
                    "INSERT INTO ml_model (set_version, term, version, algorithm, "
                    "feature_version, dataset_digest, params, metrics, artefact_digest, "
                    "artefact_bytes) VALUES ('sha256:a', 'compute', 'sha256:m', 'ridge', "
                    "1, 'sha256:d', '{}'::jsonb, '{}'::jsonb, 'sha256:m', 5)"
                )
            )
    finally:
        await engine.dispose()
    await asyncio.to_thread(command.downgrade, cfg, "0033")
    assert (
        await _scalar(
            pg_settings,
            "SELECT count(*) FROM information_schema.tables WHERE table_name IN "
            "('ml_model', 'ml_model_set')",
        )
        == 0
    )
    await asyncio.to_thread(command.upgrade, cfg, "head")
    assert await _scalar(pg_settings, "SELECT count(*) FROM ml_model_set") == 0
    await asyncio.to_thread(command.check, cfg)
    # up twice is a no-op
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.mark.asyncio
async def test_status_update_cannot_create_two_active_sets(
    pg_settings: DatabaseSettings,
) -> None:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(INSERT), {"v": "sha256:1", "s": "active", "r": None}
            )
            await conn.execute(
                text(INSERT), {"v": "sha256:2", "s": "superseded", "r": None}
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE ml_model_set SET status='active' WHERE set_version='sha256:2'"
                    )
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE ml_model_set SET status='rejected' WHERE set_version='sha256:2'"
                    )
                )  # a rejected set needs a reason
    finally:
        await engine.dispose()
