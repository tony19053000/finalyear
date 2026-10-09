"""V0.3 G6 (D19): ``SessionEndReason.STREAM_LOST`` and migration 0004.

Migration 0004 writes its CHECK value lists out literally (so the revision keeps
meaning the same thing when a later one adds a value). These tests pin that the
literal list equals the model's enum *today*: adding a ``SessionEndReason`` member
without a new migration fails here, not in production on the first INSERT.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("alembic", reason="needs the controller extra")

from meshforge.controller.store.models.node import SessionEndReason

pytestmark = pytest.mark.unit

_VERSIONS = (
    Path(__file__).resolve().parents[3]
    / "src/meshforge/controller/store/migrations/versions"
)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"_mf_mig_{name}", _VERSIONS / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stream_lost_value_is_permanent() -> None:
    assert SessionEndReason.STREAM_LOST.value == "stream_lost"
    # Values are persisted: never renamed, only added (order = history).
    assert [r.value for r in SessionEndReason] == [
        "superseded",
        "agent_closed",
        "lease_expired",
        "controller_shutdown",
        "controller_restart",
        "protocol_error",
        "internal_error",
        "stream_lost",
        "revoked",  # V0.7 G3 (0016)
    ]


def test_migration_0004_upgrade_list_is_the_model_enum() -> None:
    mig = _load("0004_stream_lost.py")
    assert mig.revision == "0004"
    assert mig.down_revision == "0003"
    today = {r.value for r in SessionEndReason}
    assert set(mig.END_REASONS_0004) == today - {"revoked"}
    assert set(mig.END_REASONS_0003) == today - {"stream_lost", "revoked"}


def test_migration_0016_lists_are_the_model_enums() -> None:
    """The newest revision's literal lists equal the enums today (V0.7 G3)."""
    from meshforge.controller.store.models.enrollment import AuditAction
    from meshforge.controller.store.models.revocation import RevocationReason

    mig = _load("0016_revocation.py")
    assert (mig.revision, mig.down_revision) == ("0016", "0015")
    assert set(mig.END_REASONS_0016) == {r.value for r in SessionEndReason}
    assert set(mig.END_REASONS_0015) == set(mig.END_REASONS_0016) - {"revoked"}
    assert set(mig.ACTIONS_0016) == {a.value for a in AuditAction}
    assert set(mig.REASONS_0016) == {r.value for r in RevocationReason}


def test_migration_0004_check_text_quotes_every_value() -> None:
    mig = _load("0004_stream_lost.py")
    text = mig._check(mig.END_REASONS_0004)
    assert text.startswith("end_reason IS NULL OR end_reason IN (")
    for reason in SessionEndReason:
        if reason is SessionEndReason.REVOKED:  # added in 0016
            assert f"'{reason.value}'" not in text
            continue
        assert f"'{reason.value}'" in text
