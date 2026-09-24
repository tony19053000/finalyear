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

The ``cluster`` tier (V0.3 G6-B) uses the same fixtures: their code lives in
``tests/_support/mf_postgres.py`` and both conftests import it. Its spawned
controller/agent processes are contained by ``tests/_support/mf_cluster.py``
(process groups, parent-death signal on Linux, atexit backstop) because
pytest-timeout can abort without running finalizers.
"""

from __future__ import annotations

# The fixtures live in tests/_support so the cluster tier shares them (G6-B);
# importing them here is what registers them for tests/component.
from mf_postgres import pg_settings, postgres_server  # noqa: F401
