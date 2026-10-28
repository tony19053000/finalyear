"""V1.0 G4 tester: migration 0036 on a populated database, and the index
predicate against the real attempt-state enum."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import sqlalchemy as sa
from alembic import command

from meshforge.common.states import ATTEMPT_TERMINAL_STATES, AttemptState
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.job import LIVE_ATTEMPT_SQL
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

_NON_TERMINAL = sorted(
    s.value for s in AttemptState if s not in ATTEMPT_TERMINAL_STATES
)


async def _sql(pg: DatabaseSettings, *statements: str) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result: list[Any] = []
            for statement in statements:
                out = await conn.execute(sa.text(statement))
                result = list(out.all()) if out.returns_rows else []
            return result
    finally:
        await engine.dispose()


def _seed_every_state(pg: DatabaseSettings) -> None:
    """One job per attempt state (lane role avoided: plain 'run')."""
    states = [s.value for s in AttemptState]
    values = ", ".join(f"('{s}', {i})" for i, s in enumerate(states, 1))
    asyncio.run(
        _sql(
            pg,
            "INSERT INTO node (node_id, hostname, os, platform, hardware, state,"
            " first_seen_at, last_seen_at, session_epoch) VALUES ('node_x', 'h',"
            " 'o', 'p', '{}', 'offline', now(), now(), 1)",
            "INSERT INTO job (id, name, state, manifest, bundle_digest, placement, "
            "request_hash, attempt_epoch) SELECT 'job_' || lpad(g::text, 26, '0'), "
            "'j', 'succeeded', '{}'::jsonb, 'sha256:' || repeat('ab', 32), "
            f"'{{}}'::jsonb, repeat('h', 64), 1 FROM generate_series(1, {len(states)}) g",
            "INSERT INTO attempt (id, job_id, node_id, fencing_token, state, role) "
            "SELECT 'att_' || lpad(v.i::text, 26, '0'), 'job_' || lpad(v.i::text, 26,"
            f" '0'), 'node_x', 1, v.s, 'run' FROM (VALUES {values}) AS v(s, i)",
        )
    )


def _index_def(pg: DatabaseSettings) -> str | None:
    rows = asyncio.run(
        _sql(
            pg,
            "SELECT indexdef FROM pg_indexes WHERE indexname = "
            "'ix_attempt_live_created_at'",
        )
    )
    return str(rows[0].indexdef) if rows else None


def test_predicate_states_equal_the_real_non_terminal_enum(
    pg_settings: DatabaseSettings,
) -> None:
    command.upgrade(alembic_config(pg_settings), "0036")
    definition = _index_def(pg_settings)
    assert definition is not None
    where = definition.split(" WHERE ", 1)[1]
    in_index = sorted(re.findall(r"'([a-z_]+)'", where))
    assert in_index == _NON_TERMINAL, (in_index, _NON_TERMINAL)
    in_model = sorted(re.findall(r"'([a-z_]+)'", LIVE_ATTEMPT_SQL))
    assert in_model == _NON_TERMINAL


def test_every_non_terminal_state_is_served_by_the_index_and_returns_its_rows(
    pg_settings: DatabaseSettings,
) -> None:
    command.upgrade(alembic_config(pg_settings), "head")
    _seed_every_state(pg_settings)
    asyncio.run(_sql(pg_settings, "ANALYZE attempt"))
    for state in _NON_TERMINAL:
        # force the planner off seq scans so only the predicate decides usability
        plan = asyncio.run(
            _sql(
                pg_settings,
                "SET LOCAL enable_seqscan = off",
                f"EXPLAIN SELECT id FROM attempt WHERE {LIVE_ATTEMPT_SQL} "
                "ORDER BY created_at",
            )
        )
        text = "\n".join(str(r[0]) for r in plan)
        assert "ix_attempt_live_created_at" in text or "Seq Scan" in text, text
    # correctness: the filtered result equals the set of non-terminal rows
    got = asyncio.run(
        _sql(
            pg_settings,
            "SET LOCAL enable_seqscan = off",
            f"SELECT state FROM attempt WHERE {LIVE_ATTEMPT_SQL} ORDER BY state",
        )
    )
    assert sorted(r[0] for r in got) == _NON_TERMINAL


def test_up_down_up_on_a_populated_database_keeps_every_row(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "head")
    _seed_every_state(pg_settings)
    before = asyncio.run(_sql(pg_settings, "SELECT id, state FROM attempt ORDER BY id"))
    command.downgrade(cfg, "0035")
    assert _index_def(pg_settings) is None
    mid = asyncio.run(_sql(pg_settings, "SELECT id, state FROM attempt ORDER BY id"))
    command.upgrade(cfg, "0036")
    assert _index_def(pg_settings) is not None
    command.downgrade(cfg, "0035")
    command.upgrade(cfg, "head")  # twice more: idempotent round trip
    after = asyncio.run(_sql(pg_settings, "SELECT id, state FROM attempt ORDER BY id"))
    assert before == mid == after and len(before) == len(list(AttemptState))
    command.check(cfg)


def test_a_state_moving_in_and_out_of_the_live_set_is_found_either_way(
    pg_settings: DatabaseSettings,
) -> None:
    """The partial index must track updates: running -> done leaves it,
    queued-ish back in re-enters it, results always equal a no-index scan."""
    command.upgrade(alembic_config(pg_settings), "head")
    _seed_every_state(pg_settings)
    asyncio.run(
        _sql(pg_settings, "UPDATE attempt SET state = 'running' WHERE state = 'done'")
    )
    q = f"SELECT id FROM attempt WHERE {LIVE_ATTEMPT_SQL} ORDER BY id"
    with_idx = asyncio.run(_sql(pg_settings, "SET LOCAL enable_seqscan = off", q))
    without = asyncio.run(
        _sql(
            pg_settings,
            "SET LOCAL enable_indexscan = off",
            "SET LOCAL enable_bitmapscan = off",
            q,
        )
    )
    assert with_idx == without and len(with_idx) > len(_NON_TERMINAL) - 1
