"""V0.10 G2 work-queue test helpers: a migrated database with an engine sized
for concurrent pullers, a fixed clock origin and a digest constant."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

T0 = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.UTC)


def at(seconds: float) -> dt.datetime:
    return T0 + dt.timedelta(seconds=seconds)


def digest(n: int) -> str:
    return f"sha256:{n:064x}"


@dataclass
class Db:
    settings: DatabaseSettings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@asynccontextmanager
async def open_db(
    settings: DatabaseSettings, *, migrate: bool = True, pool_size: int = 24
) -> AsyncIterator[Db]:
    if migrate:
        await asyncio.to_thread(command.upgrade, alembic_config(settings), "head")
    engine = create_engine(settings, pool_size=pool_size, max_overflow=4)
    try:
        yield Db(settings, engine, create_session_factory(engine))
    finally:
        await engine.dispose()
