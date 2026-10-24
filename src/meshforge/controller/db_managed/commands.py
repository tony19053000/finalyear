"""Pure command construction for the managed cluster (no I/O, unit-tested).

Every program gets a **fresh, minimal environment** (:func:`child_env`): inherited
``PG*`` variables (``PGDATA``, ``PGHOST``, ``PGPASSWORD``, ``PGOPTIONS``...) would
silently change what ``initdb``/``pg_dump`` do, so nothing is inherited. No
password is ever placed on a command line.

**The postmaster runs as our direct child** (``postgres -D ...``, not ``pg_ctl
start``, which double-forks and so orphans the server by design). On Linux it is
started through a tiny exec shim that arms ``PR_SET_PDEATHSIG = SIGINT`` before
``execve``: if the controller process dies for any reason (SIGKILL, OOM kill), the
kernel sends the postmaster SIGINT = *fast shutdown* (roll back, checkpoint, exit
cleanly). SIGINT, not SIGKILL like the agent's workload shim: a SIGKILLed
postmaster leaves its backends and shared memory behind (PostgreSQL docs, "Shutting
Down the Server"). The shim also checks the parent is still alive after arming the
signal (the race where the controller died before ``prctl``). Limit: the signal
fires when the *thread* that forked exits - start the cluster from the event-loop
thread, never from ``asyncio.to_thread``. The restart-time reconciliation in
:mod:`.procs` covers everything the death signal cannot (host crash, a start from
the wrong thread, non-Linux).
"""

from __future__ import annotations

import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from meshforge.controller.db_managed.binaries import PgBinaries
from meshforge.controller.db_managed.layout import ManagedLayout

__all__ = [
    "DEATH_SIGNAL",
    "LISTEN_ADDRESS",
    "child_env",
    "dump_argv",
    "initdb_argv",
    "postgres_argv",
    "restore_argv",
    "server_settings",
    "with_death_signal",
]

LISTEN_ADDRESS: Final = "127.0.0.1"
"""TCP is loopback only (D390); remote clients never reach the database."""

DEATH_SIGNAL: Final = signal.SIGINT
"""Fast shutdown when the controller dies (module docstring)."""

#: Exec shim: argv = parent pid, signal number, executable, *argv. Stdlib only,
#: isolated (-I) and without site (-S). 125 = could not arm, 137 = parent already gone.
_DEATH_SIGNAL_SHIM: Final = r"""
import ctypes, os, sys
ppid, sig, exe, argv = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4:]
try:
    if ctypes.CDLL(None, use_errno=True).prctl(1, sig, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
except (OSError, AttributeError) as exc:
    sys.stderr.write("meshforge: cannot arm the parent-death signal: %s\n" % exc)
    os._exit(125)
if os.getppid() != ppid:
    os._exit(137)
os.execv(exe, argv)
"""


def child_env(*, pgpassfile: Path | None = None) -> dict[str, str]:
    """The whole environment of a PostgreSQL program (nothing inherited).

    ``LC_ALL=C``: messages and initdb's locale probing do not depend on the
    host's locale (a laptop without ``en_US.UTF-8`` must work), and Python's
    PEP 538 coercion in the shim stays off. ``TZ=UTC`` matches the controller.
    """
    env = {"LC_ALL": "C", "TZ": "UTC", "PATH": os.defpath}
    if pgpassfile is not None:
        env["PGPASSFILE"] = str(pgpassfile)
    return env


def initdb_argv(
    binaries: PgBinaries, *, data_dir: Path, superuser: str, password_file: Path
) -> list[str]:
    """``initdb`` for a new cluster.

    * ``scram-sha-256`` for unix-socket and TCP connections (no ``trust``);
    * password from the owner-only file (``--pwfile``), never argv;
    * ``UTF8`` + ``C`` locale: byte-order collation, no dependence on host
      locales and no ICU collation-version drift when the bundle's ICU changes
      (trade-off: ``upper()``/``lower()`` of non-ASCII follow C rules);
    * data checksums on: silent disk corruption becomes a loud error.
    """
    return [
        str(binaries.program("initdb")),
        "--pgdata",
        str(data_dir),
        "--username",
        superuser,
        f"--pwfile={password_file}",
        "--auth-local=scram-sha-256",
        "--auth-host=scram-sha-256",
        "--encoding=UTF8",
        "--locale=C",
        "--data-checksums",
    ]


def server_settings(layout: ManagedLayout, *, port: int) -> dict[str, str]:
    """``-c`` settings forced on every start (they win over ``postgresql.conf``,
    so a hand edit cannot open the database to the network)."""
    return {
        "listen_addresses": LISTEN_ADDRESS,
        "port": str(port),
        "unix_socket_directories": str(layout.socket_dir),
        "unix_socket_permissions": "0700",
        "timezone": "UTC",
        "log_timezone": "UTC",
        "logging_collector": "on",
        "log_directory": str(layout.log_dir),
        "log_filename": "postgresql-%a.log",
        "log_truncate_on_rotation": "on",
        "log_rotation_age": "1d",
        "log_rotation_size": "0",
    }


def postgres_argv(
    binaries: PgBinaries, layout: ManagedLayout, *, port: int
) -> list[str]:
    """The postmaster command line (run in the foreground as our child)."""
    argv = [str(binaries.program("postgres")), "-D", str(layout.data_dir)]
    for key, value in server_settings(layout, port=port).items():
        argv += ["-c", f"{key}={value}"]
    return argv


def with_death_signal(
    argv: Sequence[str],
    *,
    parent_pid: int,
    signum: int = DEATH_SIGNAL,
    python: str | None = None,
    platform: str = sys.platform,
) -> list[str]:
    """``argv`` wrapped in the parent-death shim on Linux; unchanged elsewhere.

    The shim ``execve``s the real program, so the pid the caller gets back *is*
    the postmaster's pid.
    """
    if not platform.startswith("linux"):
        return list(argv)
    return [
        python or sys.executable,
        "-I",
        "-S",
        "-c",
        _DEATH_SIGNAL_SHIM,
        str(parent_pid),
        str(int(signum)),
        argv[0],
        *argv,
    ]


def _connection_args(layout: ManagedLayout, *, port: int, user: str) -> list[str]:
    # Through the unix socket: no TCP needed, and the socket dir is 0700.
    return ["--host", str(layout.socket_dir), "--port", str(port), "--username", user]


def dump_argv(
    binaries: PgBinaries,
    layout: ManagedLayout,
    *,
    port: int,
    user: str,
    database: str,
    out_file: Path,
) -> list[str]:
    """``pg_dump`` in custom format (compressed, restorable selectively)."""
    return [
        str(binaries.program("pg_dump")),
        *_connection_args(layout, port=port, user=user),
        "--no-password",
        "--format=custom",
        f"--file={out_file}",
        f"--dbname={database}",
    ]


def restore_argv(
    binaries: PgBinaries,
    layout: ManagedLayout,
    *,
    port: int,
    user: str,
    database: str,
    dump_file: Path,
) -> list[str]:
    """``pg_restore`` of a custom-format dump into an **existing, empty** database,
    all-or-nothing (``--single-transaction`` + ``--exit-on-error``)."""
    return [
        str(binaries.program("pg_restore")),
        *_connection_args(layout, port=port, user=user),
        "--no-password",
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
        f"--dbname={database}",
        str(dump_file),
    ]
