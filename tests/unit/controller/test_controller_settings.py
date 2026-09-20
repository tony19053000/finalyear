"""V0.3 G2 (0.3.6): ``[controller]`` settings - defaults, bind rules, liveness mirror.

No SQLAlchemy / gRPC needed: the settings module must import on a base install.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from meshforge.common.errors import ConfigError
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.settings import (
    DEFAULT_GRPC_PORT,
    DEFAULT_HTTP_PORT,
    ControllerSection,
    ControllerSettings,
    LivenessSettings,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def test_defaults_are_loopback_and_documented_ports() -> None:
    s = ControllerSettings.load()
    c = s.controller
    assert (DEFAULT_GRPC_PORT, DEFAULT_HTTP_PORT) == (47100, 47110)
    assert c.grpc_bind == "127.0.0.1:47100"
    assert c.http_bind == "127.0.0.1:47110"
    assert c.allow_any_bind is False
    assert c.accepted_protocol_versions == (1, 1)
    assert c.hello_timeout_s == 5.0
    assert c.shutdown_drain_s == 5.0
    assert c.outbound_queue_size == 1000
    assert c.dedupe_window == 10_000
    assert c.liveness.to_config() == LivenessConfig()  # 2 s / +0.5 s / x3
    assert s.database.command_timeout_s == 30.0  # 0.1.7 carry-over


def test_wildcard_bind_needs_allow_any() -> None:
    with pytest.raises(ConfigError, match="wildcard"):
        ControllerSettings.load(controller={"grpc_bind": "0.0.0.0:47100"})
    s = ControllerSettings.load(
        controller={"grpc_bind": "0.0.0.0:47100", "allow_any_bind": True}
    )
    assert s.controller.grpc_bind == "0.0.0.0:47100"


def test_public_http_bind_refused() -> None:
    with pytest.raises(ConfigError, match="non-private"):
        ControllerSettings.load(controller={"http_bind": "8.8.8.8:47110"})


def test_malformed_bind_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="ip:port"):
        ControllerSettings.load(controller={"grpc_bind": "localhost"})


def test_env_and_toml_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        "[controller]\n"
        'grpc_bind = "192.168.1.20:47100"\n'
        "[controller.liveness]\n"
        "heartbeat_interval_s = 1.0\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MESHFORGE_CONTROLLER__HTTP_BIND", "192.168.1.20:47110")
    monkeypatch.setenv("MESHFORGE_CONTROLLER__HELLO_TIMEOUT_S", "2.5")
    s = ControllerSettings.load(cfg)
    assert s.controller.grpc_bind == "192.168.1.20:47100"
    assert s.controller.http_bind == "192.168.1.20:47110"  # env beats file
    assert s.controller.hello_timeout_s == 2.5
    assert s.controller.liveness.heartbeat_interval_s == 1.0
    assert s.controller.liveness.to_config().offline_after_s == 3.0


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(ConfigError):
        ControllerSettings.load(controller={"grpc_port": 1})


@pytest.mark.parametrize("versions", [(0, 1), (2, 1), (1, 0)])
def test_protocol_version_range_validated(versions: tuple[int, int]) -> None:
    with pytest.raises(ValidationError):
        ControllerSection(accepted_protocol_versions=versions)


def test_accepts_protocol_version() -> None:
    c = ControllerSection(accepted_protocol_versions=(1, 2))
    assert [c.accepts_protocol_version(v) for v in (0, 1, 2, 3)] == [
        False,
        True,
        True,
        False,
    ]


def test_liveness_settings_mirror_config_and_reject_inconsistent_timing() -> None:
    ls = LivenessSettings(heartbeat_interval_s=0.2, miss_allowance_s=0.05)
    cfg = ls.to_config()
    assert cfg.suspect_after_s == pytest.approx(0.25)
    assert cfg.offline_after_s == pytest.approx(0.6)
    # lease_multiplier=1 with a miss allowance: OFFLINE would come before SUSPECT.
    with pytest.raises(ValidationError, match="offline_after_s"):
        LivenessSettings(
            heartbeat_interval_s=1.0, miss_allowance_s=0.5, lease_multiplier=1
        )
    with pytest.raises(ValidationError):
        LivenessSettings(heartbeat_interval_s=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("outbound_queue_size", 0),
        ("hello_timeout_s", 0),
        ("shutdown_drain_s", -1),
        ("dedupe_window", 0),
    ],
)
def test_bounds(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        ControllerSection(**{field: value})
