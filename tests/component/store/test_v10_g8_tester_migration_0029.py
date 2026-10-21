"""V0.10 G8 tester: migration 0029 (telemetry v3) on a real PostgreSQL.

Up from 0028 with existing rows, constraints enforced, down loses only what it
documents (mode3 bench runs; chunk records), re-up, ``alembic check`` clean.
"""

# ruff: noqa: PERF403, S608, PT017, E501, C408
from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import Coroutine
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


NOW = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


async def _insert_dummy(settings: DatabaseSettings, table: str, **over: Any) -> None:
    """Insert one row filling every NOT-NULL, default-less column with a dummy
    value of its type (FKs off for the session)."""
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET session_replication_role = replica"))
            cols = (
                await conn.execute(
                    text(
                        "SELECT column_name, data_type, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_name = :t AND table_schema = 'public'"
                    ),
                    {"t": table},
                )
            ).all()
            values: dict[str, Any] = {}
            for name, dtype, nullable, default in cols:
                if name in over:
                    values[name] = over[name]
                elif nullable == "YES" or default is not None:
                    continue
                elif dtype in ("bigint", "integer", "smallint"):
                    values[name] = 1
                elif dtype == "boolean":
                    values[name] = False
                elif dtype.startswith("timestamp"):
                    values[name] = NOW
                elif dtype in ("double precision", "real", "numeric"):
                    values[name] = 1.0
                elif dtype in ("jsonb", "json"):
                    values[name] = json.dumps({})
                else:
                    values[name] = "x"
            if table == "run_record":
                values.update({"failure_class": "ok", "placement_mode": "auto"})
                values.update({"attempt_state": "SUCCEEDED"})
            if table == "bench_run":
                values.update({"state": "planned"})
            for name, value in over.items():
                values[name] = value
            keys = ", ".join(values)
            marks = ", ".join(
                f"CAST(:{k} AS jsonb)"
                if isinstance(v, str) and k.endswith("_set")
                else f":{k}"
                for k, v in values.items()
            )
            await conn.execute(
                text(f"INSERT INTO {table} ({keys}) VALUES ({marks})"), values
            )
    finally:
        await engine.dispose()


async def _q(settings: DatabaseSettings, sql: str, **params: Any) -> list[Any]:
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            return list(res.all()) if res.returns_rows else []
    finally:
        await engine.dispose()


async def _rejected(settings: DatabaseSettings, table: str, **over: Any) -> bool:
    try:
        await _insert_dummy(settings, table, **over)
    except DBAPIError as exc:
        # only a constraint may refuse - not a typo'd column or a bad dummy
        assert "violates" in str(exc), str(exc)[:300]
        return True
    return False


def _cols(settings: DatabaseSettings, table: str) -> set[str]:
    rows = _run(
        _q(
            settings,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = 'public'",
            t=table,
        )
    )
    return {r[0] for r in rows}


V3_RUN = {
    "attempt_role", "planner_strategy", "planner_node_set", "split_node_set",
    "split_items", "split_chunk_items", "split_chunks", "split_predicted_s",
    "split_makespan_ms", "split_job_ms", "split_combine_ms", "split_ideal_bound_s",
}  # fmt: skip


