"""Programmatic Alembic configuration (no ``alembic.ini`` needed).

Used by tests and by the controller at start-up (:func:`upgrade_to_head`)::

    cfg = alembic_config(DatabaseSettings(url=...))
    alembic.command.upgrade(cfg, "head")

:func:`upgrade_to_head` is **blocking** (Alembic runs its own ``asyncio.run`` inside
``env.py``), so a running event loop must call it via ``asyncio.to_thread``.

The migration scripts live inside the package (``store/migrations``) so they ship
with it. ``env.py`` reads the settings from ``cfg.attributes`` when present,
otherwise it loads :class:`~meshforge.controller.store.settings.StoreSettings`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from meshforge.common.logging import get_logger
from meshforge.controller.store.errors import MigrationError
from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "ATTR_CONFIGURE_LOGGING",
    "ATTR_DATABASE_SETTINGS",
    "MIGRATIONS_DIR",
    "MigrationOutcome",
    "alembic_config",
    "current_revision",
    "head_revision",
    "upgrade_to_head",
]

log = get_logger(__name__)

MIGRATIONS_DIR: Final = Path(__file__).resolve().parent / "migrations"

ATTR_DATABASE_SETTINGS: Final = "meshforge.database_settings"
"""``Config.attributes`` key: :class:`DatabaseSettings` to use instead of loading them."""

ATTR_CONFIGURE_LOGGING: Final = "meshforge.configure_logging"
"""``Config.attributes`` key: ``False`` stops ``env.py`` reconfiguring logging."""


def alembic_config(
    database: DatabaseSettings | None = None, *, configure_logging: bool = False
) -> Config:
    """Alembic ``Config`` pointing at the packaged migrations.

    Args:
        database: settings to migrate; ``None`` loads them from env/TOML in ``env.py``.
        configure_logging: let ``env.py`` configure MeshForge logging (the host
            application normally already did).
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("path_separator", "os")
    cfg.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    if database is not None:
        cfg.attributes[ATTR_DATABASE_SETTINGS] = database
    cfg.attributes[ATTR_CONFIGURE_LOGGING] = configure_logging
    return cfg


def head_revision(cfg: Config | None = None) -> str:
    """The single head of the packaged migration chain (e.g. ``"0002"``)."""
    script = ScriptDirectory.from_config(cfg or alembic_config())
    heads = script.get_heads()
    if len(heads) != 1:  # a merge is missing; refuse to guess
        raise MigrationError(
            f"migration chain has {len(heads)} heads, expected 1: {heads}",
            details={"heads": list(heads)},
        )
    return heads[0]


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    """What :func:`upgrade_to_head` did."""

    target_revision: str
    current_revision: str | None
    """Revision the database reported *before* the upgrade (None = empty database)."""


def upgrade_to_head(database: DatabaseSettings) -> MigrationOutcome:
    """Run ``alembic upgrade head`` synchronously (call via ``asyncio.to_thread``).

    Idempotent. On any failure logs ``store.migrations.failed`` with the target and
    current revisions and raises :class:`MigrationError` (exit ``CONFIG_ERROR``).
    """
    cfg = alembic_config(database)
    target = head_revision(cfg)
    current: str | None = None
    try:
        current = _current_revision(database)
        log.info(
            "store.migrations.upgrading",
            target_revision=target,
            current_revision=current,
        )
        command.upgrade(cfg, "head")
    except MigrationError:
        raise
    except Exception as exc:  # alembic/sqlalchemy/asyncpg all end here
        log.exception(
            "store.migrations.failed",
            target_revision=target,
            current_revision=current,
            cause=type(exc).__name__,
        )
        raise MigrationError(
            f"database migration to revision {target} failed: {type(exc).__name__}",
            details={"target_revision": target, "current_revision": current},
        ) from exc
    log.info("store.migrations.upgraded", revision=target)
    return MigrationOutcome(target_revision=target, current_revision=current)


def current_revision(database: DatabaseSettings) -> str | None:
    """The database's migration revision, or None for an empty database.
    Blocking (call via ``asyncio.to_thread``)."""
    return _current_revision(database)


def _current_revision(database: DatabaseSettings) -> str | None:
    """``alembic_version.version_num`` or None (no table yet). Blocking."""
    import asyncio

    from sqlalchemy import pool, text

    from meshforge.controller.store.engine import create_engine

    async def go() -> str | None:
        engine = create_engine(database, poolclass=pool.NullPool)
        try:
            async with engine.connect() as conn:
                exists = await conn.execute(
                    text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
                )
                if not exists.scalar_one():
                    return None
                row = await conn.execute(
                    text("SELECT version_num FROM alembic_version")
                )
                versions = [str(v) for v in row.scalars()]
                return versions[0] if versions else None
        finally:
            await engine.dispose()

    return asyncio.run(go())
