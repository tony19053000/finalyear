"""V0.7 G1 (D134): the controller start-up loads the fabric CA and issues its
server certificate - against a real PostgreSQL, in-process and via ``run_controller``.

A CA that exists but cannot be trusted stops the start (``config_error``) before
any listener opens; no CA at all is still allowed in G1 (TLS is G3).
"""

from __future__ import annotations

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


def _settings(database: DatabaseSettings, data_dir: Path) -> ControllerSettings:
    return ControllerSettings(
        data_dir=data_dir, controller=controller_section(), database=database
    )


@pytest.mark.asyncio
async def test_start_issues_then_keeps_the_server_cert(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    fabric = create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.fabric is not None and runtime.server_cert is not None
        assert runtime.fabric.fabric_id == fabric.fabric_id
        assert runtime.server_cert.action is ServerCertAction.ISSUED
        first = runtime.server_cert.cert
        pki.verify_issued_by(first, fabric.cert)
        assert pki.cert_sans(first) == [pki.SERVER_NAME, "127.0.0.1"]
        assert runtime.grpc_port > 0
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.server_cert is not None
        assert runtime.server_cert.action is ServerCertAction.KEPT
        assert runtime.server_cert.cert == first


def test_start_without_a_fabric_is_refused_in_mtls_mode(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    """G1 allowed it; since V0.7 G3 an mtls controller needs its CA."""
    settings = _settings(pg_settings, tmp_path)
    assert run_controller(settings) == ExitCode.CONFIG_ERROR
    assert not settings.tls_dir.exists()


@pytest.mark.asyncio
async def test_start_without_a_fabric_is_allowed_in_insecure_dev(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = ControllerSettings(
        data_dir=tmp_path,
        security={"mode": "insecure-dev"},
        controller=controller_section(),
        database=pg_settings,
    )
    async with ControllerRuntime.start(settings) as runtime:
        assert runtime.fabric is None and runtime.server_cert is None
    assert not settings.tls_dir.exists()


def test_broken_ca_stops_the_start_with_config_error(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = _settings(pg_settings, tmp_path)
    create_fabric(settings.ca_dir)
    (settings.ca_dir / "ca.key").write_bytes(b"not a key")
    assert run_controller(settings) == ExitCode.CONFIG_ERROR
    assert not settings.tls_dir.exists()
