"""V0.14 G1 tester r1: migration 0030 on a real PostgreSQL, from the
criteria (replay + replay_version together, replay an object, version >= 1,
objective one of D295's): the constraints exist by name, JSON null / array /
scalar replays are refused, the three objectives are accepted, NULL rows stay
valid, down removes columns AND constraints, up again is clean (twice), and
the models match (alembic check)."""

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

CHECKS = {"ck_plan_decision_replay", "ck_plan_decision_objective"}


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


def _checks(pg: DatabaseSettings) -> set[str]:
    rows = _run(
        _q(
            pg,
            "SELECT conname FROM pg_constraint WHERE conrelid = "
            "'plan_decision'::regclass AND contype = 'c'",
        )
    )
    return {r[0] for r in rows}


def _insert(pg: DatabaseSettings, job: str, **new: Any) -> None:
    cols = (
        "job_id, attempt_no, outcome, policy, snapshot_digest, reason_key, candidates"
    )
    vals = ":j, 1, 'waiting', 'cost_model', 'd', 'r', '[]'"
    for k in new:
        cols += f", {k}"
        vals += f", CAST(:{k} AS jsonb)" if k == "replay" else f", :{k}"
    _run(_q(pg, f"INSERT INTO plan_decision ({cols}) VALUES ({vals})", j=job, **new))  # noqa: S608


def _ok(pg: DatabaseSettings, job: str, **new: Any) -> None:
    _insert(pg, job, **new)


def _refused(pg: DatabaseSettings, job: str, **new: Any) -> None:
    with pytest.raises(DBAPIError):
        _insert(pg, job, **new)


def test_0030_constraints_by_name_and_value(pg_settings: DatabaseSettings) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0029")
    assert not (CHECKS & _checks(pg_settings))
    command.upgrade(cfg, "0030")
    assert _checks(pg_settings) >= CHECKS

    good = json.dumps({"replay_version": 1})
    _ok(pg_settings, "ok_null")  # all four NULL
    _ok(pg_settings, "ok_replay", replay=good, replay_version=1, objective="min_time")
    _ok(pg_settings, "ok_nullobj", replay=good, replay_version=7, objective=None)
    for i, objective in enumerate(("min_time", "min_network", "prefer_local")):
        _ok(pg_settings, f"ok_obj{i}", objective=objective)
    _ok(pg_settings, "ok_calib", calibration_version=2**62)

    _refused(pg_settings, "bad_jsonnull", replay="null", replay_version=1)
    _refused(pg_settings, "bad_array", replay="[]", replay_version=1)
    _refused(pg_settings, "bad_num", replay="5", replay_version=1)
    _refused(pg_settings, "bad_str", replay='"x"', replay_version=1)
    _refused(pg_settings, "bad_bool", replay="true", replay_version=1)
    _refused(pg_settings, "bad_nover", replay=good)
    _refused(pg_settings, "bad_noreplay", replay_version=1)
    _refused(pg_settings, "bad_v0", replay=good, replay_version=0)
    _refused(pg_settings, "bad_vneg", replay=good, replay_version=-1)
    _refused(pg_settings, "bad_obj", objective="MIN_TIME")
    _refused(pg_settings, "bad_obj2", objective=" min_time")
    _refused(pg_settings, "bad_obj3", objective="min_time; DROP")
    _refused(pg_settings, "bad_nul", replay='{"a":"\\u0000"}', replay_version=1)

    n = _run(_q(pg_settings, "SELECT count(*) FROM plan_decision"))[0][0]
    assert n == 7  # only the accepted rows exist


def test_0030_down_removes_constraints_and_up_is_repeatable(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "head")
    _insert(
        pg_settings, "keep", replay='{"x":1}', replay_version=1, objective="min_time"
    )
    for _ in range(2):
        command.downgrade(cfg, "0029")
        assert not (CHECKS & _checks(pg_settings))
        cols = _run(
            _q(
                pg_settings,
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='plan_decision' AND column_name IN "
                "('replay','replay_version','objective','calibration_version')",
            )
        )
        assert cols == []
        assert _run(_q(pg_settings, "SELECT count(*) FROM plan_decision"))[0][0] == 1
        command.upgrade(cfg, "head")
        assert _checks(pg_settings) >= CHECKS
        # a pre-0030 row comes back with NULLs (the data in the dropped columns is gone)
        (row,) = _run(
            _q(
                pg_settings,
                "SELECT replay, replay_version, objective, calibration_version "
                "FROM plan_decision WHERE job_id='keep'",
            )
        )
        assert tuple(row) == (None, None, None, None)
        command.check(cfg)
