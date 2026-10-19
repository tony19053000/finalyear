"""V0.9 G4 lane A tester r1 on real PostgreSQL: the v2 columns end to end
(recorder -> run_record -> fetch_records -> streamed Parquet), v1 rows written
before migration 0023 exported next to v2 rows, hostile condition values, the
recorder's injectable clock, and bench-store sync edge cases."""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import datetime as dt
import io
import json
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from alembic import command
from sqlalchemy import text
from test_v09_g3a_recorder_cap import (  # noqa: F401  (fixtures are imported)
    T0,
    _attempts,
    _recorded,
    engine,
    node_id,
)

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.telemetry import export
from meshforge.controller.telemetry.recorder import RunRecorder
from meshforge.controller.telemetry.schema import COLUMN_NAMES, SCHEMA_VERSION

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(180)]

LONG = "c" * 129


def _lookup(table: dict[str, dict[str, Any]]) -> Any:
    async def campaign_of(db: Any, job_ids: Any) -> dict[str, dict[str, Any]]:
        return {j: table[j] for j in job_ids if j in table}

    return campaign_of


async def _rows(engine: Any) -> dict[str, dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT attempt_id, schema_version, condition, condition_measured, "
                "result_schema_version, group_id FROM run_record"
            )
        )
        return {r[0]: dict(r._mapping) for r in result}


async def test_recorder_stores_condition_and_measured_for_good_values(
    engine: Any, node_id: str
) -> None:
    made = await _attempts(engine, node_id, 2, start_s=0)
    table = {
        made[0][0]: {
            "campaign_id": "camp_1",
            "group_id": "g",
            "config_id": "c",
            "repetition": 1,
            "condition": "queue:2",
            "session_label": "lan",
            "condition_facts": {"fillers_ahead": 2, "rtt_ms": 52.5},
        },
        made[1][0]: {
            "campaign_id": "camp_1",
            "group_id": "g",
            "config_id": "c2",
            "repetition": 1,
            "condition": "none",
        },
    }
    recorder = RunRecorder(engine, campaign_of=_lookup(table))
    await recorder.sweep()
    rows = await _rows(engine)
    first, second = rows[made[0][1]], rows[made[1][1]]
    assert first["schema_version"] == SCHEMA_VERSION == 2
    # after the G4 A x B merge condition_outcome is the columns' one writer
    assert first["condition"] == "queue:2"
    assert first["condition_measured"] == {
        "verified": True,
        "reason": None,
        "controller": {"fillers_ahead": 2, "rtt_ms": 52.5},
        "session_label": "lan",
    }
    assert second["condition"] == "none"
    assert second["condition_measured"] == {
        "verified": True,
        "reason": None,
        "session_label": None,
    }


async def test_hostile_condition_values_never_lose_or_block_the_record(
    engine: Any, node_id: str
) -> None:
    made = await _attempts(engine, node_id, 5, start_s=0)
    base = {"campaign_id": "camp_1", "group_id": "g", "config_id": "c", "repetition": 1}
    table = {
        made[0][0]: {**base, "condition": LONG},
        made[1][0]: {
            **base,
            "condition": "none",
            "condition_facts": {"k": "v\x00"},
        },
        made[2][0]: {
            **base,
            "condition": "queue:1",
            "condition_facts": {
                "fillers_ahead": 1,
                "x": float("nan"),
                "y": [float("inf")],
            },
        },
        made[3][0]: {**base, "condition": 7, "condition_facts": "not an object"},
        made[4][0]: {**base, "condition": "ok:1"},
    }
    recorder = RunRecorder(engine, campaign_of=_lookup(table))
    await recorder.sweep()
    assert await _recorded(engine) == {a for _, a in made}  # all five written
    rows = await _rows(engine)
    assert rows[made[0][1]]["condition"] is None
    assert rows[made[1][1]]["condition"] == "none"
    assert rows[made[1][1]]["condition_measured"] is None  # NUL: whole value null
    assert rows[made[2][1]]["condition_measured"]["controller"] == {
        "fillers_ahead": 1,
        "x": None,
        "y": [None],
    }
    assert rows[made[3][1]]["condition"] is None
    assert rows[made[3][1]]["condition_measured"] is None
    # not a canonical name[:arg] condition: never stored (lane A reviewer)
    assert rows[made[4][1]]["condition"] is None
    assert rows[made[4][1]]["condition_measured"]["verified"] is False
    assert recorder.skipped == 0


