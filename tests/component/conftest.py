"""Shared component-test fixtures: one real PostgreSQL per session, one DB per test.

``postgres_server`` starts a single ``postgres:16.15-alpine3.24`` container (same
image as ``deploy/docker-compose.dev.yml``) the first time a test asks for it and
stops it at session end; unit runs and component runs that never touch the DB start
nothing. ``pg_settings`` creates a brand-new, empty database (``TEMPLATE template0``)
for each test and drops it afterwards (``WITH (FORCE)``), so migration tests always
see a clean database and tests never observe each other's tables or data.

Both skip with the reason when Docker is unreachable (via ``docker_available``) or
the controller extra / testcontainers are not installed. Imports of those packages
are lazy so this conftest loads on a base install.

Container cleanup relies on testcontainers' Ryuk reaper, which removes the container
even if this process is killed (e.g. by pytest-timeout or Ctrl-C) before the fixture
finalizer runs. Ryuk must therefore stay enabled: ``TESTCONTAINERS_RYUK_DISABLED`` is
unsupported for this suite and may leak containers.

Future ``cluster`` fixtures (controller + N agents as local processes) must not rely
on fixture teardown after a timeout either: pytest-timeout can abort without running
finalizers, so spawned processes must be contained so they die with the test run
(POSIX process groups / Windows Job Objects) — to be designed in V0.2+.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings

POSTGRES_IMAGE: Final = "postgres:16.15-alpine3.24"
_SUPERUSER: Final = "mf"
_SUPERUSER_PASSWORD: Final = "mf-test-pw"
_ADMIN_DB: Final = "mf"


@dataclass(frozen=True)
class PostgresServer:
    """Connection facts for the session container (superuser credentials)."""

    host: str
    port: int
    user: str
    password: str
    admin_db: str

    def settings(self, database: str) -> DatabaseSettings:
        from pydantic import SecretStr

        from meshforge.controller.store.settings import DatabaseSettings

        return DatabaseSettings(
            host=self.host,
            port=self.port,
            name=database,
            user=self.user,
            password=SecretStr(self.password),
            connect_timeout_s=10,
        )

    def execute_admin(self, *statements: str, database: str | None = None) -> None:
        """Run statements outside a transaction (needed for CREATE/DROP DATABASE)."""
        import asyncpg

        async def go() -> None:
            conn = await asyncpg.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=database or self.admin_db,
                timeout=10,
            )
            try:
                for statement in statements:
                    await conn.execute(statement)
            finally:
                await conn.close()

        asyncio.run(go())


@pytest.fixture(scope="session")
def postgres_server(docker_available: None) -> Iterator[PostgresServer]:
    pytest.importorskip("asyncpg", reason="needs the controller extra")
    pytest.importorskip("testcontainers", reason="needs the dev dependency group")
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer(
        POSTGRES_IMAGE,
        username=_SUPERUSER,
        password=_SUPERUSER_PASSWORD,
        dbname=_ADMIN_DB,
        driver=None,
    ) as pg:
        yield PostgresServer(
            host=pg.get_container_host_ip(),
            port=int(pg.get_exposed_port(5432)),
            user=_SUPERUSER,
            password=_SUPERUSER_PASSWORD,
            admin_db=_ADMIN_DB,
        )


@pytest.fixture
def pg_settings(postgres_server: PostgresServer) -> Iterator[DatabaseSettings]:
    """Settings for a fresh, empty database owned by the superuser; dropped after."""
    name = f"mf_test_{uuid4().hex[:16]}"
    postgres_server.execute_admin(f'CREATE DATABASE "{name}" TEMPLATE template0')
    try:
        yield postgres_server.settings(name)
    finally:
        postgres_server.execute_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
