"""V0.7 G1 tester: controller start-up against the fabric files (real PostgreSQL).

Fails closed on an untrustworthy CA (expired, loose key), heals a doubtful server
cert, and never opens a listener or touches ``tls/`` when it refuses. Brief §4 item 19.
"""

from __future__ import annotations

import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_controller_harness import controller_section

from meshforge.common import pki
from meshforge.common.errors import ExitCode
from meshforge.controller.fabric_ca import ServerCertAction, create_fabric
from meshforge.controller.server import ControllerRuntime, run_controller
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

posix_only = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX modes")


def _settings(database: DatabaseSettings, data_dir: Path) -> ControllerSettings:
    return ControllerSettings(
        data_dir=data_dir, controller=controller_section(), database=database
    )


def test_expired_ca_stops_the_start_and_issues_nothing(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    past = datetime.now(UTC) - timedelta(days=400)
    create_fabric(settings.ca_dir, now=past, lifetime=timedelta(days=30))
    assert run_controller(settings) == ExitCode.CONFIG_ERROR
    assert not settings.tls_dir.exists()


@posix_only
def test_world_readable_ca_key_stops_the_start(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    create_fabric(settings.ca_dir)
    (settings.ca_dir / "ca.key").chmod(0o644)
    assert run_controller(settings) == ExitCode.CONFIG_ERROR
    assert not settings.tls_dir.exists()


@posix_only
@pytest.mark.asyncio
async def test_start_heals_a_leaked_server_key_and_a_san_change(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.server_cert is not None
        first = runtime.server_cert.cert
    (settings.tls_dir / "server.key").chmod(0o644)
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.server_cert is not None
        assert runtime.server_cert.action is ServerCertAction.REISSUED
        assert runtime.server_cert.cert != first
    assert stat.S_IMODE((settings.tls_dir / "server.key").stat().st_mode) == 0o600
    # a configured extra SAN is picked up at the next start
    changed = ControllerSettings(
        data_dir=tmp_path,
        controller=controller_section(tls={"extra_sans": ["controller.lan"]}),
        database=pg_settings,
    )
    async with ControllerRuntime.start(changed) as runtime:
        assert runtime.server_cert is not None
        assert runtime.server_cert.action is ServerCertAction.REISSUED
        assert "controller.lan" in pki.cert_sans(runtime.server_cert.cert)