async def test_v1_row_from_before_0023_is_exported_next_to_a_v2_row(
    pg_settings: DatabaseSettings,
) -> None:
    """Upgrade to 0022, write a v1 record, upgrade to head, write a v2 record
    with the recorder, export through the real fetch + streaming path."""
    cfg = alembic_config(pg_settings)
    await asyncio.to_thread(command.upgrade, cfg, "0022")
    eng = create_engine(pg_settings, pool_size=3)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO run_record (schema_version, attempt_id, job_id, attempt_no, "
                    "node_id, strategy, placement_mode, attempt_state, failure_class, "
                    "attempt_created_at, recorded_at) VALUES (1, 'att_old', 'job_old', 1, "
                    "'node_a', 'local', 'auto', 'done', 'ok', :t, :t)"
                ),
                {"t": T0},
            )
        await asyncio.to_thread(command.upgrade, cfg, "head")
        await asyncio.to_thread(command.upgrade, cfg, "head")  # idempotent
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO run_record (schema_version, attempt_id, job_id, attempt_no, "
                    "node_id, strategy, placement_mode, attempt_state, failure_class, "
                    "attempt_created_at, recorded_at, result_schema_version, condition, "
                    "condition_measured) VALUES (2, 'att_new', 'job_new', 1, 'node_a', "
                    "'mode1', 'pinned', 'done', 'ok', :t, :t, 1, 'queue:4', "
                    "CAST('{\"depth\": 4}' AS jsonb))"
                ),
                {"t": T0 + dt.timedelta(seconds=5)},
            )
        records = await export.fetch_records(eng)
        assert {r["attempt_id"] for r in records} == {"att_old", "att_new"}
        body = b"".join(export.iter_parquet(records, sort_in_place=True))
        table = pq.read_table(io.BytesIO(body))
        assert table.column_names == list(COLUMN_NAMES)
        by_id = {r["attempt_id"]: r for r in table.to_pylist()}
        old, new = by_id["att_old"], by_id["att_new"]
        assert (old["schema_version"], new["schema_version"]) == (1, 2)
        assert old["condition"] is None and new["condition"] == "queue:4"
        assert old["condition_measured"] is None
        assert json.loads(new["condition_measured"]) == {"depth": 4}
        assert (old["result_schema_version"], new["result_schema_version"]) == (None, 1)
        # the pure export of the same fetched records is byte-identical (D222)
        assert body == export.parquet_bytes(await export.fetch_records(eng))
    finally:
        await eng.dispose()
        await asyncio.to_thread(command.downgrade, cfg, "base")


async def test_alembic_has_one_head_0023(pg_settings: DatabaseSettings) -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config(pg_settings))
    assert script.get_heads() == ["0026"]  # V0.10 G1: migration 0026


async def test_recorder_run_resweeps_with_an_injected_clock_without_real_waits(
    engine: Any, node_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() with a stub clock: attempts recorded across several batches with
    interval 3600 s - must finish well inside the timeout (no real 60 s wait,
    no interval sleep while a full batch is examined)."""
    made = await _attempts(engine, node_id, 7, start_s=0)
    ticks = {"n": 0}

    def clock() -> float:
        ticks["n"] += 1
        return 1000.0 + ticks["n"]

    recorder = RunRecorder(engine, interval_s=3600.0, batch=2, clock=clock)
    task = asyncio.create_task(recorder.run())
    try:
        async with asyncio.timeout(30):
            while await _recorded(engine) != {a for _, a in made}:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --------------------------------------------------------- bench store (0.6/0.7)


async def test_orphan_job_already_terminal_reaches_done_in_one_sync_and_concurrent_syncs_agree(
    engine: Any,
) -> None:
    from test_v09_g4a_bench_store_sync import _campaign, _job, _run

    from meshforge.controller.bench.store import BenchStore
    from meshforge.controller.jobs.store import JobStore

    store, jobs = BenchStore(engine), JobStore(engine)
    campaign_id = await _campaign(store)
    run = (await store.runs(campaign_id, limit=1))[0]
    job_id = await _job(jobs, run)
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE job SET state = 'succeeded' WHERE id = :j"), {"j": job_id}
        )
    results = await asyncio.gather(*(store.sync(campaign_id) for _ in range(4)))
    assert sum(results) == 1  # moved to done exactly once across racing syncs
    done = await _run(store, campaign_id, run.id)
    assert done.state == "done" and done.job_id == job_id and done.ended_at is not None


async def test_sync_without_campaign_id_links_across_campaigns(engine: Any) -> None:
    from test_v09_g4a_bench_store_sync import _campaign, _job, _run

    from meshforge.controller.bench.store import BenchStore
    from meshforge.controller.jobs.store import JobStore

    store, jobs = BenchStore(engine), JobStore(engine)
    c1, c2 = await _campaign(store), await _campaign(store)
    r1 = (await store.runs(c1, limit=1))[0]
    r2 = (await store.runs(c2, limit=1))[0]
    j1, j2 = await _job(jobs, r1), await _job(jobs, r2)
    await store.sync(c1)  # scoped: must not touch c2
    assert (await _run(store, c2, r2.id)).state == "planned"
    await store.sync()
    assert (await _run(store, c1, r1.id)).job_id == j1
    assert (await _run(store, c2, r2.id)).job_id == j2
