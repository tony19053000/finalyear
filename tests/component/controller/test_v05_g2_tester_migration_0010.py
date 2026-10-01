"""V0.5 G2 tester: migration 0010 on real PostgreSQL - repeated down/up cycles keep
the CHECK exact, and a downgrade rewrites only ``venv`` rows (docker, subprocess and
NULL are untouched)."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.asyncio

_SPEC = importlib.util.spec_from_file_location(
    "mf_g2_venv_helpers", Path(__file__).with_name("test_v05_attempt_runtime_venv.py")
)
assert _SPEC and _SPEC.loader
_HELPERS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPERS)


@pytest_asyncio.fixture
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


async def _check_def(pg_settings: DatabaseSettings) -> str:
    engine = create_engine(pg_settings)
    try:
        async with engine.connect() as conn:
            row = await conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_attempt_runtime'"
                )
            )
            return str(row.scalar_one())
    finally:
        await engine.dispose()


async def test_downgrade_rewrites_only_venv_rows_and_cycles_are_stable(
    store: ExecutionStore, pg_settings: DatabaseSettings
) -> None:
    venv_id = await _HELPERS._venv_attempt(store, pg_settings)
    docker_id = await _HELPERS._venv_attempt(store, pg_settings)
    null_id = await _HELPERS._venv_attempt(store, pg_settings)
    sub_id = await _HELPERS._venv_attempt(store, pg_settings)
    await _HELPERS._set_runtime(pg_settings, docker_id, "docker")
    await _HELPERS._set_runtime(pg_settings, sub_id, "subprocess")
    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE attempt SET runtime = NULL WHERE id = :i"), {"i": null_id}
            )
    finally:
        await engine.dispose()
    cfg = alembic_config(pg_settings)
    for _ in range(2):  # down/up twice: idempotent, no leftover constraint
        await asyncio.to_thread(command.downgrade, cfg, "0009")
        narrow = await _check_def(pg_settings)
        assert "venv" not in narrow and "docker" in narrow and "subprocess" in narrow
        rt = _HELPERS._runtime_of
        assert await rt(pg_settings, venv_id) == "subprocess"
        assert await rt(pg_settings, docker_id) == "docker"
        assert await rt(pg_settings, sub_id) == "subprocess"
        assert await rt(pg_settings, null_id) is None
        await asyncio.to_thread(command.upgrade, cfg, "head")
        wide = await _check_def(pg_settings)
        assert "venv" in wide and "docker" in wide and "subprocess" in wide
        assert await rt(pg_settings, docker_id) == "docker"
    # the empty string and case variants are still refused
    for bad in ("", "VENV", "Docker", "venv "):
        with pytest.raises(IntegrityError):
            await _HELPERS._set_runtime(pg_settings, venv_id, bad)
