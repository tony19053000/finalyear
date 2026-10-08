"""V0.7 G3 deliverable 2 (D139): ``[security] mode``.

``mtls`` is the default for the controller, the agent and the CLI.
``insecure-dev`` (plaintext, the agent's claim trusted) is accepted only when
nothing can be reached from another machine: every controller listener and the
advertised URL loopback, no ``allow_any_bind``; the agent dials a loopback
controller (no mDNS) and serves peers on loopback. Each refusal is a typed
``ConfigError`` naming the setting (mutation-checked: dropping a branch fails
its test).
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.cli.settings import CliSettings
from meshforge.common.config import SecurityMode, SecuritySettings
from meshforge.common.errors import ConfigError
from meshforge.common.netaddr import is_loopback_host
from meshforge.controller.settings import ControllerSection, ControllerSettings

pytestmark = pytest.mark.unit

DEV = {"mode": "insecure-dev"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


def _controller(**section: Any) -> ControllerSettings:
    return ControllerSettings(security=DEV, controller=ControllerSection(**section))


def _agent(**section: Any) -> AgentSettings:
    return AgentSettings(security=DEV, agent=AgentSection(**section))


def test_mtls_is_the_default_everywhere() -> None:
    assert ControllerSettings().security.mode is SecurityMode.MTLS
    assert AgentSettings().security.mode is SecurityMode.MTLS
    assert CliSettings().security.mode is SecurityMode.MTLS
    assert SecuritySettings().insecure is False


def test_env_and_unknown_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESHFORGE_SECURITY__MODE", "insecure-dev")
    assert ControllerSettings().security.insecure is True
    monkeypatch.setenv("MESHFORGE_SECURITY__MODE", "off")
    with pytest.raises(ConfigError, match=r"security\.mode"):
        ControllerSettings()


def test_controller_insecure_dev_on_loopback_is_accepted() -> None:
    settings = _controller(
        grpc_bind="127.0.0.1:0", http_bind="[::1]:0", enroll={"bind": "127.0.0.2:0"}
    )
    assert settings.security.insecure


@pytest.mark.parametrize(
    ("section", "match"),
    [
        ({"grpc_bind": "192.168.1.5:47100"}, "grpc_bind"),
        ({"http_bind": "10.0.0.7:47110"}, "http_bind"),
        ({"enroll": {"bind": "192.168.1.5:47101"}}, "enroll.bind"),
        (
            {"grpc_bind": "0.0.0.0:47100", "allow_any_bind": True},
            "allow_any_bind",
        ),
        ({"api": {"advertise_url": "http://192.168.1.5:47110"}}, "advertise_url"),
    ],
)
def test_controller_insecure_dev_refuses_anything_reachable(
    section: dict[str, Any], match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        _controller(**section)
    # the same values are fine in mtls mode (the LAN rule still applies there)
    ControllerSettings(controller=ControllerSection(**section))


def test_controller_default_enroll_bind_follows_a_lan_grpc_host() -> None:
    # grpc on loopback, enroll bind defaults to <grpc host>:47101 = loopback too
    assert _controller(grpc_bind="127.0.0.1:47100").controller.enroll_bind == (
        "127.0.0.1:47101"
    )


def test_agent_insecure_dev_needs_a_loopback_controller_and_no_discovery() -> None:
    assert _agent(controller_address="127.0.0.1:47100").security.insecure
    assert _agent(controller_address="localhost:47100").security.insecure
    assert _agent(controller_address="[::1]:47100").security.insecure
    with pytest.raises(ConfigError, match="discovery is off"):
        _agent(controller_address="")
    with pytest.raises(ConfigError, match="loopback controller_address"):
        _agent(controller_address="192.168.1.5:47100")
    with pytest.raises(ConfigError, match="loopback controller_address"):
        _agent(controller_address="controller.lan:47100")


@pytest.mark.parametrize(
    ("peer", "match"),
    [
        ({"bind": "192.168.1.5"}, "bind"),
        ({"bind": "127.0.0.1", "advertise_host": "192.168.1.5"}, "advertise_host"),
        ({"bind": "0.0.0.0", "allow_any_bind": True}, "allow_any_bind"),  # noqa: S104
    ],
)
def test_agent_insecure_dev_peer_server_stays_on_loopback(
    peer: dict[str, Any], match: str
) -> None:
    with pytest.raises(ConfigError, match=match):
        _agent(controller_address="127.0.0.1:47100", peer=peer)
    assert _agent(controller_address="127.0.0.1:47100", peer={"bind": "127.0.0.1"})


@pytest.mark.parametrize(
    ("host", "loop"),
    [
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("[::1]", True),
        ("::ffff:127.0.0.1", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("192.168.1.5", False),
        ("0.0.0.0", False),  # noqa: S104
        ("::", False),
        ("localhost.evil.example", False),
        ("", False),
        ("fe80::1%eth0", False),
    ],
)
def test_is_loopback_host(host: str, loop: bool) -> None:
    assert is_loopback_host(host) is loop
