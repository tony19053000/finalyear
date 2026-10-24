"""Lifecycle of the controller-managed PostgreSQL cluster (spike SP2 for D390).

:class:`ManagedCluster` owns one cluster under a :class:`~.layout.ManagedLayout`:

``start()``
    1. take ``managed.lock`` (one controller per cluster; released when this
       process dies, so a crashed controller never blocks the next start);
    2. check the bundle (programs present, ``postgres --version`` major);
    3. create the owner-only dirs and the password file (generated once);
    4. ``initdb`` once - into ``data.initdb-tmp`` then renamed to ``data``, so a
       crash mid-initdb leaves no half cluster - or refuse a ``PG_VERSION`` of
       another major (:class:`~.errors.ClusterVersionMismatchError`) / a non-empty
       non-cluster dir;
    5. **reconcile** what a crashed controller left (:meth:`reconcile`): stop an
       orphan postmaster of this data dir, remove a stale ``postmaster.pid``;
    6. spawn ``postgres`` as a direct child (own session, parent-death SIGINT on
       Linux - see :mod:`.commands`) bound to the unix socket dir + ``127.0.0.1``;
    7. wait for ``postmaster.pid`` status ``ready`` (no client needed);
    8. create the application database if missing.

``stop()``
    smart (SIGTERM) -> fast (SIGINT) -> immediate (SIGQUIT) -> SIGKILL of the
    whole process group, each with its own timeout.

Not wired into the controller yet (G2 adds ``[controller.db] mode = "managed"``).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Final

from pydantic import SecretStr

from meshforge.common.atomic import atomic_write_bytes
from meshforge.common.errors import FailureType
from meshforge.common.filelock import try_lock, unlock
from meshforge.common.logging import get_logger
from meshforge.controller.db_managed.binaries import BUNDLED_MAJOR, PgBinaries
from meshforge.controller.db_managed.commands import (
    LISTEN_ADDRESS,
    child_env,
    dump_argv,
    initdb_argv,
    postgres_argv,
    restore_argv,
    with_death_signal,
)
from meshforge.controller.db_managed.errors import (
    ClusterVersionMismatchError,
    ManagedDbError,
    ManagedDbReason,
)
from meshforge.controller.db_managed.layout import ManagedLayout, check_socket_dir
from meshforge.controller.db_managed.pidfile import (
    PIDFILE_NAME,
    PidFileError,
    PostmasterStatus,
    check_cluster_major,
    read_cluster_major,
    read_postmaster_pid,
)
from meshforge.controller.db_managed.procs import (
    data_dir_holders,
    is_postgres_of,
    pid_alive,
    stop_foreign,
)
from meshforge.controller.db_managed.secret import ensure_password_file, pgpass_line
from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "ManagedCluster",
    "ReconcileKind",
    "ReconcileOutcome",
    "StartOutcome",
    "StopOutcome",
    "StopTimeouts",
]

log = get_logger(__name__)

_IDENT: Final = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_LOG_TAIL_BYTES: Final = 2000
_POLL_S: Final = 0.02
_TOOL_TIMEOUT_S: Final = 600.0
_INITDB_TIMEOUT_S: Final = 300.0


@dataclass(frozen=True, slots=True)
class StopTimeouts:
    """Seconds to wait after each shutdown signal before escalating."""

    smart_s: float = 5.0
    fast_s: float = 30.0
    immediate_s: float = 10.0
    kill_s: float = 5.0


class ReconcileKind(StrEnum):
    CLEAN = "clean"
    """No lock file, no process: nothing to do."""
    ORPHAN_STOPPED = "orphan_stopped"
    """A postmaster of this data dir (left by a crashed controller) was stopped."""
    STRAY_STOPPED = "stray_stopped"
    """Backends of this data dir without a postmaster were stopped."""
    STALE_PIDFILE_REMOVED = "stale_pidfile_removed"
    """``postmaster.pid`` named a dead or reused pid; removed."""
    CORRUPT_PIDFILE_REMOVED = "corrupt_pidfile_removed"


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    kind: ReconcileKind
    pids: tuple[int, ...] = ()
    signal: str | None = None
    duration_s: float = 0.0


@dataclass(frozen=True, slots=True)
class StartOutcome:
    pid: int
    server_version: str
    initdb_s: float | None
    """Seconds ``initdb`` took; None when the cluster already existed."""
    ready_s: float
    """Spawn to ``ready`` in ``postmaster.pid``."""
    total_s: float
    reconcile: ReconcileOutcome
    password_created: bool


@dataclass(frozen=True, slots=True)
class StopOutcome:
    mode: str
    """``smart``/``fast``/``immediate``/``kill``, or ``already_exited``."""
    exit_code: int | None
    duration_s: float


class ManagedCluster:
    """One managed cluster; see the module docstring."""

    def __init__(
        self,
        binaries: PgBinaries,
        layout: ManagedLayout,
        *,
        port: int,
        superuser: str = "meshforge",
        database: str = "meshforge",
        start_timeout_s: float = 60.0,
        stop_timeouts: StopTimeouts | None = None,
        die_with_parent: bool = True,
    ) -> None:
        for what, value in (("superuser", superuser), ("database", database)):
            if not _IDENT.match(value):
                raise ValueError(f"{what} must be a lower-case identifier: {value!r}")
        if not 1 <= port <= 65535:
            raise ValueError(f"port out of range: {port}")
        self.binaries = binaries
        self.layout = layout
        self.port = port
        self.superuser = superuser
        self.database = database
        self.start_timeout_s = start_timeout_s
        self.stop_timeouts = stop_timeouts or StopTimeouts()
        self.die_with_parent = die_with_parent
        self._proc: asyncio.subprocess.Process | None = None
        self._lock_fh: IO[bytes] | None = None
        self._password: SecretStr | None = None
        self._log_marks: dict[Path, int] = {}

    # ------------------------------------------------------------ properties
    @property
    def pid(self) -> int | None:
        """The postmaster pid while running (our child)."""
        proc = self._proc
        return proc.pid if proc is not None and proc.returncode is None else None

    def database_settings(self, *, database: str | None = None) -> DatabaseSettings:
        """Controller connection settings (TCP loopback) - after :meth:`start`."""
        return DatabaseSettings(
            host=LISTEN_ADDRESS,
            port=self.port,
            name=database or self.database,
            user=self.superuser,
            password=self._require_password(),
        )

    def _require_password(self) -> SecretStr:
        if self._password is None:
            raise ManagedDbError(
                "the managed database is not started",
                reason=ManagedDbReason.NOT_RUNNING,
            )
        return self._password

    # ------------------------------------------------------------------ start
    async def start(self) -> StartOutcome:
        """Prepare, reconcile and start the cluster; see the module docstring.

        Must run on the event-loop thread (the parent-death signal follows the
        forking thread). On any failure nothing is left running and the lock is
        released.
        """
        if self._proc is not None:
            raise RuntimeError("cluster already started by this object")
        started = time.monotonic()
        self._acquire_lock()
        try:
            version = await asyncio.to_thread(self.binaries.check)
            check_socket_dir(self.layout.socket_dir, self.port)
            self.layout.prepare()
            password, created = ensure_password_file(self.layout.password_file)
            self._password = password
            initdb_s = await self._ensure_initialized()
            reconcile = await self.reconcile()
            pid, ready_s = await self._spawn_and_wait()
            await self._ensure_database()
        except BaseException:
            await self._abort_start()
            raise
        outcome = StartOutcome(
            pid=pid,
            server_version=version,
            initdb_s=initdb_s,
            ready_s=ready_s,
            total_s=time.monotonic() - started,
            reconcile=reconcile,
            password_created=created,
        )
        log.info(
            "controller.db.managed.started",
            pid=outcome.pid,
            port=self.port,
            data_dir=str(self.layout.data_dir),
            server_version=version,
            initdb_ms=None if initdb_s is None else round(initdb_s * 1000),
            ready_ms=round(ready_s * 1000),
            total_ms=round(outcome.total_s * 1000),
            reconcile=reconcile.kind.value,
        )
        return outcome

    def _acquire_lock(self) -> None:
        self.layout.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fh = open(self.layout.lock_file, "a+b")  # noqa: SIM115 - held until stop()
        if os.name == "posix":
            os.fchmod(fh.fileno(), 0o600)
        if not try_lock(fh):
            fh.close()
            raise ManagedDbError(
                f"another process already manages the database in {self.layout.root}",
                reason=ManagedDbReason.LOCKED,
                details={"root": str(self.layout.root)},
            )
        self._lock_fh = fh

    def _release_lock(self) -> None:
        fh, self._lock_fh = self._lock_fh, None
        if fh is not None:
            with contextlib.suppress(OSError):
                unlock(fh)
            fh.close()

    async def _abort_start(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(Exception):
                await self._shutdown(StopTimeouts(smart_s=0, fast_s=10, immediate_s=5))
        self._proc = None
        self._release_lock()

    # ----------------------------------------------------------------- initdb
    async def _ensure_initialized(self) -> float | None:
        data = self.layout.data_dir
        if data.exists():
            major = read_cluster_major(data)
            if major is None:
                if any(data.iterdir()):
                    raise ManagedDbError(
                        f"{data} is not empty but holds no PostgreSQL cluster "
                        "(no PG_VERSION); refusing to touch it",
                        reason=ManagedDbReason.DATA_DIR_INVALID,
                        details={"data_dir": str(data)},
                    )
                data.rmdir()  # empty: initialise below
            elif not check_cluster_major(major, BUNDLED_MAJOR):
                raise ClusterVersionMismatchError(
                    data_dir=str(data), data_major=major, binary_major=BUNDLED_MAJOR
                )
            else:
                return None
        return await self._initdb()

    async def _initdb(self) -> float:
        tmp = self.layout.initdb_tmp_dir
        if tmp.exists():  # a previous initdb was interrupted
            log.warning("controller.db.managed.initdb_leftover_removed", path=str(tmp))
            await asyncio.to_thread(shutil.rmtree, tmp)
        argv = initdb_argv(
            self.binaries,
            data_dir=tmp,
            superuser=self.superuser,
            password_file=self.layout.password_file,
        )
        started = time.monotonic()
        code, output = await _run(argv, timeout_s=_INITDB_TIMEOUT_S)
        elapsed = time.monotonic() - started
        if code != 0:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(shutil.rmtree, tmp)
            raise ManagedDbError(
                f"initdb failed (exit {code}): {_tail(output)}",
                reason=ManagedDbReason.INITDB_FAILED,
                details={"exit_code": code, "output_tail": _tail(output)},
            )
        os.replace(tmp, self.layout.data_dir)
        _fsync_dir(self.layout.root)
        log.info(
            "controller.db.managed.initdb",
            data_dir=str(self.layout.data_dir),
            initdb_ms=round(elapsed * 1000),
        )
        return elapsed

    # -------------------------------------------------------------- reconcile
    async def reconcile(self) -> ReconcileOutcome:
        """Clean up after a controller that died without stopping the cluster.

        Called with the lock held, so no live controller owns any process found.
        Adoption was rejected: a postmaster that is not our child cannot be
        ``wait()``ed for, has no parent-death signal tied to us, and may run with
        settings of an older version - it is stopped (fast shutdown) and started
        again as our child; this costs one checkpoint.
        """
        data = self.layout.data_dir
        started = time.monotonic()
        try:
            pidfile = read_postmaster_pid(data)
            corrupt = False
        except PidFileError:
            pidfile, corrupt = None, True
        if pidfile is not None and is_postgres_of(pidfile.pid, data):
            result = await stop_foreign(pidfile.pid)
            if not result.stopped:
                raise ManagedDbError(
                    f"a PostgreSQL server (pid {pidfile.pid}) left running in "
                    f"{data} could not be stopped",
                    reason=ManagedDbReason.ORPHAN_STOP_FAILED,
                    details={"pid": pidfile.pid},
                )
            # A SIGKILLed postmaster leaves backends that exit on their own
            # shortly; wait for them before anything touches the lock file.
            await self._stop_strays()
            outcome = ReconcileOutcome(
                ReconcileKind.ORPHAN_STOPPED,
                (pidfile.pid,),
                result.signal,
                time.monotonic() - started,
            )
            log.warning(
                "controller.db.managed.orphan_stopped",
                pid=pidfile.pid,
                signal=result.signal,
                duration_ms=round(outcome.duration_s * 1000),
            )
            return outcome
        strays = await self._stop_strays()
        kind = ReconcileKind.CLEAN
        if strays:
            kind = ReconcileKind.STRAY_STOPPED
        if corrupt or pidfile is not None:
            # Nothing of ours runs in the data dir (checked above), so the lock
            # file is stale whatever its pid now is (dead, or reused by an
            # unrelated process - which PostgreSQL would refuse to start over).
            with contextlib.suppress(FileNotFoundError):
                (data / PIDFILE_NAME).unlink()
            if kind is ReconcileKind.CLEAN:
                kind = (
                    ReconcileKind.CORRUPT_PIDFILE_REMOVED
                    if corrupt
                    else ReconcileKind.STALE_PIDFILE_REMOVED
                )
            log.warning(
                "controller.db.managed.pidfile_removed",
                corrupt=corrupt,
                stale_pid=None if pidfile is None else pidfile.pid,
                stale_pid_alive=None if pidfile is None else pid_alive(pidfile.pid),
            )
        return ReconcileOutcome(kind, tuple(strays), None, time.monotonic() - started)

    async def _stop_strays(self) -> list[int]:
        holders = await asyncio.to_thread(data_dir_holders, self.layout.data_dir)
        for pid in holders:
            await stop_foreign(
                pid, steps=((signal.SIGQUIT, 5.0), (signal.SIGKILL, 5.0))
            )
        if holders:
            log.warning("controller.db.managed.strays_stopped", pids=holders)
        return holders

    # ------------------------------------------------------------ spawn/ready
    def _open_startup_log(self) -> IO[bytes]:
        """``startup.log`` for the child's stdout/stderr (closed by the caller
        right after the spawn: the child keeps its own descriptor)."""
        self._mark_logs()
        log_fh = open(self.layout.startup_log, "ab")  # noqa: SIM115 - caller closes
        log_fh.write(f"--- meshforge start {time.time():.0f} ---\n".encode())
        log_fh.flush()
        return log_fh

    async def _spawn_and_wait(self) -> tuple[int, float]:
        """Spawn the postmaster; ``(pid, seconds to ready)``."""
        argv = postgres_argv(self.binaries, self.layout, port=self.port)
        if self.die_with_parent:
            argv = with_death_signal(argv, parent_pid=os.getpid())
        started = time.monotonic()
        with self._open_startup_log() as log_fh:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                env=child_env(),
                cwd=str(self.layout.root),
                start_new_session=True,
            )
        self._proc = proc
        deadline = started + self.start_timeout_s
        while True:
            if proc.returncode is not None:
                raise self._start_failed(proc.returncode)
            try:
                info = read_postmaster_pid(self.layout.data_dir)
            except PidFileError:
                info = None  # being written
            if (
                info is not None
                and info.pid == proc.pid
                and info.status is PostmasterStatus.READY
            ):
                return proc.pid, time.monotonic() - started
            if time.monotonic() >= deadline:
                raise ManagedDbError(
                    f"PostgreSQL did not become ready within {self.start_timeout_s:g}s",
                    reason=ManagedDbReason.START_TIMEOUT,
                    failure_type=FailureType.TIMEOUT,
                    details={"log_tail": self._startup_log_tail()},
                )
            await asyncio.sleep(_POLL_S)

    def _start_failed(self, code: int) -> ManagedDbError:
        tail = self._startup_log_tail()
        if "Address already in use" in tail or "could not create any TCP/IP" in tail:
            return ManagedDbError(
                f"PostgreSQL cannot listen on {LISTEN_ADDRESS}:{self.port}: the "
                "port is in use by another program; choose another port",
                reason=ManagedDbReason.PORT_IN_USE,
                details={"port": self.port, "exit_code": code, "log_tail": tail},
            )
        return ManagedDbError(
            f"PostgreSQL exited during start-up (exit {code}); see "
            f"{self.layout.startup_log}",
            reason=ManagedDbReason.START_FAILED,
            details={"exit_code": code, "log_tail": tail},
        )

    def _startup_log_tail(self) -> str:
        """Tail of ``startup.log`` (stderr before the logging collector runs)
        plus the newest server log (where e.g. bind errors land once the
        collector started - it starts before the listen sockets are opened)."""
        parts = [
            _file_tail(path, start=self._log_marks.get(path, 0))
            for path in self._log_files()
        ]
        return "\n".join(part for part in parts if part)

    def _log_files(self) -> list[Path]:
        return [
            self.layout.startup_log,
            *sorted(self.layout.log_dir.glob("postgresql-*.log")),
        ]

    def _mark_logs(self) -> None:
        """Remember log sizes so failure tails show only this start's lines."""
        self._log_marks = {}
        for path in self._log_files():
            with contextlib.suppress(OSError):
                self._log_marks[path] = path.stat().st_size

    async def _ensure_database(self) -> None:
        import asyncpg  # controller extra; imported late like the store does

        password = self._require_password()
        conn = await asyncpg.connect(
            host=str(self.layout.socket_dir),
            port=self.port,
            user=self.superuser,
            password=password.get_secret_value(),
            database="postgres",
            timeout=10,
        )
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", self.database
            )
            if not exists:
                # Identifier validated by _IDENT in __init__.
                await conn.execute(f'CREATE DATABASE "{self.database}"')
                log.info("controller.db.managed.database_created", name=self.database)
        finally:
            await conn.close()

    # ------------------------------------------------------------------- stop
    async def stop(self, timeouts: StopTimeouts | None = None) -> StopOutcome:
        """Stop the postmaster (smart -> fast -> immediate -> kill) and release
        the lock. Idempotent."""
        try:
            if self._proc is None:
                return StopOutcome("already_exited", None, 0.0)
            outcome = await self._shutdown(timeouts or self.stop_timeouts)
            log.info(
                "controller.db.managed.stopped",
                mode=outcome.mode,
                exit_code=outcome.exit_code,
                duration_ms=round(outcome.duration_s * 1000),
            )
            return outcome
        finally:
            self._proc = None
            self._release_lock()

    async def _shutdown(self, timeouts: StopTimeouts) -> StopOutcome:
        proc = self._proc
        if proc is None:
            return StopOutcome("already_exited", None, 0.0)
        started = time.monotonic()
        if proc.returncode is not None or not pid_alive(proc.pid):
            # Exited already (crashed or killed); asyncio may not have reaped it.
            code = await proc.wait()
            return StopOutcome("already_exited", code, time.monotonic() - started)
        steps: Sequence[tuple[str, signal.Signals, float]] = (
            ("smart", signal.SIGTERM, timeouts.smart_s),
            ("fast", signal.SIGINT, timeouts.fast_s),
            ("immediate", signal.SIGQUIT, timeouts.immediate_s),
        )
        for mode, signum, timeout_s in steps:
            if timeout_s <= 0:
                continue
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(signum)
            with contextlib.suppress(TimeoutError):
                code = await asyncio.wait_for(proc.wait(), timeout_s)
                # The postmaster handles TERM/INT/QUIT and exits 0; death by a
                # signal means something else killed it before our request.
                return StopOutcome(
                    mode if code >= 0 else "already_exited",
                    code,
                    time.monotonic() - started,
                )
        # Last resort: the whole group (postmaster is its leader; backends too).
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            code = await asyncio.wait_for(proc.wait(), timeouts.kill_s)
        except TimeoutError:
            raise ManagedDbError(
                f"PostgreSQL (pid {proc.pid}) did not exit even after SIGKILL",
                reason=ManagedDbReason.STOP_FAILED,
                details={"pid": proc.pid},
            ) from None
        log.error("controller.db.managed.killed", pid=proc.pid)
        return StopOutcome("kill", code, time.monotonic() - started)

    # --------------------------------------------------------- dump / restore
    @contextlib.asynccontextmanager
    async def _pgpass(self) -> AsyncIterator[Path]:
        """A one-use ``PGPASSFILE`` (0600, in the 0700 socket dir)."""
        if self._password is None or self.pid is None:
            raise ManagedDbError(
                "the managed database is not running",
                reason=ManagedDbReason.NOT_RUNNING,
            )
        path = self.layout.socket_dir / f".pgpass.{os.getpid()}.{id(self)}"
        atomic_write_bytes(
            path, pgpass_line(self.superuser, self._password).encode(), mode=0o600
        )
        try:
            yield path
        finally:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    async def dump(self, out_file: Path, *, database: str | None = None) -> float:
        """``pg_dump`` (custom format) to ``out_file`` atomically; returns seconds."""
        tmp = out_file.with_name(f".{out_file.name}.partial")
        # The dump is the whole database (tokens, encrypted secrets): create it
        # owner-only *before* pg_dump writes (it truncates and keeps the mode;
        # left to the umask it would be 0644/0664).
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        os.close(os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        argv = dump_argv(
            self.binaries,
            self.layout,
            port=self.port,
            user=self.superuser,
            database=database or self.database,
            out_file=tmp,
        )
        started = time.monotonic()
        async with self._pgpass() as passfile:
            code, output = await _run(
                argv, timeout_s=_TOOL_TIMEOUT_S, env=child_env(pgpassfile=passfile)
            )
        if code != 0:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            raise ManagedDbError(
                f"pg_dump failed (exit {code}): {_tail(output)}",
                reason=ManagedDbReason.DUMP_FAILED,
                details={"exit_code": code, "output_tail": _tail(output)},
            )
        os.replace(tmp, out_file)
        return time.monotonic() - started

    async def create_database(self, name: str) -> None:
        """``CREATE DATABASE`` (for a restore target)."""
        await self._admin_sql(name, 'CREATE DATABASE "{name}"')

    async def drop_database(self, name: str) -> None:
        """``DROP DATABASE IF EXISTS ... WITH (FORCE)``."""
        await self._admin_sql(name, 'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    async def _admin_sql(self, name: str, template: str) -> None:
        import asyncpg

        if not _IDENT.match(name):
            raise ValueError(f"database must be a lower-case identifier: {name!r}")
        password = self._require_password()
        conn = await asyncpg.connect(
            host=str(self.layout.socket_dir),
            port=self.port,
            user=self.superuser,
            password=password.get_secret_value(),
            database="postgres",
            timeout=10,
        )
        try:
            await conn.execute(template.format(name=name))
        finally:
            await conn.close()

    async def restore(self, dump_file: Path, *, database: str) -> float:
        """``pg_restore`` into the existing, empty ``database``; returns seconds.

        All-or-nothing (one transaction): on failure the target stays empty.
        """
        argv = restore_argv(
            self.binaries,
            self.layout,
            port=self.port,
            user=self.superuser,
            database=database,
            dump_file=dump_file,
        )
        started = time.monotonic()
        async with self._pgpass() as passfile:
            code, output = await _run(
                argv, timeout_s=_TOOL_TIMEOUT_S, env=child_env(pgpassfile=passfile)
            )
        if code != 0:
            raise ManagedDbError(
                f"pg_restore failed (exit {code}): {_tail(output)}",
                reason=ManagedDbReason.RESTORE_FAILED,
                details={"exit_code": code, "output_tail": _tail(output)},
            )
        return time.monotonic() - started


# ------------------------------------------------------------------ helpers
async def _run(
    argv: Sequence[str], *, timeout_s: float, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run a bundled program to completion; ``(exit code, stdout+stderr)``.

    A timeout kills it and reports exit ``-9``.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env or child_env(),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return -9, f"timed out after {timeout_s:g}s"
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    code = proc.returncode if proc.returncode is not None else await proc.wait()
    return code, out.decode("utf-8", errors="replace")


def _file_tail(path: Path, *, start: int = 0) -> str:
    """Last bytes of ``path`` written after offset ``start``."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            if end < start:  # truncated (log rotation): read from the top
                start = 0
            fh.seek(max(start, end - _LOG_TAIL_BYTES))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _tail(text: str, limit: int = 600) -> str:
    return text.strip()[-limit:]


def _fsync_dir(directory: Path) -> None:
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
