"""V0.17 G2 (D390, Q11, Q15, Q18, Q22): ``[controller.db]`` and the managed runtime glue.

Pure: settings validation, where the binaries come from, the CLI's connection,
the controller CLI's re-execution as the service user, the pins file agreeing
with the code's pin.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from meshforge.common.errors import ConfigError
from meshforge.common.private_files import PrivateFileError
from meshforge.common.system_cli import decide, first_command
from meshforge.controller.db_managed import LINUX_X86_64, ManagedDbError
from meshforge.controller.db_managed.runtime import (
    cluster_for,
    pg_bin_dir,
    tool_database_settings,
)
from meshforge.controller.settings import (
    DEFAULT_MANAGED_DB_PORT,
    ControllerSettings,
    DbMode,
)
from meshforge.controller.store.engine import connect_args
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.controller.system_install import CONTROLLER_PROGRAM

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]


def _settings(tmp_path: Path, **db: object) -> ControllerSettings:
    return ControllerSettings(
        data_dir=tmp_path, controller={"db": db}, database=DatabaseSettings()
    )


# ---------------------------------------------------------------- settings
def test_mode_defaults_follow_the_install_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("MESHFORGE_INSTALL_MODE", raising=False)
    assert _settings(tmp_path).controller.db.mode is DbMode.EXTERNAL
    monkeypatch.setenv("MESHFORGE_INSTALL_MODE", "system")
    assert _settings(tmp_path).controller.db.mode is DbMode.MANAGED


def test_managed_defaults(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="managed")
    db = settings.controller.db
    assert db.port == DEFAULT_MANAGED_DB_PORT == 47150  # Q22, never 5432
    assert settings.managed_db
    assert settings.db_root == tmp_path / "db"
    assert db.backups_keep == 3
    assert db.health_timeout_s == 60


@pytest.mark.parametrize("field", ["url", "password", "host", "port", "user", "name"])
def test_managed_refuses_a_database_connection_it_would_ignore(
    tmp_path: Path, field: str
) -> None:
    values: dict[str, object] = {
        "url": SecretStr("postgresql://u:p@h/db"),
        "password": SecretStr("x"),
        "host": "10.0.0.1",
        "port": 5433,
        "user": "someone",
        "name": "other",
    }
    with pytest.raises(ValueError, match="would be ignored"):
        ControllerSettings(
            data_dir=tmp_path,
            controller={"db": {"mode": "managed"}},
            database=DatabaseSettings(**{field: values[field]}),
        )


def test_managed_keeps_pool_settings(tmp_path: Path) -> None:
    settings = ControllerSettings(
        data_dir=tmp_path,
        controller={"db": {"mode": "managed"}},
        database=DatabaseSettings(pool_size=3, command_timeout_s=12),
    )
    assert settings.database.pool_size == 3


def test_managed_mode_skips_the_dev_password_check(tmp_path: Path) -> None:
    _settings(tmp_path, mode="managed").check_database_password()  # no raise
    with pytest.raises(ConfigError):
        _settings(tmp_path, mode="external").check_database_password()


@pytest.mark.parametrize("field", ["root", "socket_dir", "bin_dir"])
def test_db_paths_must_be_absolute(tmp_path: Path, field: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        _settings(tmp_path, mode="managed", **{field: "relative/dir"})


@pytest.mark.parametrize("port", [0, 65536])
def test_db_port_range(tmp_path: Path, port: int) -> None:
    with pytest.raises(ValueError):
        _settings(tmp_path, port=port)


def test_session_role_becomes_a_startup_setting() -> None:
    plain = connect_args(DatabaseSettings())
    assert "role" not in plain["server_settings"]
    acting = connect_args(DatabaseSettings(session_role="meshforge"))
    assert acting["server_settings"]["role"] == "meshforge"


@pytest.mark.parametrize("bad", ["Meshforge", "a-b", "x;drop", ""])
def test_session_role_is_an_identifier(bad: str) -> None:
    with pytest.raises(ValueError):
        DatabaseSettings(session_role=bad)


# ---------------------------------------------------------------- runtime
def test_bin_dir_from_setting_then_bundle_else_config_error(tmp_path: Path) -> None:
    explicit = tmp_path / "pg" / "bin"
    assert pg_bin_dir(_settings(tmp_path, bin_dir=explicit)) == explicit
    bundle = tmp_path / "bundle"
    assert pg_bin_dir(_settings(tmp_path), bundle=bundle) == bundle / "pgsql" / "bin"
    if sys.executable and (Path(sys.executable).parents[1] / "BUILDINFO.json").exists():
        pytest.skip("running from a bundle")
    with pytest.raises(ConfigError, match="bin_dir"):
        pg_bin_dir(_settings(tmp_path, mode="managed"))


def test_cluster_for_uses_the_settings(tmp_path: Path) -> None:
    sock = tmp_path / "s"
    cluster = cluster_for(
        _settings(tmp_path, mode="managed", port=47999, socket_dir=sock),
        bundle=tmp_path / "b",
    )
    assert cluster.port == 47999
    assert cluster.layout.root == tmp_path / "db"
    assert cluster.layout.socket_dir == sock
    assert cluster.binaries.bin_dir == tmp_path / "b" / "pgsql" / "bin"
    assert (cluster.superuser, cluster.app_user) == ("meshforge_admin", "meshforge")


def test_tool_settings_external_mode_is_unchanged(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="external")
    assert tool_database_settings(settings) is settings.database


def test_tool_settings_managed_reads_the_app_password(tmp_path: Path) -> None:
    settings = _settings(tmp_path, mode="managed", port=47151)
    with pytest.raises(ManagedDbError, match="start the controller"):
        tool_database_settings(settings)
    root = tmp_path / "db"
    root.mkdir(mode=0o700)
    path = root / "app.password"
    path.write_text("s3cret-value\n")
    path.chmod(0o600)
    tool = tool_database_settings(settings)
    assert (tool.host, tool.port, tool.user, tool.name) == (
        "127.0.0.1",
        47151,
        "meshforge",
        "meshforge",
    )
    assert tool.password.get_secret_value() == "s3cret-value"
    assert tool.session_role is None
    assert "s3cret" not in repr(tool)
    path.chmod(0o644)  # a password others can read is refused, not used
    with pytest.raises(PrivateFileError):
        tool_database_settings(settings)


# ---------------------------------------------------------------- pins
def test_pins_file_matches_the_code_pin() -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import pg_bundle
        from bundle_lib import load_pins

        pin = pg_bundle.pg_pin(load_pins(REPO / "packaging" / "pins.toml"))
    finally:
        sys.path.remove(str(REPO / "scripts"))
    assert pin.sha256 == LINUX_X86_64.sha256
    assert pin.archive_root == LINUX_X86_64.archive_root
    assert LINUX_X86_64.url in pin.urls
    assert pin.urls[0] != LINUX_X86_64.url  # Q19: our mirror first
    assert pin.version == LINUX_X86_64.version


# ---------------------------------------------------------------- CLI re-exec
def _decide(args: list[str], **over: object) -> str:
    kw: dict[str, object] = {
        "program": CONTROLLER_PROGRAM,
        "environ": {},
        "platform": "linux",
        "user": "asha",
        "installed": True,
        "bundled": True,
    }
    kw.update(over)
    return decide(args, **kw).action  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "args",
    [
        ["user", "create", "admin", "--admin"],
        ["--config", "/x.toml", "db", "backup"],
        ["fabric", "invite"],
        ["doctor", "--json"],
        ["db", "status"],
    ],
)
def test_controller_admin_commands_reexecute(args: list[str]) -> None:
    assert _decide(args) == "reexec"
    decision = decide(
        args,
        program=CONTROLLER_PROGRAM,
        environ={},
        platform="linux",
        user="root",
        installed=True,
        bundled=True,
    )
    assert decision.argv[:6] == (
        "sudo",
        "-u",
        "meshforge",
        "--",
        "/usr/bin/env",
        "MESHFORGE_INSTALL_MODE=system",
    )
    assert decision.argv[6] == "MESHFORGE_CONFIG=/etc/meshforge/controller.toml"
    assert decision.argv[7] == (
        "/opt/meshforge/controller/current/bin/meshforge-controller"
    )
    assert list(decision.argv[8:]) == args


@pytest.mark.parametrize(
    "args",
    [[], ["--config", "x.toml"], ["--allow-any-bind"], ["--version"], ["db", "--help"]],
)
def test_controller_server_and_harmless_commands_run_in_place(
    args: list[str],
) -> None:
    assert _decide(args) == "none"


def test_controller_service_user_and_unit_run_in_place() -> None:
    assert _decide(["db", "backup"], user="meshforge") == "none"
    assert (
        _decide(["db", "backup"], environ={"MESHFORGE_INSTALL_MODE": "system"})
        == "none"
    )


def test_first_command_skips_options_and_their_values() -> None:
    opts = frozenset({"--config"})
    assert first_command(["--config", "user", "db"], opts) == "db"
    assert first_command(["--json", "doctor"], opts) == "doctor"
    # V0.17 G4 fix round 1 (G4-T1): after `--` the next argument is the command
    assert first_command(["--", "db"], opts) == "db"
    assert first_command(["--"], opts) is None
    assert first_command([], opts) is None
