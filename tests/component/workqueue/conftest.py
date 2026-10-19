"""V0.10 G2: real PostgreSQL (one fresh database per test, ``pg_settings``)
migrated to head, plus an engine/session factory sized for concurrent pullers."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

import pytest_asyncio
from mf_workqueue import Db, open_db

from meshforge.controller.store.settings import DatabaseSettings


@pytest_asyncio.fixture
async def db(pg_settings: DatabaseSettings) -> AsyncIterator[Db]:
    async with open_db(pg_settings) as opened:
        yield opened
