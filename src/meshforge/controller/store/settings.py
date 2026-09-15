"""Database settings for the controller (``[database]`` section).

Sources follow :class:`~meshforge.common.config.MeshForgeSettings`: init args, then
``MESHFORGE_DATABASE__*`` environment variables, then TOML ``[database]``.

Either set the individual fields (``host``, ``port``, ``name``, ``user``,
``password``) or a full ``url`` that overrides all of them. Defaults match
``deploy/docker-compose.dev.yml`` - they are development-only values for a database
bound to 127.0.0.1.

Only the ``asyncpg`` driver is supported (the controller is fully async; Alembic runs
through the async engine too). ``postgresql://`` and ``postgres://`` URLs are
accepted and normalised to ``postgresql+asyncpg://``.

This module deliberately does not import SQLAlchemy (see
:func:`meshforge.controller.store.engine.build_url` for the ``URL`` object), so it is
importable and testable on a base install.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, SecretStr, field_validator

from meshforge.common.config import MeshForgeSettings, SectionModel

__all__ = [
    "ASYNC_DRIVER_SCHEME",
    "DatabaseSettings",
    "StoreSettings",
]

ASYNC_DRIVER_SCHEME: Final = "postgresql+asyncpg"
_ACCEPTED_SCHEMES: Final = frozenset({"postgresql", "postgres", ASYNC_DRIVER_SCHEME})
_MASK: Final = "***"


class DatabaseSettings(SectionModel):
    """Connection and pool settings for PostgreSQL."""

    url: SecretStr | None = Field(
        default=None,
        description="Full URL; overrides host/port/name/user/password when set.",
    )
    host: str = "127.0.0.1"
    port: int = Field(default=5432, ge=1, le=65535)
    name: str = Field(default="meshforge", min_length=1)
    user: str = Field(default="meshforge", min_length=1)
    password: SecretStr = SecretStr("meshforge-dev")

    connect_timeout_s: float = Field(default=10.0, gt=0)
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    pool_recycle_s: int = Field(default=1800, ge=-1)
    application_name: str = "meshforge-controller"
    echo_sql: bool = False

    @field_validator("url", mode="after")
    @classmethod
    def _normalise_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        # Messages never include the value: it may contain a password.
        scheme, sep, rest = raw.partition("://")
        if not sep:
            raise ValueError("database url must look like postgresql+asyncpg://...")
        if scheme.lower() not in _ACCEPTED_SCHEMES:
            raise ValueError(
                "database url must use postgresql+asyncpg:// (or postgresql://); "
                "other drivers are not supported"
            )
        parts = urlsplit(f"{ASYNC_DRIVER_SCHEME}://{rest}")
        if not parts.hostname:
            raise ValueError("database url has no host")
        if not parts.path.strip("/"):
            raise ValueError("database url has no database name")
        return SecretStr(urlunsplit(parts))

    def url_secret(self) -> SecretStr:
        """The effective ``postgresql+asyncpg://`` URL, password included (secret)."""
        if self.url is not None:
            return self.url
        user = quote(self.user, safe="")
        password = quote(self.password.get_secret_value(), safe="")
        return SecretStr(
            f"{ASYNC_DRIVER_SCHEME}://{user}:{password}@"
            f"{_host_for_url(self.host)}:{self.port}/{quote(self.name, safe='')}"
        )

    def display_url(self) -> str:
        """The effective URL with the password masked - safe for logs and errors."""
        parts = urlsplit(self.url_secret().get_secret_value())
        if parts.password is None:
            return urlunsplit(parts)
        userinfo, _, hostinfo = parts.netloc.rpartition("@")
        user = userinfo.split(":", 1)[0]
        return urlunsplit(parts._replace(netloc=f"{user}:{_MASK}@{hostinfo}"))


def _host_for_url(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


class StoreSettings(MeshForgeSettings):
    """Settings needed by the store alone (Alembic, store tests).

    The future ``ControllerSettings`` (V0.3) will carry the same ``database`` field,
    so ``MESHFORGE_DATABASE__*`` / ``[database]`` keep working unchanged.
    """

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