def test_0029_up_keeps_old_rows_enforces_constraints_and_down_up_round_trips(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0028")
    assert not (V3_RUN & _cols(pg_settings, "run_record"))
    assert "chunk_record" not in {
        r[0]
        for r in _run(
            _q(
                pg_settings,
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'",
            )
        )
    }
    # an old (v2) record written before 0029
    _run(
        _insert_dummy(
            pg_settings,
            "run_record",
            schema_version=2,
            strategy="mode1",
            attempt_id="old-1",
        )
    )

    command.upgrade(cfg, "0029")
    assert _cols(pg_settings, "run_record") >= V3_RUN
    assert "shadow_node_set" in _cols(pg_settings, "plan_decision")
    assert "node_set" in _cols(pg_settings, "bench_run")
    old = _run(
        _q(
            pg_settings,
            "SELECT schema_version, strategy, attempt_role, split_job_ms FROM run_record WHERE attempt_id='old-1'",
        )
    )
    assert old == [(2, "mode1", None, None)], (
        "an old record must keep its version and get nulls"
    )

    # --- constraints
    good = dict(
        job_id="j1", chunk_index=0, lease_seq=1, schema_version=3, outcome="committed",
        failure_class="ok", items=2, first_item=0, speculative=False, won=True,
    )  # fmt: skip
    _run(_insert_dummy(pg_settings, "chunk_record", **good))
    assert _run(_rejected(pg_settings, "chunk_record", **good)), "duplicate PK accepted"
    for bad in (
        {"outcome": "weird"},
        {"failure_class": "meh"},
        {"lease_seq": 0},
        {"chunk_index": -1},
        {"items": 0},
        {"first_item": -1},
        {"schema_version": 2},
        {"stderr_tail": "x" * 65_537},
    ):
        row = {**good, "lease_seq": 2, **bad}
        assert _run(_rejected(pg_settings, "chunk_record", **row)), f"accepted {bad}"
    # 64 KiB exactly fits (the cap is on characters)
    _run(
        _insert_dummy(
            pg_settings,
            "chunk_record",
            **{**good, "lease_seq": 3, "stderr_tail": "é" * 65_536},
        )
    )
    for outcome in ("lost", "failed", "superseded", "released", "expired"):
        _run(
            _insert_dummy(
                pg_settings,
                "chunk_record",
                **{**good, "lease_seq": 10 + len(outcome), "outcome": outcome},
            )
        )

    _run(
        _insert_dummy(
            pg_settings,
            "run_record",
            schema_version=3,
            strategy="mode3",
            attempt_id="new-1",
            attempt_role="combine",
            planner_strategy="mode3",
        )
    )
    assert _run(
        _rejected(
            pg_settings,
            "run_record",
            schema_version=3,
            strategy="mode3",
            attempt_id="new-2",
            attempt_role="bogus",
        )
    )
    assert _run(
        _rejected(
            pg_settings,
            "run_record",
            schema_version=3,
            strategy="mode9",
            attempt_id="new-3",
        )
    )
    assert _run(
        _rejected(
            pg_settings,
            "run_record",
            schema_version=3,
            strategy="mode1",
            attempt_id="new-4",
            planner_strategy="mode9",
        )
    )

    _run(
        _insert_dummy(
            pg_settings, "bench_run", strategy="mode3", node_set='["n1","n2"]'
        )
    )
    assert _run(
        _rejected(
            pg_settings, "bench_run", strategy="mode3", node_set=None, id="r2", seq=2
        )
    ), "mode3 without node_set"
    assert _run(
        _rejected(
            pg_settings,
            "bench_run",
            strategy="mode1",
            node_set='["n1"]',
            id="r3",
            seq=3,
        )
    ), "node_set on mode1"

    command.check(cfg)  # models == migrations

    # --- down: documented data loss only
    command.downgrade(cfg, "0028")
    assert not (V3_RUN & _cols(pg_settings, "run_record"))
    assert "node_set" not in _cols(pg_settings, "bench_run")
    surv = _run(
        _q(
            pg_settings,
            "SELECT attempt_id, strategy, schema_version FROM run_record ORDER BY attempt_id",
        )
    )
    assert surv == [("new-1", "mode1", 3), ("old-1", "mode1", 2)]
    assert _run(
        _q(pg_settings, "SELECT count(*) FROM bench_run WHERE strategy='mode3'")
    ) == [(0,)]
    assert _run(
        _rejected(
            pg_settings,
            "run_record",
            schema_version=2,
            strategy="mode3",
            attempt_id="z",
        )
    )

    # --- up again: clean, and the kept rows are usable
    command.upgrade(cfg, "head")
    command.check(cfg)
    assert _run(_q(pg_settings, "SELECT count(*) FROM chunk_record")) == [(0,)]
    command.downgrade(cfg, "base")
