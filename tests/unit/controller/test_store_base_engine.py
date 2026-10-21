"""Task 0.1.7: naming convention, column helpers, engine/URL construction, logging.

Needs the ``controller`` extra (SQLAlchemy); skipped on a base install.
"""

from __future__ import annotations

import io
import json

import pytest
from pydantic import SecretStr

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import CheckConstraint, ForeignKey, Index, MetaData, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import CreateIndex, CreateTable

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_job_id
from meshforge.common.logging import configure_logging
from meshforge.controller.store.base import (
    ID_MAX_LENGTH,
    NAMING_CONVENTION,
    UINT64_MAX,
    Base,
    TimestampMixin,
    UInt64,
    id_column,
    uint64_check,
)
from meshforge.controller.store.engine import (
    build_url,
    connect_args,
    create_engine,
    create_session_factory,
    database_unavailable,
)
from meshforge.controller.store.errors import DatabaseUnavailableError
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.unit

PASSWORD = "p@ss:/w0rd-xyz"


class _TBase(DeclarativeBase):
    """Separate metadata (same convention) so test tables never leak into Base."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = Base.type_annotation_map


class _Parent(_TBase):
    __tablename__ = "parent"
    id: Mapped[str] = id_column(primary_key=True)


class _Child(TimestampMixin, _TBase):
    __tablename__ = "child"
    __table_args__ = (
        UniqueConstraint("parent_id", "slot"),
        CheckConstraint("slot >= 0", name="slot_non_negative"),
        Index(None, "slot"),
    )
    id: Mapped[str] = id_column(primary_key=True)
    parent_id: Mapped[str] = mapped_column(ForeignKey("parent.id"))
    slot: Mapped[int]


def _ddl() -> str:
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    parts = [
        str(CreateTable(t).compile(dialect=dialect))
        for t in _TBase.metadata.sorted_tables
    ]
    parts += [
        str(CreateIndex(ix).compile(dialect=dialect))
        for t in _TBase.metadata.sorted_tables
        for ix in t.indexes
    ]
    return "\n".join(parts)


def test_naming_convention_gives_deterministic_constraint_names() -> None:
    ddl = _ddl()
    for name in (
        "pk_parent",
        "pk_child",
        "fk_child_parent_id_parent",
        "uq_child_parent_id_slot",
        "ck_child_slot_non_negative",
        "ix_child_slot",
    ):
        assert name in ddl, f"{name} missing from DDL:\n{ddl}"


def test_base_metadata_uses_convention_and_registers_every_model() -> None:
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    import meshforge.controller.store.models  # noqa: F401 - registry import

    # V0.1 had no domain tables; V0.3 G2 added the node registry (0.3.6) and G4
    # the resource history (0.3.11). Every model module must be imported by
    # models/__init__ or Alembic cannot see it. V0.4 G2 (0006) adds the job tables,
    # V0.5 G4 (0011) the cache location index.
    assert set(Base.metadata.tables) == {
        "node",
        "node_session",
        "resource_snapshot",
        "job",
        "job_transition",
        "attempt",
        "attempt_phase",
        "cache_location",
        "named_input",
        "job_input",
        "transfer_event",
        "plan_decision",  # V0.6 G2 (0013)
        "network_link",  # V0.6 G4 (0014)
        "join_token",  # V0.7 G2 (0015)
        "audit_event",  # V0.7 G2 (0015)
        "revoked_cert",  # V0.7 G3 (0016)
        "app_user",  # V0.7 G4 (0018)
        "api_token",  # V0.7 G4 (0018)
        "secret",  # V0.7 G6 (0019)
        "run_record",  # V0.9 G1 (0021)
        "bench_campaign",  # V0.9 G3 (0022)
        "bench_run",  # V0.9 G3 (0022)
        "chunk_record",  # V0.10 G8 (0029)
    }


def test_timestamps_are_timestamptz_with_server_defaults() -> None:
    table = _Child.__table__
    for col in ("created_at", "updated_at"):
        column = table.c[col]
        assert column.type.timezone is True  # type: ignore[attr-defined]
        assert column.server_default is not None
        assert column.nullable is False
    assert table.c.updated_at.onupdate is not None
    assert "TIMESTAMP WITH TIME ZONE DEFAULT now()" in _ddl()


def test_id_column_fits_generated_ids() -> None:
    column = _Parent.__table__.c.id
    assert column.type.length == ID_MAX_LENGTH  # type: ignore[attr-defined]
    assert len(new_job_id()) <= ID_MAX_LENGTH


def test_build_url_from_fields_escapes_password() -> None:
    url = build_url(DatabaseSettings(password=SecretStr(PASSWORD)))
    assert url.drivername == "postgresql+asyncpg"
    assert url.password == PASSWORD
    assert PASSWORD not in repr(url)
    assert PASSWORD not in url.render_as_string(hide_password=True)


def test_build_url_from_url_setting_matches_fields_form() -> None:
    fields = DatabaseSettings(password=SecretStr(PASSWORD))
    via_url = DatabaseSettings(url=fields.url_secret())
    a, b = build_url(fields), build_url(via_url)
    assert (a.username, a.password, a.host, a.port, a.database) == (
        b.username,
        b.password,
        b.host,
        b.port,
        b.database,
    )


def test_connect_args_force_utc_and_app_name() -> None:
    args = connect_args(DatabaseSettings(connect_timeout_s=3, command_timeout_s=7))
    assert args["timeout"] == 3
    assert args["command_timeout"] == 7  # 0.1.7 carry-over: statement timeout
    assert args["server_settings"] == {
        "timezone": "UTC",
        "application_name": "meshforge-controller",
    }


def test_create_engine_does_not_connect_and_logs_masked_url() -> None:
    stream = io.StringIO()
    configure_logging("DEBUG", fmt="json", stream=stream)
    settings = DatabaseSettings(password=SecretStr(PASSWORD), pool_size=3)
    engine = create_engine(settings)
    try:
        assert engine.pool.size() == 3  # type: ignore[attr-defined]
        factory = create_session_factory(engine)
        assert factory.kw["expire_on_commit"] is False
    finally:
        engine.sync_engine.dispose()
    output = stream.getvalue()
    assert PASSWORD not in output
    events = [json.loads(line) for line in output.splitlines() if line.strip()]
    created = [e for e in events if e.get("event") == "store.engine.created"]
    # display_url() masks with "***"; common/logging's URL scrubber may further
    # normalise the mask to "[REDACTED]". Either is acceptable — the password is gone.
    assert created
    url = created[0]["url"]
    assert url.endswith(
        (
            "meshforge:***@127.0.0.1:5432/meshforge",
            "meshforge:[REDACTED]@127.0.0.1:5432/meshforge",
        )
    )


def test_create_engine_with_nullpool_drops_pool_sizing() -> None:
    from sqlalchemy.pool import NullPool

    engine = create_engine(DatabaseSettings(), poolclass=NullPool)
    assert isinstance(engine.pool, NullPool)
    engine.sync_engine.dispose()


def test_database_unavailable_error_is_typed_and_masked() -> None:
    settings = DatabaseSettings(password=SecretStr(PASSWORD))
    err = database_unavailable(settings, ConnectionRefusedError(111, PASSWORD))
    assert isinstance(err, DatabaseUnavailableError)
    assert err.failure_type is FailureType.NETWORK_ERROR
    assert err.retryable is True
    rendered = f"{err} {err!r} {err.to_dict()}"
    assert PASSWORD not in rendered
    assert "ConnectionRefusedError" in rendered


# ------------------------------------------------------------------ UInt64 (G2)


def test_uint64_compiles_to_numeric_20_0_with_a_domain_check() -> None:
    class _U(_TBase):
        __tablename__ = "u"
        __table_args__ = (uint64_check("epoch", name="epoch"),)
        id: Mapped[str] = id_column(primary_key=True)
        epoch: Mapped[int] = mapped_column(UInt64, nullable=False)

    ddl = str(CreateTable(_U.__table__).compile(dialect=postgresql.dialect()))
    assert "epoch NUMERIC(20, 0) NOT NULL" in ddl
    assert f"CONSTRAINT ck_u_epoch CHECK (epoch >= 0 AND epoch <= {UINT64_MAX})" in ddl
    assert UINT64_MAX == 2**64 - 1


def test_uint64_binds_ints_in_range_and_refuses_everything_else() -> None:
    import decimal

    dialect = postgresql.dialect()
    t = UInt64()
    assert t.process_bind_param(0, dialect) == 0
    assert t.process_bind_param(UINT64_MAX, dialect) == UINT64_MAX
    assert t.process_bind_param(None, dialect) is None
    for bad in (-1, UINT64_MAX + 1):
        with pytest.raises(ValueError, match="outside"):
            t.process_bind_param(bad, dialect)
    for wrong in (True, 1.0, "1"):
        with pytest.raises(TypeError, match="expects an int"):
            t.process_bind_param(wrong, dialect)  # type: ignore[arg-type]
    got = t.process_result_value(decimal.Decimal(UINT64_MAX), dialect)
    assert got == UINT64_MAX and type(got) is int
    assert t.process_result_value(None, dialect) is None
