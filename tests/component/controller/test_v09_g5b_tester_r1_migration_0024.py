"""V0.9 G5 lane B, tester round 1: migration 0024 on real PostgreSQL with data.

Upgrade 0023 -> 0024 with existing campaigns / runs / jobs / attempts: old rows
get the documented defaults, new CHECKs bite (bad throttle, bad state, bad
finished_reason, negative tries), ``owner_preempted`` becomes legal on jobs and
attempts, and the downgrade with ``paused`` campaigns and ``owner_preempted``
failures in place maps them back (paused -> active, owner_preempted ->
cancelled) instead of failing on a constraint, then the chain goes on to base.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

_DEFAULTS_BY_TYPE: dict[str, str] = {
    "character varying": "'x'",
    "text": "'x'",
    "integer": "1",
    "bigint": "1",
    "smallint": "1",
    "boolean": "false",
    "double precision": "1",
    "real": "1",
    "numeric": "1",
    "jsonb": "'{}'::jsonb",
    "json": "'{}'::json",
    "timestamp with time zone": "now()",
    "timestamp without time zone": "now()",
    "uuid": "gen_random_uuid()",
}


async def _insert(conn: Any, table: str, **values: Any) -> None:
    """INSERT with every NOT NULL column without a default filled by type
    (FKs are not enforced: the session is a replica for the insert)."""
    cols = (
        await conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = :t AND is_nullable = 'NO' AND column_default IS NULL"
            ),
            {"t": table},
        )
    ).all()
    names, exprs, params = [], [], {}
    for name, dtype in cols:
        if name in values:
            continue
        names.append(name)
        exprs.append(_DEFAULTS_BY_TYPE[dtype])
    for name, value in values.items():
        names.append(name)
        exprs.append(f":{name}")
        params[name] = value
    await conn.execute(text("SET LOCAL session_replication_role = replica"))
    sql = f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(exprs)})"  # noqa: S608
    await conn.execute(text(sql), params)


async def test_0024_upgrade_defaults_constraints_and_downgrade_with_data(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    await asyncio.to_thread(command.upgrade, cfg, "0023")
    eng = create_engine(pg_settings, pool_size=3)
    try:
        async with eng.begin() as conn:
            await _insert(conn, "bench_campaign", id="camp_old", state="active")
            await _insert(conn, "bench_campaign", id="camp_done", state="done")
            await _insert(
                conn,
                "bench_run",
                id="brun_old",
                campaign_id="camp_old",
                state="submitted",
                job_id="job_old",
                strategy="local",
            )
            await _insert(
                conn,
                "job",
                id="job_old",
                state="failed",
                failure_type="cancelled",
                tree_id="sha256:" + "a" * 64,
            )
            await _insert(
                conn, "attempt", id="att_old", job_id="job_old", state="failed"
            )
        # 0023 does not know the new failure type
        with pytest.raises(IntegrityError):
            async with eng.begin() as conn:
                await conn.execute(
                    text("UPDATE job SET failure_type='owner_preempted'")
                )

        await asyncio.to_thread(command.upgrade, cfg, "0024")
        await asyncio.to_thread(command.upgrade, cfg, "head")  # idempotent

        async with eng.connect() as conn:
            camp = (
                await conn.execute(
                    text(
                        "SELECT max_concurrent_per_node, cooldown_s, max_infra_retries, "
                        "started_at, finished_reason FROM bench_campaign WHERE id='camp_old'"
                    )
                )
            ).one()
            assert tuple(camp) == (1, 5.0, 2, None, None)  # existing rows: defaults
            run = (
                await conn.execute(
                    text(
                        "SELECT tries, generation, last_failure, state, job_id "
                        "FROM bench_run WHERE id='brun_old'"
                    )
                )
            ).one()
            assert tuple(run) == (0, 0, None, "submitted", "job_old")

        async def rejected(sql: str) -> None:
            with pytest.raises(IntegrityError):
                async with eng.begin() as conn:
                    await conn.execute(text(sql))

        for bad in (
            "UPDATE bench_campaign SET max_concurrent_per_node = 0",
            "UPDATE bench_campaign SET max_concurrent_per_node = 65",
            "UPDATE bench_campaign SET cooldown_s = -0.1",
            "UPDATE bench_campaign SET cooldown_s = 86401",
            "UPDATE bench_campaign SET cooldown_s = 'NaN'",
            "UPDATE bench_campaign SET max_infra_retries = 11",
            "UPDATE bench_campaign SET max_infra_retries = -1",
            "UPDATE bench_campaign SET finished_reason = 'whenever'",
            "UPDATE bench_campaign SET state = 'running'",
            "UPDATE bench_run SET tries = -1",
            "UPDATE bench_run SET generation = -1",
            "UPDATE job SET failure_type = 'owner_preemptd'",
            "UPDATE attempt SET failure_type = 'OWNER_PREEMPTED'",
        ):
            await rejected(bad)

        async with eng.begin() as conn:
            await conn.execute(
                text("UPDATE bench_campaign SET state='paused' WHERE id='camp_old'")
            )
            await conn.execute(
                text(
                    "UPDATE bench_campaign SET finished_reason='budget' "
                    "WHERE id='camp_done'"
                )
            )
            await conn.execute(
                text("UPDATE job SET failure_type='owner_preempted' WHERE id='job_old'")
            )
            await conn.execute(
                text(
                    "UPDATE attempt SET failure_type='owner_preempted' WHERE id='att_old'"
                )
            )
            await conn.execute(
                text(
                    "UPDATE bench_run SET tries=2, generation=3, "
                    "last_failure=repeat('é', 128) WHERE id='brun_old'"
                )
            )
        with pytest.raises(Exception):  # noqa: B017 - too long for the column
            async with eng.begin() as conn:
                await conn.execute(
                    text("UPDATE bench_run SET last_failure=repeat('x', 129)")
                )

        # downgrade with the new values in place: mapped back, never an error
        await asyncio.to_thread(command.downgrade, cfg, "0023")
        async with eng.connect() as conn:
            assert (
                await conn.execute(
                    text("SELECT state FROM bench_campaign WHERE id='camp_old'")
                )
            ).scalar_one() == "active"
            assert (
                await conn.execute(
                    text("SELECT failure_type FROM job WHERE id='job_old'")
                )
            ).scalar_one() == "cancelled"
            assert (
                await conn.execute(
                    text("SELECT failure_type FROM attempt WHERE id='att_old'")
                )
            ).scalar_one() == "cancelled"
            columns = {
                r[0]
                for r in await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name IN ('bench_run','bench_campaign')"
                    )
                )
            }
            assert columns.isdisjoint(
                {
                    "tries",
                    "generation",
                    "last_failure",
                    "started_at",
                    "finished_reason",
                    "cooldown_s",
                    "max_infra_retries",
                    "max_concurrent_per_node",
                }
            )
        # the old constraints are back
        with pytest.raises(IntegrityError):
            async with eng.begin() as conn:
                await conn.execute(text("UPDATE bench_campaign SET state='paused'"))
        # and the round trip works with data (re-upgrade, then everything down)
        await asyncio.to_thread(command.upgrade, cfg, "head")
        async with eng.connect() as conn:
            assert (
                await conn.execute(
                    text("SELECT tries FROM bench_run WHERE id='brun_old'")
                )
            ).scalar_one() == 0
    finally:
        await eng.dispose()
        await asyncio.to_thread(command.downgrade, cfg, "base")
