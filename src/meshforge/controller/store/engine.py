"""Async engine and session factory (SQLAlchemy 2.0 + asyncpg).

The URL always comes from :class:`~meshforge.controller.store.settings.DatabaseSettings`;
nothing is hard-coded. Every connection runs with ``timezone=UTC`` and an
``application_name`` so sessions are identifiable in ``pg_stat_activity``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from meshforge.common.logging import get_logger
from meshforge.controller.store.errors import DatabaseUnavailableError
from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "build_url",
    "check_connection",
    "connect_args",
    "create_engine",
    "create_session_factory",
]

log = get_logger(__name__)


def build_url(settings: DatabaseSettings) -> URL:
    """SQLAlchemy ``URL`` for ``settings`` (contains the password - never log it raw)."""
    if settings.url is not None:
        return make_url(settings.url.get_secret_value())
    return URL.create(
        "postgresql+asyncpg",
        username=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.name,
    )


def connect_args(settings: DatabaseSettings) -> dict[str, Any]:
    """asyncpg ``connect()`` keyword arguments derived from ``settings``."""
    server_settings = {
        "timezone": "UTC",
        "application_name": settings.application_name,
    }
    if settings.session_role is not None:
        # A startup-packet GUC: the session acts as this role from its first
        # statement (validated identifier, so no quoting issue).
        server_settings["role"] = settings.session_role
    return {
        "timeout": settings.connect_timeout_s,
        "command_timeout": settings.command_timeout_s,
        "server_settings": server_settings,
    }


def create_engine(settings: DatabaseSettings, **overrides: Any) -> AsyncEngine:
    """Create the async engine. Does not connect (use :func:`check_connection`).

    ``overrides`` are passed to :func:`create_async_engine` (e.g. ``poolclass`` for
    Alembic's single-use engine).
    """
    options: dict[str, Any] = {
        "pool_size": settings.pool_size,
        "max_overflow": settings.max_overflow,
        "pool_recycle": settings.pool_recycle_s,
        "pool_pre_ping": True,
        "echo": settings.echo_sql,
        "connect_args": connect_args(settings),
    }
    options.update(overrides)
    if "poolclass" in overrides:  # NullPool & co. reject queue-pool sizing options
        for key in ("pool_size", "max_overflow"):
            if key not in overrides:
                options.pop(key, None)
    engine = create_async_engine(build_url(settings), **options)
    log.debug("store.engine.created", url=settings.display_url())
    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory; objects stay usable after commit (``expire_on_commit=False``)."""
    return async_sessionmaker(engine, expire_on_commit=False)


async def check_connection(engine: AsyncEngine, settings: DatabaseSettings) -> None:
    """Run ``SELECT 1``; raise :class:`DatabaseUnavailableError` if it fails."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # asyncpg, OSError, TimeoutError... all mean "unavailable"
        raise database_unavailable(settings, exc) from exc


def database_unavailable(
    settings: DatabaseSettings, exc: BaseException
) -> DatabaseUnavailableError:
    """Wrap a driver/network error without leaking the password."""
    url = settings.display_url()
    return DatabaseUnavailableError(
        f"cannot connect to PostgreSQL at {url}: {type(exc).__name__}",
        details={"url": url, "cause": type(exc).__name__},
    )
