"""V0.9 G3 lane B item 2: migration 0022 (bench_campaign, bench_run, shadow plan) up/down."""

# ruff: noqa: E501
from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component


def _sql(
    settings: DatabaseSettings, statements: list[tuple[str, dict[str, Any]]]
) -> list[Any]:
    async def go() -> list[Any]:
        engine = create_engine(settings)
        out: list[Any] = []
        try:
            async with engine.begin() as conn:
                for stmt, params in statements:
                    result = await conn.execute(text(stmt), params)
                    out.append(result.fetchall() if result.returns_rows else None)
        finally:
            await engine.dispose()
        return out

    return asyncio.run(go())


def _cols(settings: DatabaseSettings, table: str) -> set[str]:
    rows = _sql(
        settings,
        [
            (
                "SELECT column_name FROM information_schema.columns WHERE table_name=:t",
                {"t": table},
            )
        ],
    )[0]
    return {r[0] for r in rows}


CAMPAIGN = (
    "INSERT INTO bench_campaign (id, name, spec_yaml, spec_digest, seed, state, run_count, summary) "
    "VALUES ('camp_1', 'c', 'name: c', 'sha256:x', 1, 'active', 2, '{}'::jsonb)"
)


def _run(
    seq: int, state: str, job: str | None, *, strategy: str = "local"
) -> tuple[str, dict[str, Any]]:
    return (
        "INSERT INTO bench_run (id, campaign_id, seq, group_id, config_id, repetition, family, params, seed, "
        "conditions, strategy, node_id, origin_node_id, state, job_id) VALUES "
        "(:id, 'camp_1', :seq, 'grp', 'cfg', 1, 'cpu.sort', '{}'::jsonb, 3, '[\"none\"]'::jsonb, :strategy, "
        "'node_a', 'node_a', :state, :job)",
        {
            "id": f"brun_{seq}",
            "seq": seq,
            "state": state,
            "job": job,
            "strategy": strategy,
        },
    )


def test_0022_constraints_cascade_and_downgrade(pg_settings: DatabaseSettings) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "head")
    command.check(cfg)
    assert {"shadow_chosen_node_id", "shadow_estimate_s"} <= _cols(
        pg_settings, "plan_decision"
    )
    _sql(
        pg_settings,
        [(CAMPAIGN, {}), _run(1, "planned", None), _run(2, "submitted", "job_1")],
    )
    for bad in (
        _run(3, "planned", "job_3"),  # a planned run has no job
        _run(4, "submitted", None),  # a submitted run has one
        _run(5, "running", None),  # unknown state
        _run(6, "planned", None, strategy="mode2"),
        _run(1, "planned", None),  # (campaign, seq) unique - and id
        _run(7, "submitted", "job_1"),  # one job, one run
    ):
        with pytest.raises(IntegrityError):
            _sql(pg_settings, [bad])
    _sql(pg_settings, [_run(8, "skipped", None)])  # skipped: with or without a job
    # campaign delete cascades to its runs
    _sql(pg_settings, [("DELETE FROM bench_campaign WHERE id = 'camp_1'", {})])
    assert _sql(pg_settings, [("SELECT count(*) FROM bench_run", {})])[0][0][0] == 0

    _sql(pg_settings, [(CAMPAIGN, {}), _run(1, "planned", None)])
    command.downgrade(cfg, "0021")
    assert not _cols(pg_settings, "bench_run")
    assert not _cols(pg_settings, "bench_campaign")
    assert not {"shadow_chosen_node_id", "shadow_estimate_s"} & _cols(
        pg_settings, "plan_decision"
    )
    assert _cols(pg_settings, "run_record")  # 0021 untouched

    command.upgrade(cfg, "head")
    command.check(cfg)
    command.downgrade(cfg, "base")
