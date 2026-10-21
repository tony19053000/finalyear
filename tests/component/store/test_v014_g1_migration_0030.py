"""V0.14 G1: migration 0030 (plan_decision replay) on a real PostgreSQL.

Up from 0029 with an existing decision row (it keeps NULL in the four new
columns), the new checks enforced (a replay and its version come together;
a replay is an object; the version is >= 1; the objective is one of D295's),
a large replay stored and read back unchanged, down drops the columns, up
again, ``alembic check`` clean (the models match).
"""

from __future__ import annotations

import asyncio
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

NEW = {"replay", "replay_version", "objective", "calibration_version"}


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _q(settings: DatabaseSettings, sql: str, **params: Any) -> list[Any]:
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET session_replication_role = replica"))
            res = await conn.execute(text(sql), params)
            return list(res.all()) if res.returns_rows else []
    finally:
        await engine.dispose()


def _insert(settings: DatabaseSettings, job: str, **new: Any) -> None:
    cols = ["job_id", "attempt_no", "outcome", "policy", "snapshot_digest"]
    cols += ["reason_key", "candidates", *new]
    marks = [":job_id", "1", "'waiting'", "'cost_model'", "'d'", "'r'", "'[]'"]
    marks += [f"CAST(:{k} AS jsonb)" if k == "replay" else f":{k}" for k in new]
    _run(
        _q(
            settings,
            f"INSERT INTO plan_decision ({', '.join(cols)}) "  # noqa: S608 - test-only literals
            f"VALUES ({', '.join(marks)})",
            job_id=job,
            **new,
        )
    )


def _refused(settings: DatabaseSettings, **new: Any) -> bool:
    try:
        _insert(settings, "job_bad", **new)
    except DBAPIError as exc:
        assert "violates check constraint" in str(exc), str(exc)[:300]  # noqa: PT017
        return True
    return False


def _cols(settings: DatabaseSettings) -> set[str]:
    rows = _run(
        _q(
            settings,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'plan_decision' AND table_schema = 'public'",
        )
    )
    return {r[0] for r in rows}


def test_0030_up_keeps_old_rows_enforces_checks_and_down_up_round_trips(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0029")
    assert not (NEW & _cols(pg_settings))
    _insert(pg_settings, "job_old")

    command.upgrade(cfg, "0030")
    assert _cols(pg_settings) >= NEW
    old = _run(
        _q(
            pg_settings,
            "SELECT replay, replay_version, objective, calibration_version "
            "FROM plan_decision WHERE job_id = 'job_old'",
        )
    )
    assert old == [(None, None, None, None)]

    replay = {"replay_version": 1, "now": None, "request": {}, "snapshot": {}}
    big = {**replay, "pad": "x" * 60_000, "f": {"$f": "inf"}, "n": 1e300}
    _insert(
        pg_settings,
        "job_new",
        replay=json.dumps(big),
        replay_version=1,
        objective="min_time",
        calibration_version=7,
    )
    (back,) = _run(
        _q(
            pg_settings,
            "SELECT replay, replay_version, objective, calibration_version "
            "FROM plan_decision WHERE job_id = 'job_new'",
        )
    )
    assert back.replay_version == 1
    # why replay v1 tags large floats ({"$f": ...}): jsonb keeps numbers as
    # numeric and hands an untagged 1e300 back as an *integer*
    assert isinstance(back.replay.pop("n"), int)
    assert back.replay == {k: v for k, v in big.items() if k != "n"}
    assert back.objective == "min_time" and back.calibration_version == 7
    for objective in ("min_network", "prefer_local"):
        _insert(pg_settings, f"job_{objective}", objective=objective)

    good = {"replay": json.dumps(replay), "replay_version": 1}
    assert _refused(pg_settings, replay=json.dumps(replay))  # no version
    assert _refused(pg_settings, replay_version=1)  # a version, no replay
    assert _refused(pg_settings, replay=json.dumps([1, 2]), replay_version=1)
    assert _refused(pg_settings, replay=json.dumps("text"), replay_version=1)
    assert _refused(pg_settings, **{**good, "replay_version": 0})
    assert _refused(pg_settings, objective="fastest")
    assert _refused(pg_settings, objective="")

    command.downgrade(cfg, "0029")
    assert not (NEW & _cols(pg_settings))
    rows = _run(_q(pg_settings, "SELECT count(*) FROM plan_decision"))
    assert rows[0][0] == 4  # rows stay; only the columns go
    command.upgrade(cfg, "head")
    command.check(cfg)
