"""Programmatic Alembic configuration (no ``alembic.ini`` needed).

Used by tests and, later, by controller commands that upgrade the schema::

    cfg = alembic_config(DatabaseSettings(url=...))
    alembic.command.upgrade(cfg, "head")

The migration scripts live inside the package (``store/migrations``) so they ship
with it. ``env.py`` reads the settings from ``cfg.attributes`` when present,
otherwise it loads :class:`~meshforge.controller.store.settings.StoreSettings`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from alembic.config import Config

from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "ATTR_CONFIGURE_LOGGING",
    "ATTR_DATABASE_SETTINGS",
    "MIGRATIONS_DIR",
    "alembic_config",
]

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
