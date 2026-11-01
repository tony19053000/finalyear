"""V0.10 G2: real PostgreSQL (one fresh database per test, ``pg_settings``)
migrated to head, plus an engine/session factory sized for concurrent pullers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from mf_conftest_gate import missing_requirement, skip_items_under

#: Why this directory cannot run here (``None``: it can). Not ``importorskip`` at
#: module level: that aborts collection of every other path on the command line.
MISSING = missing_requirement(
    [
        ("sqlalchemy", "needs the controller extra"),
        ("testcontainers", "needs the dev dependency group"),
    ]
)

if MISSING is None:
    from mf_workqueue import Db, open_db

    from meshforge.controller.store.settings import DatabaseSettings


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    skip_items_under(Path(__file__).parent, items, MISSING)


@pytest_asyncio.fixture
async def db(pg_settings: DatabaseSettings) -> AsyncIterator[Db]:
    async with open_db(pg_settings) as opened:
        yield opened
