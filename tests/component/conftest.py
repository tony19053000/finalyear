"""Shared component-test fixtures: one real PostgreSQL per session, one DB per test.

``postgres_server`` starts a single ``postgres:16.15-alpine3.24`` container (same
image as ``deploy/docker-compose.dev.yml``) the first time a test asks for it and
removes it at session end; unit runs and component runs that never touch the DB
start nothing. ``pg_settings`` creates a brand-new, empty database (``TEMPLATE
template0``) for each test and drops it afterwards (``WITH (FORCE)``), so migration
tests always see a clean database and tests never observe each other's tables or
data.

Both skip with the reason when Docker is unreachable (via ``docker_available``) or
the controller extra / testcontainers are not installed. Imports of those packages
are lazy so this conftest loads on a base install.

The container lifecycle machinery (labels, owner identity, sweep, SIGINT shield,
cleanup registry) lives in ``tests/_support/mf_test_containers.py`` (module ``containers``).

Container cleanup guarantees (defence in depth, DEFECT-0.1.8-01)
-----------------------------------------------------------------
Fixture finalizers alone are *not* enough: when Ctrl-C arrives twice (a terminal
Ctrl-C reaches ``uv`` and pytest, and ``uv`` forwards it again) the second
``KeyboardInterrupt`` can land in a function-scoped finalizer, escape pytest's
``teardown_exact`` (it only catches test outcomes) and skip every later finalizer,
including the session container's. And testcontainers' Ryuk reaper does **not**
cover that case: testcontainers registers an ``atexit`` hook that stops the Ryuk
container on interpreter exit, and Ryuk only reaps after its client socket has been
gone for ``RYUK_RECONNECTION_TIMEOUT`` (10 s) — so on any exit where ``atexit`` runs,
Ryuk is killed before it reaps. The layers are therefore:

1. **Fixture finalizer** — the normal path; removes the container at session end.
2. **``atexit`` hook** — registered *before* the container starts; removes it on
   any interpreter exit that skipped the finalizer (Ctrl-C, ``KeyboardInterrupt``
   escaping teardown, internal errors). Removal runs under ``sigint_deferred``: the
   SIGINT handler is temporarily replaced by one that only records the signal
   (Python runs handlers in the main thread whichever thread the OS picks, and this
   works on Windows too), so a further Ctrl-C cannot abort it; a recorded Ctrl-C
   is re-raised as ``KeyboardInterrupt`` once removal finished. ``pg_settings``
   also removes the container immediately if a ``KeyboardInterrupt`` hits its
   ``DROP DATABASE``.
3. **Ryuk** — covers exits that run no Python code at all (``SIGKILL``,
   ``os._exit`` from pytest-timeout's ``thread`` method, the default on Windows):
   the socket drops without ``atexit`` stopping Ryuk, so Ryuk removes every
   container with this session's ``org.testcontainers.session-id`` label ~10 s
   later. Ryuk must stay enabled; ``TESTCONTAINERS_RYUK_DISABLED`` is unsupported.
4. **Stale sweep** — before starting a container, ``postgres_server`` runs
   ``sweep_stale_test_containers`` (e.g. Ryuk disabled, or the Docker daemon
   restarted before Ryuk reaped). See the sweep policy below.

pytest-timeout with the ``signal`` method (POSIX default) raises inside the test, so
normal teardown (layer 1) runs.

Docker label scheme (on every MeshForge test container)
-------------------------------------------------------
======================================  ==============================================
``meshforge.test=1``                    marks a MeshForge test container
``meshforge.test.owner-pid``            PID of the pytest process that started it
``meshforge.test.owner-start``          that process's start time (Linux: ``/proc``
                                        stat field 22; else psutil create time) —
                                        detects PID reuse
``meshforge.test.owner-host``           ``socket.gethostname()``
``meshforge.test.owner-ns``             PID-namespace identity: Linux
                                        ``linux:<boot_id>:<inode of /proc/self/ns/
                                        pid>``, Windows ``win32:<MachineGuid>``,
                                        else empty
``meshforge.test.run-id``               ``$MESHFORGE_TEST_RUN_ID`` if set, else
                                        random (tests launching a child pytest set
                                        it to find the child's containers)
======================================  ==============================================

Sweep policy
------------
The sweep is global (every labelled container on the daemon) but removes a
container only if **all** hold: ``owner-ns`` equals this process's namespace
identity (unknown identity ⇒ sweep nothing); ``owner-host`` equals this host; and
the owner is *known* gone — no such PID, or the PID is alive with a different start
time (PID reuse). A host name alone is not an identity: WSL2 distros and Windows
share one Docker Desktop daemon and usually the same computer name but not a PID
namespace, and CI jobs in containers may share a daemon, so ``owner-ns`` keeps one
run from deleting another namespace's live containers. Unlabelled containers,
containers of live runs, containers with an unknown liveness (Windows without
``psutil``) and containers without an ``owner-start`` label whose PID is alive are
never touched.

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
