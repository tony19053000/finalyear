"""V0.3 G3 (0.3.8/0.3.9): ``AgentSettings`` validation and layout."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from meshforge import __version__
from meshforge.agent.settings import (
    DEFAULT_SERVICE_TYPE,
    AgentSection,
    AgentSettings,
    DiscoverySettings,
    ReconnectSettings,
    parse_controller_address,
)
from meshforge.common.errors import ConfigError, FailureType

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def test_defaults_match_adr_0002_updates() -> None:
    s = AgentSettings.load()
    a = s.agent
    assert a.controller_address == ""
    assert a.uses_discovery is True
    assert (a.reconnect.base_s, a.reconnect.cap_s, a.reconnect.reset_after_s) == (
        0.5,
        5.0,  # D27 (criterion 2); was 15.0
        30.0,
    )
    assert (a.keepalive.time_ms, a.keepalive.timeout_ms) == (10_000, 5_000)
    assert a.replay_ring_size == 10_000
    assert a.discovery.enabled is True
    assert a.discovery.timeout_s == 5.0
    assert a.discovery.service_type == DEFAULT_SERVICE_TYPE
    assert a.hello_timeout_s == 3.0
    assert a.agent_version == __version__


def test_identity_dir_is_under_data_dir(tmp_path: Path) -> None:
    s = AgentSettings.load(data_dir=tmp_path / "mf")
    assert s.identity_dir == tmp_path / "mf" / "identity"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("192.168.1.20:47100", ("192.168.1.20", 47100)),
        (" controller.lan:1 ", ("controller.lan", 1)),
        ("[::1]:47100", ("::1", 47100)),
    ],
)
def test_parse_controller_address_ok(text: str, expected: tuple[str, int]) -> None:
    assert parse_controller_address(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "nohost",
        ":47100",
        "host:",
        "host:abc",
        "host:0",
        "host:70000",
        "::1:47100",
        "[::1]47100",
    ],
)
def test_parse_controller_address_rejects(text: str) -> None:
    with pytest.raises(ConfigError) as info:
        parse_controller_address(text)
    assert info.value.failure_type is FailureType.CONFIG_ERROR


def test_bad_controller_address_is_a_config_error_from_settings() -> None:
    with pytest.raises(ConfigError) as info:
        AgentSettings.load(agent={"controller_address": "nope"})
    assert "controller_address" in str(info.value)


def test_env_override_sets_controller_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHFORGE_AGENT__CONTROLLER_ADDRESS", "10.0.0.5:47100")
    s = AgentSettings.load()
    assert s.agent.controller_address == "10.0.0.5:47100"
    assert s.agent.uses_discovery is False


def test_toml_section_is_read(tmp_path: Path) -> None:
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        '[agent]\ncontroller_address = "10.0.0.7:47100"\nreplay_ring_size = 5\n'
        "[agent.reconnect]\ncap_s = 3.0\nbase_s = 0.25\n",
        encoding="utf-8",
    )
    s = AgentSettings.load(cfg)
    assert s.agent.controller_address == "10.0.0.7:47100"
    assert s.agent.replay_ring_size == 5
    assert s.agent.reconnect.cap_s == 3.0


def test_unknown_key_in_section_is_reported() -> None:
    with pytest.raises(ConfigError):
        AgentSettings.load(agent={"controler_address": "x:1"})


def test_no_address_and_discovery_off_is_rejected() -> None:
    with pytest.raises(ConfigError) as info:
        AgentSettings.load(agent={"discovery": {"enabled": False}})
    assert "no way to find a controller" in str(info.value)


def test_reconnect_cap_below_base_rejected() -> None:
    with pytest.raises(ValueError, match="cap_s"):
        ReconnectSettings(base_s=2.0, cap_s=1.0)


@pytest.mark.parametrize(
    "bad", ["", "meshforge._tcp.local.", "_x._udp.local.", "_x._tcp.local"]
)
def test_service_type_shape(bad: str) -> None:
    with pytest.raises(ValueError, match="service_type"):
        DiscoverySettings(service_type=bad)


def test_agent_version_bounded() -> None:
    with pytest.raises(ValueError, match="agent_version"):
        AgentSection(agent_version="x" * 65)
    with pytest.raises(ValueError, match="agent_version"):
        AgentSection(agent_version="")


def test_hello_timeout_plus_backoff_cap_is_bounded_by_criterion_2() -> None:
    # D29: a dead-path attempt (hello_timeout_s) followed by the longest sleep
    # (cap_s) must fit under criterion 2's 10 s with room for the real connect.
    shipped = AgentSection()
    assert shipped.reconnect.cap_s + shipped.hello_timeout_s <= 8.0
    AgentSection(hello_timeout_s=3.0, reconnect={"cap_s": 5.0})  # exactly the budget
    with pytest.raises(ValueError, match="criterion 2"):
        AgentSection(hello_timeout_s=3.5, reconnect={"cap_s": 5.0})
    with pytest.raises(ValueError, match="criterion 2"):
        AgentSection(hello_timeout_s=8.0, reconnect={"cap_s": 0.5})
