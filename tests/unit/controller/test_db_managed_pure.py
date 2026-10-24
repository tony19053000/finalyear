"""Spike SP2 (0.17.0): pure parts of the managed PostgreSQL prototype.

``postmaster.pid`` / ``PG_VERSION`` / ``--version`` parsing, the major-version
check, socket-path rules, command construction (no password on argv, loopback
only, scram everywhere, minimal env), the password file (owner-only, never
regenerated) and the layout. No PostgreSQL binaries needed.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from meshforge.common.errors import FailureType
from meshforge.common.private_files import PrivateFileError, PrivateFileReason
from meshforge.controller.db_managed import (
    BUNDLED_MAJOR,
    LINUX_X86_64,
    ClusterVersionMismatchError,
    ManagedCluster,
    ManagedDbError,
    ManagedDbReason,
    ManagedLayout,
    PgBinaries,
)
from meshforge.controller.db_managed.commands import (
    DEATH_SIGNAL,
    child_env,
    dump_argv,
    initdb_argv,
    postgres_argv,
    restore_argv,
    server_settings,
    with_death_signal,
)
from meshforge.controller.db_managed.layout import (
    MAX_SOCKET_PATH_BYTES,
    check_socket_dir,
    socket_path,
)
from meshforge.controller.db_managed.pidfile import (
    PidFileError,
    PostmasterStatus,
    check_cluster_major,
    parse_postmaster_pid,
    parse_server_version,
    read_cluster_major,
    read_postmaster_pid,
)
from meshforge.controller.db_managed.secret import (
    ensure_password_file,
    generate_password,
    pgpass_line,
    read_password_file,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")

READY_PIDFILE = (
    "12345\n/var/lib/meshforge/db/data\n1792838491\n5433\n/run/meshforge/db\n"
    "127.0.0.1\n  5433001         7\nready   \n"
)


# ------------------------------------------------------------- postmaster.pid
def test_parse_full_ready_pidfile() -> None:
    info = parse_postmaster_pid(READY_PIDFILE)
    assert info.pid == 12345
    assert info.data_dir == "/var/lib/meshforge/db/data"
    assert info.start_time == 1792838491
    assert info.port == 5433
    assert info.socket_dir == "/run/meshforge/db"
    assert info.listen_address == "127.0.0.1"
    assert info.shmem_key == "5433001         7"
    assert info.status is PostmasterStatus.READY


def test_parse_partial_pidfile_while_starting() -> None:
    info = parse_postmaster_pid("999\n/data\n")
    assert info.pid == 999
    assert info.port is None
    assert info.status is None


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("starting", PostmasterStatus.STARTING),
        ("stopping", PostmasterStatus.STOPPING),
        ("standby ", PostmasterStatus.STANDBY),
        ("bogus", None),
        ("", None),
    ],
)
def test_parse_status_line(status: str, expected: PostmasterStatus | None) -> None:
    text = "\n".join(["1", "/d", "0", "5432", "/s", "*", "k", status]) + "\n"
    assert parse_postmaster_pid(text).status is expected


@pytest.mark.parametrize(
    "text",
    ["", "\n", "abc\n/data\n", "0\n/data\n", "-55\n/data\n", "123\n", "123\n\n"],
)
def test_parse_rejects_invalid_pidfile(text: str) -> None:
    with pytest.raises(PidFileError):
        parse_postmaster_pid(text)


def test_parse_tolerates_garbage_numeric_fields() -> None:
    info = parse_postmaster_pid("7\n/d\nnot-a-time\nnot-a-port\n")
    assert (info.start_time, info.port) == (None, None)


def test_read_pidfile_missing_and_oversized(tmp_path: Path) -> None:
    assert read_postmaster_pid(tmp_path) is None
    (tmp_path / "postmaster.pid").write_bytes(b"1\n" + b"x" * 5000)
    with pytest.raises(PidFileError):
        read_postmaster_pid(tmp_path)
    (tmp_path / "postmaster.pid").write_text(READY_PIDFILE)
    info = read_postmaster_pid(tmp_path)
    assert info is not None and info.pid == 12345


# ------------------------------------------------------------------- versions
def test_parse_server_version() -> None:
    assert parse_server_version("postgres (PostgreSQL) 16.15\n") == ("16", "16.15")
    assert parse_server_version("postgres (PostgreSQL) 17beta1") == ("17", "17beta1")


@pytest.mark.parametrize(
    "output",
    [
        "",
        "postgres: error while loading shared libraries: libc.so.6: version `GLIBC_2.36'",
        "postgres (PostgreSQL) 9.6.24",
        "postgres (PostgreSQL) x.y",
    ],
)
def test_parse_server_version_rejects(output: str) -> None:
    with pytest.raises(ValueError, match=r"."):
        parse_server_version(output)


def test_cluster_major_check(tmp_path: Path) -> None:
    assert read_cluster_major(tmp_path) is None
    (tmp_path / "PG_VERSION").write_text("16\n")
    assert read_cluster_major(tmp_path) == "16"
    assert check_cluster_major("16", "16")
    assert check_cluster_major("16\n", " 16")
    assert not check_cluster_major("15", "16")
    assert not check_cluster_major("17", "16")


def test_version_mismatch_error_is_typed_and_clear() -> None:
    exc = ClusterVersionMismatchError(
        data_dir="/var/lib/meshforge/db/data", data_major="15", binary_major="16"
    )
    assert isinstance(exc, ManagedDbError)
    assert exc.reason is ManagedDbReason.VERSION_MISMATCH
    assert exc.failure_type is FailureType.CONFIG_ERROR
    assert not exc.retryable
    assert exc.details == {
        "reason": "version_mismatch",
        "data_dir": "/var/lib/meshforge/db/data",
        "data_major": "15",
        "binary_major": "16",
    }
    assert "PostgreSQL 15" in str(exc) and "PostgreSQL 16" in str(exc)
    assert "refusing" in str(exc)


def test_pins_are_consistent() -> None:
    assert LINUX_X86_64.version.split(".")[0] == BUNDLED_MAJOR
    assert len(LINUX_X86_64.sha256) == 64
    assert LINUX_X86_64.url.startswith("https://")
    assert LINUX_X86_64.url.endswith(LINUX_X86_64.archive_root + ".tar.gz")


def test_binaries_check_reports_missing_programs(tmp_path: Path) -> None:
    with pytest.raises(ManagedDbError) as info:
        PgBinaries(tmp_path).check()
    assert info.value.reason is ManagedDbReason.BINARIES_MISSING
    assert set(info.value.details["missing"]) == {
        "postgres",
        "initdb",
        "pg_dump",
        "pg_restore",
    }


# ----------------------------------------------------------------- socket dir
def test_socket_path_shape() -> None:
    assert socket_path(Path("/run/x"), 5433) == "/run/x/.s.PGSQL.5433"


def test_socket_dir_length_boundary() -> None:
    suffix = len("/.s.PGSQL.5433")
    fits = Path("/" + "a" * (MAX_SOCKET_PATH_BYTES - suffix - 1))
    check_socket_dir(fits, 5433)  # exactly 107 bytes
    too_long = Path("/" + "a" * (MAX_SOCKET_PATH_BYTES - suffix))
    with pytest.raises(ManagedDbError) as info:
        check_socket_dir(too_long, 5433)
    assert info.value.reason is ManagedDbReason.PATH_INVALID
    assert info.value.details["length"] == MAX_SOCKET_PATH_BYTES + 1


@pytest.mark.parametrize("bad", ["relative/dir", "/a,b", '/a"b'])
def test_socket_dir_rejects_unusable_paths(bad: str) -> None:
    with pytest.raises(ManagedDbError) as info:
        check_socket_dir(Path(bad), 5432)
    assert info.value.reason is ManagedDbReason.PATH_INVALID


def test_socket_dir_allows_spaces() -> None:
    check_socket_dir(Path("/home/u/FINAL YEAR/run"), 5432)


# ------------------------------------------------------------------- commands
BINS = PgBinaries(Path("/opt/meshforge/pg/bin"))
LAYOUT = ManagedLayout(Path("/var/lib/meshforge/db"))


def test_initdb_argv_scram_everywhere_and_password_from_file() -> None:
    argv = initdb_argv(
        BINS,
        data_dir=LAYOUT.initdb_tmp_dir,
        superuser="meshforge",
        password_file=LAYOUT.password_file,
    )
    assert argv[0] == "/opt/meshforge/pg/bin/initdb"
    assert "--auth-local=scram-sha-256" in argv
    assert "--auth-host=scram-sha-256" in argv
    assert f"--pwfile={LAYOUT.password_file}" in argv
    assert "--data-checksums" in argv
    assert "--locale=C" in argv and "--encoding=UTF8" in argv
    assert not any("trust" in a for a in argv)
    assert argv[argv.index("--pgdata") + 1] == str(LAYOUT.initdb_tmp_dir)


def test_postgres_argv_is_loopback_only_and_private_socket() -> None:
    argv = postgres_argv(BINS, LAYOUT, port=5433)
    assert argv[:3] == ["/opt/meshforge/pg/bin/postgres", "-D", str(LAYOUT.data_dir)]
    settings = dict(
        argv[i + 1].split("=", 1) for i in range(3, len(argv), 2) if argv[i] == "-c"
    )
    assert settings["listen_addresses"] == "127.0.0.1"
    assert settings["port"] == "5433"
    assert settings["unix_socket_directories"] == str(LAYOUT.socket_dir)
    assert settings["unix_socket_permissions"] == "0700"
    assert settings["timezone"] == "UTC"
    assert settings == server_settings(LAYOUT, port=5433)


def test_socket_dir_override() -> None:
    layout = ManagedLayout(Path("/var/lib/x"), socket_dir_override=Path("/run/mf"))
    assert layout.socket_dir == Path("/run/mf")
    assert server_settings(layout, port=1)["unix_socket_directories"] == "/run/mf"


def test_death_signal_wrapper_linux() -> None:
    argv = ["/opt/pg/bin/postgres", "-D", "/d"]
    wrapped = with_death_signal(argv, parent_pid=4242, python="/py", platform="linux")
    assert wrapped[:4] == ["/py", "-I", "-S", "-c"]
    shim = wrapped[4]
    assert "prctl(1, sig" in shim and "os.execv(exe, argv)" in shim
    assert "getppid() != ppid" in shim
    assert wrapped[5:] == ["4242", str(int(DEATH_SIGNAL)), *argv[:1], *argv]
    assert DEATH_SIGNAL.name == "SIGINT"  # fast shutdown, never SIGKILL


def test_death_signal_wrapper_elsewhere_is_identity() -> None:
    argv = ["postgres", "-D", "/d"]
    assert with_death_signal(argv, parent_pid=1, platform="win32") == argv
    assert with_death_signal(argv, parent_pid=1, platform="darwin") == argv


def test_child_env_inherits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGHOST", "evil")
    monkeypatch.setenv("PGPASSWORD", "leak")
    monkeypatch.setenv("PGDATA", "/elsewhere")
    env = child_env()
    assert set(env) == {"LC_ALL", "TZ", "PATH"}
    assert env["LC_ALL"] == "C" and env["TZ"] == "UTC"
    env = child_env(pgpassfile=Path("/run/x/.pgpass"))
    assert env["PGPASSFILE"] == "/run/x/.pgpass"
    assert "PGPASSWORD" not in env


def test_dump_and_restore_argv() -> None:
    dump = dump_argv(
        BINS,
        LAYOUT,
        port=5433,
        user="meshforge",
        database="meshforge",
        out_file=Path("/b/pre-0.17.dump"),
    )
    assert dump[0].endswith("/pg_dump")
    assert dump[dump.index("--host") + 1] == str(LAYOUT.socket_dir)
    assert "--no-password" in dump and "--format=custom" in dump
    assert "--file=/b/pre-0.17.dump" in dump and "--dbname=meshforge" in dump
    restore = restore_argv(
        BINS,
        LAYOUT,
        port=5433,
        user="meshforge",
        database="target",
        dump_file=Path("/b/x.dump"),
    )
    assert restore[0].endswith("/pg_restore")
    assert {"--no-password", "--exit-on-error", "--single-transaction"} <= set(restore)
    assert restore[-1] == "/b/x.dump" and "--dbname=target" in restore


# --------------------------------------------------------------------- secret
def test_generated_passwords_are_long_random_and_pgpass_safe() -> None:
    a, b = generate_password(), generate_password()
    assert a.get_secret_value() != b.get_secret_value()
    assert len(a.get_secret_value()) == 43
    assert not set(a.get_secret_value()) & {":", "\\", "\n", "@", "/"}
    assert a.get_secret_value() not in repr(a)


def test_pgpass_line_escapes() -> None:
    line = pgpass_line("meshforge", SecretStr("a:b\\c"))
    assert line == "*:*:*:meshforge:a\\:b\\\\c\n"


@posix_only
def test_password_file_created_owner_only_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "password"
    first, created = ensure_password_file(path)
    assert created
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == first.get_secret_value() + "\n"
    second, created_again = ensure_password_file(path)
    assert not created_again
    assert second.get_secret_value() == first.get_secret_value()


@posix_only
def test_password_file_refused_when_readable_by_others(tmp_path: Path) -> None:
    path = tmp_path / "password"
    path.write_text("hunter2\n")
    path.chmod(0o644)
    with pytest.raises(PrivateFileError) as info:
        ensure_password_file(path)
    assert info.value.reason is PrivateFileReason.NOT_PRIVATE
    assert "hunter2" not in str(info.value)


@posix_only
def test_password_file_refuses_symlink_and_empty(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_text("pw\n")
    real.chmod(0o600)
    link = tmp_path / "password"
    link.symlink_to(real)
    with pytest.raises(PrivateFileError):
        read_password_file(link)
    empty = tmp_path / "empty"
    empty.write_bytes(b"\n")
    empty.chmod(0o600)
    with pytest.raises(PrivateFileError):
        read_password_file(empty)
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff\xfe\n")
    binary.chmod(0o600)
    with pytest.raises(PrivateFileError):
        read_password_file(binary)


# --------------------------------------------------------------------- layout
def test_layout_paths() -> None:
    layout = ManagedLayout(Path("/var/lib/meshforge/db"))
    root = Path("/var/lib/meshforge/db")
    assert layout.data_dir == root / "data"
    assert layout.initdb_tmp_dir == root / "data.initdb-tmp"
    assert layout.password_file == root / "password"
    assert layout.lock_file == root / "managed.lock"
    assert layout.socket_dir == root / "run"
    assert layout.log_dir == root / "log"
    assert layout.startup_log == root / "log" / "startup.log"
    assert layout.backups_dir == root / "backups"


@posix_only
def test_layout_prepare_creates_and_tightens_dirs(tmp_path: Path) -> None:
    layout = ManagedLayout(tmp_path / "db")
    (tmp_path / "db" / "log").mkdir(parents=True)
    os.chmod(tmp_path / "db" / "log", 0o755)  # noqa: S103 - the loose mode under test
    layout.prepare()
    for path in (layout.root, layout.socket_dir, layout.log_dir, layout.backups_dir):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
    assert not layout.data_dir.exists()  # initdb creates it


# -------------------------------------------------------------------- cluster
@pytest.mark.parametrize(
    "kwargs",
    [
        {"superuser": "Bad-Name"},
        {"database": 'x"; DROP'},
        {"database": ""},
        {"port": 0},
        {"port": 70000},
    ],
)
def test_cluster_validates_identifiers_and_port(kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {"port": 5433, **kwargs}
    with pytest.raises(ValueError, match=r"."):
        ManagedCluster(BINS, LAYOUT, **args)  # type: ignore[arg-type]


def test_settings_before_start_is_typed_error() -> None:
    cluster = ManagedCluster(BINS, LAYOUT, port=5433)
    assert cluster.pid is None
    with pytest.raises(ManagedDbError) as info:
        cluster.database_settings()
    assert info.value.reason is ManagedDbReason.NOT_RUNNING


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc scan")
def test_proc_helpers_on_this_process(tmp_path: Path) -> None:
    from meshforge.controller.db_managed.procs import (
        data_dir_holders,
        is_postgres_of,
        pid_alive,
    )

    assert pid_alive(os.getpid())
    assert not is_postgres_of(os.getpid(), Path.cwd())  # python, not postgres
    assert data_dir_holders(tmp_path) == []
