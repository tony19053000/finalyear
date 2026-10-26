"""V0.17 G2: the packaged controller's first settings file (setup init-config)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meshforge.controller.settings import ControllerSettings, DbMode
from meshforge.controller.setup import config_file
from meshforge.controller.setup.config_file import (
    LOOPBACK,
    default_config_text,
    write_default_config,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("address", ["192.168.1.10", LOOPBACK])
def test_written_file_loads_as_valid_settings(
    tmp_path: Path, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "etc" / "controller.toml"
    written, chosen = write_default_config(path, address)
    assert written and chosen == address
    if os.name != "nt":  # POSIX file modes
        assert path.stat().st_mode & 0o777 == 0o644
    monkeypatch.setenv("MESHFORGE_INSTALL_MODE", "system")
    settings = ControllerSettings.load(path, data_dir=tmp_path / "state")
    assert settings.controller.grpc_bind == f"{address}:47100"
    assert settings.controller.http_bind == f"{address}:47110"
    assert settings.controller.enroll_bind == f"{address}:47101"
    assert settings.controller.db.mode is DbMode.MANAGED
    assert address in settings.server_cert_hosts
    assert ("LAN address" in default_config_text(address)) is (address == LOOPBACK)


def test_never_overwrites(tmp_path: Path) -> None:
    path = tmp_path / "controller.toml"
    path.write_text("# mine\n")
    written, _ = write_default_config(path, "192.168.1.10")
    assert not written and path.read_text() == "# mine\n"


def test_detection_falls_back_to_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config_file, "detect_lan_ipv4", lambda: None)
    written, chosen = write_default_config(tmp_path / "c.toml")
    assert written and chosen == LOOPBACK


def test_detect_returns_a_bindable_lan_address_or_none() -> None:
    found = config_file.detect_lan_ipv4()
    assert found is None or not found.startswith("127.")
