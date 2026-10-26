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
import concurrent.futures
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import uuid4

import pytest
from mf_managed_pg import (
    SessionCluster,
    managed_pg_bin_dir,
    managed_pg_requested,
    short_dir,
)
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

        try:
            asyncio.get_running_loop()
        except RuntimeError:  # the usual case: no loop in this thread
            asyncio.run(go())
            return
        # A loop already runs in this thread - Playwright's sync API keeps one
        # in the main thread for the whole e2e session (V0.8 G2): run the
        # statements on a fresh loop in a worker thread instead.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, go()).result()


@pytest.fixture(scope="session")
def postgres_server(request: pytest.FixtureRequest) -> Iterator[PostgresServer]:
    pytest.importorskip("asyncpg", reason="needs the controller extra")
    if managed_pg_requested():
        # V0.17 G2 (SP2 R5): MESHFORGE_TEST_PG=managed runs the whole suite on
        # the controller's bundled PostgreSQL instead of the Docker image.
        yield from _managed_server()
        return
    external = external_pg_server()
    if external is not None:
        # V0.17 G4: the Windows runner has no Linux containers; its preinstalled
        # PostgreSQL serves the package-windows fabric test.
        yield external
        return
    request.getfixturevalue("docker_available")
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


EXTERNAL_PG_PREFIX: Final = "MESHFORGE_TEST_PG_"
"""``MESHFORGE_TEST_PG=external`` + ``MESHFORGE_TEST_PG_{HOST,PORT,USER,PASSWORD,DB}``:
an already running server (superuser credentials; test databases are created
and dropped in it like in the container)."""


def external_pg_server(
    environ: Mapping[str, str] | None = None,
) -> PostgresServer | None:
    """The ``external`` server named by the environment, or None (not asked)."""
    env = os.environ if environ is None else environ
    if env.get("MESHFORGE_TEST_PG", "").strip().lower() != "external":
        return None

    def get(key: str, default: str) -> str:
        return env.get(EXTERNAL_PG_PREFIX + key) or default

    return PostgresServer(
        host=get("HOST", "127.0.0.1"),
        port=int(get("PORT", "5432")),
        user=get("USER", "postgres"),
        password=get("PASSWORD", ""),
        admin_db=get("DB", "postgres"),
    )


def _managed_server() -> Iterator[PostgresServer]:
    with short_dir("mfpg-suite-") as root:
        session = SessionCluster(managed_pg_bin_dir(), root / "db")
        session.start()
        try:
            cluster = session.cluster
            yield PostgresServer(
                host="127.0.0.1",
                port=cluster.port,
                user=cluster.superuser,
                password=session.superuser_password,
                admin_db="postgres",
            )
        finally:
            session.stop()


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
