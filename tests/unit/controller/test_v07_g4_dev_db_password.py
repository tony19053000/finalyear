"""V0.7 G4, D145 (d): a controller outside ``insecure-dev`` refuses to start with
the published development database password; dev/test paths are unaffected.

The check runs first thing at start-up (before any file, socket or database is
touched), so these tests need no PostgreSQL: a refused start never connects.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from pydantic import SecretStr

from meshforge.common.errors import ConfigError, ExitCode
from meshforge.controller.server import ControllerRuntime, run_controller
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DEV_DB_PASSWORD, DatabaseSettings

pytestmark = [pytest.mark.unit, pytest.mark.security]


def _settings(
    tmp_path: Path, *, mode: str = "mtls", **db: object
) -> ControllerSettings:
    return ControllerSettings(
        data_dir=tmp_path,
        security={"mode": mode},
        database=DatabaseSettings(**db),  # type: ignore[arg-type]
        controller={"http_bind": "127.0.0.1:0", "grpc_bind": "127.0.0.1:0"},
    )


@pytest.mark.parametrize(
    "db",
    [
        {},  # the default
        {"password": SecretStr(DEV_DB_PASSWORD)},
        {"url": SecretStr(f"postgresql://meshforge:{DEV_DB_PASSWORD}@db:5432/mf")},
        {"url": SecretStr("postgresql://meshforge:meshforge%2Ddev@db:5432/mf")},
    ],
)
def test_dev_password_is_detected(tmp_path: Path, db: dict[str, object]) -> None:
    settings = _settings(tmp_path, **db)
    assert settings.database.uses_dev_password()
    with pytest.raises(ConfigError) as caught:
        settings.check_database_password()
    assert "MESHFORGE_DATABASE__PASSWORD" in caught.value.message
    assert caught.value.details["reason"] == "dev_db_password"


@pytest.mark.parametrize(
    "db",
    [
        {"password": SecretStr("a-strong-one")},
        {"url": SecretStr("postgresql://meshforge:s3cret@db:5432/mf")},
        {"url": SecretStr("postgresql://meshforge@db:5432/mf")},  # no password
        {"password": SecretStr(DEV_DB_PASSWORD + "x")},
    ],
)
def test_other_passwords_pass(tmp_path: Path, db: dict[str, object]) -> None:
    settings = _settings(tmp_path, **db)
    assert not settings.database.uses_dev_password()
    settings.check_database_password()


def test_insecure_dev_keeps_the_dev_default(tmp_path: Path) -> None:
    _settings(tmp_path, mode="insecure-dev").check_database_password()


@pytest.mark.asyncio
async def test_the_runtime_refuses_to_start_before_touching_anything(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError):
        async with ControllerRuntime.start(_settings(tmp_path)):
            pytest.fail("the controller started")  # pragma: no cover
    # Nothing was created: not even the fabric token file.
    assert not (tmp_path / "controller").exists()


def test_the_process_exits_config_error(tmp_path: Path) -> None:
    assert run_controller(_settings(tmp_path)) == ExitCode.CONFIG_ERROR
