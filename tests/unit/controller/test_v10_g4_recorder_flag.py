"""V1.0 G4 F-4 (D412): the recorder's work-list query and the ``recorded`` flag,
without a database.

The query must be one PostgreSQL can serve from ``ix_attempt_unrecorded``: the
same predicate as the index (``NOT recorded`` and the terminal states rendered
as literals, so even a generic plan proves the implication), oldest first, and
row-locked with ``FOR NO KEY UPDATE SKIP LOCKED`` (disjoint batches for two
sweepers; no conflict with foreign-key inserts). The migration's frozen
predicate, the model's index and the shared constant must agree.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy.dialects import postgresql

from meshforge.common.states import ATTEMPT_TERMINAL_STATES
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.store.migrate import MIGRATIONS_DIR
from meshforge.controller.store.models.job import UNRECORDED_ATTEMPT_SQL, Attempt
from meshforge.controller.telemetry.recorder import work_query

pytestmark = pytest.mark.unit


def _sql(*args: object) -> str:
    query = work_query(*args)  # type: ignore[arg-type]
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    return str(
        query.compile(dialect=dialect, compile_kwargs={"render_postcompile": True})
    )


def _migration() -> ModuleType:
    path = Path(MIGRATIONS_DIR) / "versions" / "0037_attempt_recorded.py"
    spec = importlib.util.spec_from_file_location("mf_migration_0037", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_query_has_the_index_predicate_order_and_lock() -> None:
    sql = _sql((), None, 200)
    assert "NOT attempt.recorded" in sql
    for state in ATTEMPT_TERMINAL_STATES:
        assert f"'{state.value}'" in sql  # literals, not parameters
    assert "ORDER BY attempt.created_at, attempt.id" in sql
    assert sql.rstrip().endswith("FOR NO KEY UPDATE SKIP LOCKED")
    assert "run_record" not in sql  # no anti-join any more


def test_the_skip_list_and_watermark_are_still_applied() -> None:
    sql = _sql(("att_a", "att_b"), (dt.datetime(2026, 1, 1, tzinfo=dt.UTC), "x"), 5)
    assert "attempt.id NOT IN" in sql
    assert "(attempt.created_at, attempt.id) >" in sql
    assert "LIMIT" in sql


def test_predicate_constant_model_index_and_migration_agree() -> None:
    assert (
        UNRECORDED_ATTEMPT_SQL
        == "NOT recorded AND state IN ('done', 'failed', 'fenced', 'lost')"
    )
    assert _migration()._UNRECORDED == UNRECORDED_ATTEMPT_SQL
    (index,) = (
        i for i in Attempt.__table__.indexes if i.name == "ix_attempt_unrecorded"
    )
    assert [c.name for c in index.columns] == ["created_at", "id"]
    assert str(index.dialect_options["postgresql"]["where"]) == UNRECORDED_ATTEMPT_SQL


def test_the_flag_column_is_not_null_default_false() -> None:
    column = Attempt.__table__.c.recorded
    assert column.nullable is False
    assert column.server_default is not None
    assert str(column.server_default.arg) == "false"  # type: ignore[attr-defined]


def test_the_calibration_read_counter_is_read_only() -> None:
    """G4 reviewer note: the F-2 counter is a read-only metric, not a public
    mutable attribute."""
    prop = CalibrationStore.__dict__["outcome_reads_total"]
    assert isinstance(prop, property) and prop.fset is None
    assert "full_reads" not in CalibrationStore.__dict__
