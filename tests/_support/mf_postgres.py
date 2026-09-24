"""Shared PostgreSQL fixtures: one container per pytest session, one database per test.

Moved here from ``tests/component/conftest.py`` in V0.3 G6-B so the ``cluster``
tier (``tests/cluster/conftest.py``) uses the *same* fixtures instead of a copy;
both conftests import ``postgres_server`` and ``pg_settings`` from this module,
which is how pytest registers them for their directory. The container lifecycle
policy (labels, sweep, SIGINT shield, atexit, Ryuk) is documented in
``tests/component/conftest.py`` and implemented in ``mf_test_containers``.

Imported as the top-level module ``mf_postgres`` (``tests/conftest.py`` puts
``tests/_support`` on ``sys.path``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from mf_test_containers import (
    meshforge_test_labels,
    register_cleanup,
    run_all_cleanups,
    sweep_stale_test_containers,
)

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings

POSTGRES_IMAGE: Final = "postgres:16.15-alpine3.24"
_SUPERUSER: Final = "mf"
_SUPERUSER_PASSWORD: Final = "mf-test-pw"
_ADMIN_DB: Final = "mf"
#: Upper bound for one admin statement (CREATE/DROP DATABASE); a hang becomes an
#: ordinary error so later finalizers still run.
_ADMIN_STATEMENT_TIMEOUT_S: Final = 30.0


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
                    await conn.execute(statement, timeout=_ADMIN_STATEMENT_TIMEOUT_S)
            finally:
                await conn.close()

        asyncio.run(go())


@pytest.fixture(scope="session")
def postgres_server(docker_available: None) -> Iterator[PostgresServer]:
    pytest.importorskip("asyncpg", reason="needs the controller extra")
    pytest.importorskip("testcontainers", reason="needs the dev dependency group")
    from testcontainers.community.postgres import PostgresContainer

    pg = PostgresContainer(
        POSTGRES_IMAGE,
        username=_SUPERUSER,
        password=_SUPERUSER_PASSWORD,
        dbname=_ADMIN_DB,
        driver=None,
        labels=meshforge_test_labels(),
    )
    sweep_stale_test_containers(pg.get_docker_client().client)

    # Registered before start(): whatever interrupts from here on, the container
    # is removed by the finalizer, or at interpreter exit if teardown is skipped.
    cleanup = register_cleanup(pg)
    try:
        pg.start()
        yield PostgresServer(
            host=pg.get_container_host_ip(),
            port=int(pg.get_exposed_port(5432)),
            user=_SUPERUSER,
            password=_SUPERUSER_PASSWORD,
            admin_db=_ADMIN_DB,
        )
    finally:
        cleanup()


@pytest.fixture
def pg_settings(postgres_server: PostgresServer) -> Iterator[DatabaseSettings]:
    """Settings for a fresh, empty database owned by the superuser; dropped after."""
    name = f"mf_test_{uuid4().hex[:16]}"
    postgres_server.execute_admin(f'CREATE DATABASE "{name}" TEMPLATE template0')
    try:
        yield postgres_server.settings(name)
    finally:
        try:
            postgres_server.execute_admin(
                f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
            )
        except KeyboardInterrupt:
            # This interrupt will skip the remaining pytest finalizers: remove the
            # session container now (the database dies with it), then re-raise.
            run_all_cleanups()
            raise
