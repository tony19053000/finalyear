"""Alembic environment (async template, asyncpg).

* URL: from ``config.attributes`` (programmatic use, see :mod:`..migrate`) or from
  :class:`StoreSettings` (``MESHFORGE_DATABASE__*`` / TOML ``[database]``). Never
  from ``alembic.ini``.
* ``target_metadata`` is ``Base.metadata`` after importing the model registry.
* ``compare_type=True`` so column type changes show up in autogenerate / ``check``.
* Connection failures become :class:`DatabaseUnavailableError` with a masked URL.
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection

from meshforge.common.logging import configure_logging, get_logger
from meshforge.controller.store.engine import (
    build_url,
    create_engine,
    database_unavailable,
)
from meshforge.controller.store.migrate import (
    ATTR_CONFIGURE_LOGGING,
    ATTR_DATABASE_SETTINGS,
)
from meshforge.controller.store.models import Base
from meshforge.controller.store.settings import DatabaseSettings, StoreSettings

config = context.config
target_metadata = Base.metadata
log = get_logger("meshforge.controller.store.migrations")


def _database_settings() -> DatabaseSettings:
    given = config.attributes.get(ATTR_DATABASE_SETTINGS)
    if isinstance(given, DatabaseSettings):
        if config.attributes.get(ATTR_CONFIGURE_LOGGING, False):
            configure_logging(component="alembic")
        return given
    settings = StoreSettings.load()
    if config.attributes.get(ATTR_CONFIGURE_LOGGING, True):
        configure_logging(
            settings.log.level, fmt=settings.log.format, component="alembic"
        )
    return settings.database


def _configure(connection: Connection | None = None, **kwargs: object) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        **kwargs,  # type: ignore[arg-type]
    )


def run_migrations_offline(settings: DatabaseSettings) -> None:
    """``--sql`` mode: emit SQL without connecting."""
    _configure(
        url=build_url(settings),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online(settings: DatabaseSettings) -> None:
    engine = create_engine(settings, poolclass=pool.NullPool)
    log.info("store.migrations.connecting", url=settings.display_url())
    try:
        try:
            connection = await engine.connect()
        except Exception as exc:
            raise database_unavailable(settings, exc) from exc
        try:
            await connection.run_sync(_run_sync)
            await connection.commit()
        finally:
            await connection.close()
    finally:
        await engine.dispose()


def main() -> None:
    settings = _database_settings()
    if context.is_offline_mode():
        run_migrations_offline(settings)
    else:
        asyncio.run(run_migrations_online(settings))


main()
