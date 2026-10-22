"""V0.15 G3: migration 0034 (``ml_model_set`` + ``ml_model``) on a real
PostgreSQL.

Up from 0033 creates both tables, their checks and indexes; the checks refuse
what they must (an unknown status / algorithm, a rejected set without a
reason, a learned member without an artefact and a ``none`` member with one,
a non-object JSON, a version that is not a digest); the partial unique
indexes allow one ``active`` and one ``pinned`` set; deleting a set deletes
its members; down drops both, up again works, ``alembic check`` is clean.
"""

from __future__ import annotations

import asyncio
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

SET_CHECKS = {
    "ck_ml_model_set_status",
    "ck_ml_model_set_gate",
    "ck_ml_model_set_inputs",
    "ck_ml_model_set_reason",
    "ck_ml_model_set_rejected_has_reason",
    "ck_ml_model_set_set_version",
    "ck_ml_model_set_feature_version",
}
MODEL_CHECKS = {
    "ck_ml_model_algorithm",
    "ck_ml_model_params",
    "ck_ml_model_metrics",
    "ck_ml_model_reason",
    "ck_ml_model_artefact_iff_learned",
    "ck_ml_model_artefact_bytes",
    "ck_ml_model_artefact_bytes_positive",
    "ck_ml_model_version",
}


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _q(settings: DatabaseSettings, sql: str, **params: Any) -> list[Any]:
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            return list(res.all()) if res.returns_rows else []
    finally:
        await engine.dispose()


def _tables(settings: DatabaseSettings) -> set[str]:
    rows = _run(
        _q(
            settings,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = "
            "'public' AND table_name IN ('ml_model_set', 'ml_model')",
        )
    )
    return {str(r[0]) for r in rows}


def _checks(settings: DatabaseSettings, table: str) -> set[str]:
    rows = _run(
        _q(
            settings,
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'",
            t=table,
        )
    )
    return {str(r[0]) for r in rows}


def _set(settings: DatabaseSettings, version: str, **values: Any) -> None:
    row: dict[str, Any] = {
        "v": version,
        "status": "superseded",
        "reason": None,
        "gate": "{}",
        "inputs": "{}",
        "fv": 1,
    }
    row.update(values)
    _run(
        _q(
            settings,
            "INSERT INTO ml_model_set (set_version, status, reason, gate, "
            "dataset_digest, feature_version, inputs) VALUES (:v, :status, :reason, "
            "CAST(:gate AS jsonb), 'sha256:d', :fv, CAST(:inputs AS jsonb))",
            **row,
        )
    )


def _member(settings: DatabaseSettings, set_version: str, **values: Any) -> None:
    row: dict[str, Any] = {
        "s": set_version,
        "term": "compute",
        "version": "sha256:m",
        "algorithm": "ridge",
        "params": "{}",
        "metrics": "{}",
        "artefact": "sha256:a",
        "size": 10,
    }
    row.update(values)
    _run(
        _q(
            settings,
            "INSERT INTO ml_model (set_version, term, version, algorithm, "
            "feature_version, dataset_digest, params, metrics, artefact_digest, "
            "artefact_bytes) VALUES (:s, :term, :version, :algorithm, 1, 'sha256:d', "
            "CAST(:params AS jsonb), CAST(:metrics AS jsonb), :artefact, :size)",
            **row,
        )
    )


def _refused(fn: Any, *args: Any, **values: Any) -> None:
    with pytest.raises(DBAPIError) as caught:
        fn(*args, **values)
    message = str(caught.value)
    assert "violates" in message or "duplicate key" in message, message[:300]


def test_0034_up_checks_indexes_down_up_and_alembic_check(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0033")
    assert _tables(pg_settings) == set()
    command.upgrade(cfg, "0034")
    assert _tables(pg_settings) == {"ml_model_set", "ml_model"}
    assert _checks(pg_settings, "ml_model_set") >= SET_CHECKS
    assert _checks(pg_settings, "ml_model") >= MODEL_CHECKS

    _set(pg_settings, "sha256:a1", status="active")
    _set(pg_settings, "sha256:p1", status="pinned")
    _set(pg_settings, "sha256:r1", status="rejected", reason="not_better: x")
    _set(pg_settings, "sha256:s1")
    _set(pg_settings, "sha256:s2")  # many superseded
    # one active, one pinned
    _refused(_set, pg_settings, "sha256:a2", status="active")
    _refused(_set, pg_settings, "sha256:p2", status="pinned")
    _refused(_set, pg_settings, "sha256:x1", status="retired")
    _refused(_set, pg_settings, "sha256:x2", status="rejected")  # no reason
    _refused(_set, pg_settings, "sha256:x3", reason="r" * 513)
    _refused(_set, pg_settings, "sha256:x4", gate="[]")
    _refused(_set, pg_settings, "sha256:x5", inputs='"x"')
    _refused(_set, pg_settings, "md5:x6")
    _refused(_set, pg_settings, "sha256:x7", fv=0)

    _member(pg_settings, "sha256:a1")
    _member(
        pg_settings,
        "sha256:a1",
        term="env",
        algorithm="none",
        artefact=None,
        size=None,
    )
    _refused(_member, pg_settings, "sha256:a1")  # (set, term) is the key
    _refused(_member, pg_settings, "sha256:s1", algorithm="svm")
    _refused(_member, pg_settings, "sha256:s1", artefact=None, size=None)
    _refused(_member, pg_settings, "sha256:s1", algorithm="none")
    _refused(_member, pg_settings, "sha256:s1", size=None)
    _refused(_member, pg_settings, "sha256:s1", size=0)
    _refused(_member, pg_settings, "sha256:s1", params="[]")
    _refused(_member, pg_settings, "sha256:s1", metrics="1")
    _refused(_member, pg_settings, "sha256:s1", version="v1")
    _refused(_member, pg_settings, "sha256:nope")  # foreign key
    _run(_q(pg_settings, "DELETE FROM ml_model_set WHERE set_version = 'sha256:a1'"))
    assert _run(_q(pg_settings, "SELECT count(*) FROM ml_model"))[0][0] == 0

    command.check(cfg)
    command.downgrade(cfg, "0033")
    assert _tables(pg_settings) == set()
    command.upgrade(cfg, "head")
    assert _tables(pg_settings) == {"ml_model_set", "ml_model"}
    command.check(cfg)
    command.downgrade(cfg, "base")
