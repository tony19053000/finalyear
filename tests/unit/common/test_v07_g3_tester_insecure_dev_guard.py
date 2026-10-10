"""V0.7 G3 tester: trying to get ``insecure-dev`` (plaintext, claimed identity)
onto a reachable address.

Criterion 3 / D139: insecure-dev must be impossible with anything another
machine could reach. Each spelling below is a way an operator (or a hostile
config snippet) might smuggle a reachable address past a loopback check:
IPv4-mapped IPv6, wildcard forms, URL userinfo tricks, look-alike host names.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from meshforge.agent.settings import AgentSection, AgentSettings
from meshforge.cli.settings import CliSettings
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


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",  # noqa: S104
        "::",
        "[::]",
        "",
        " ",
        "*",
        "192.168.1.5",
        "10.0.0.1",
        "8.8.8.8",
        "::ffff:192.168.1.5",
        "::ffff:8.8.8.8",
        "[::ffff:10.0.0.1]",
        "fe80::1",
        "fe80::1%eth0",
        "127.0.0.1.nip.io",
        "localhost.evil.com",
        "evil.localhost.com",
        "127.0.0.1.",
        "localhost.",
        "127.1",
        "0x7f.0.0.1",
        "2130706433",
        "0177.0.0.1",
        "127.0.0.1/8",
        "127.0.0.1@evil.com",
        "evil.com@127.0.0.1",
        "::1.2.3.4",
        "::2",
        "1::1",
        "0:0:0:0:0:0:0:2",
        "127.0.0.1\n192.168.1.5",
        "127.0.0.1,192.168.1.5",
        "127.0.0.1 192.168.1.5",
    ],
)
def test_nothing_reachable_passes_as_loopback(host: str) -> None:
    assert is_loopback_host(host) is False, host


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.255.255.254",
        "::1",
        "[::1]",
        "localhost",
        "LOCALHOST",
        "::ffff:127.0.0.1",
    ],
)
def test_real_loopback_forms_pass(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "bind",
    [
        "0.0.0.0:47100",
        "[::]:47100",
        "192.168.1.5:47100",
        "[::ffff:192.168.1.5]:47100",
        "[::ffff:8.8.8.8]:47100",
        "[fe80::1]:47100",
        "127.0.0.1.nip.io:47100",
        "localhost.evil.com:47100",
        ":47100",
        "[::1]@evil.com:47100",
    ],
)
@pytest.mark.parametrize("which", ["grpc_bind", "http_bind"])
def test_controller_never_binds_plaintext_to_a_reachable_address(
    which: str, bind: str
) -> None:
    with pytest.raises(Exception, match=r".") as info:  # ConfigError or ValidationError
        ControllerSettings(security=DEV, controller=ControllerSection(**{which: bind}))
    assert "insecure-dev" in str(info.value) or "bind" in str(info.value).lower()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:47110@evil.com:80",
        "http://127.0.0.1:x@evil.com",
        "https://127.0.0.1:1@evil.com:2",
        "https://evil.com/127.0.0.1:1",
        "https://evil.com:47110/@127.0.0.1:1",
        "https://evil.com\\@127.0.0.1:47110",
        "https://192.168.1.5:47110",
        "https://[::ffff:192.168.1.5]:47110",
        "https://127.0.0.1.nip.io:47110",
    ],
)
def test_advertised_url_cannot_point_elsewhere(url: str) -> None:
    with pytest.raises(Exception):  # noqa: B017 - ConfigError/ValidationError
        ControllerSettings(
            security=DEV, controller=ControllerSection(api={"advertise_url": url})
        )


@pytest.mark.parametrize(
    "address",
    [
        "evil.com:47100",
        "127.0.0.1@evil.com:47100",
        "localhost.evil.com:47100",
        "[::ffff:10.0.0.1]:47100",
        "0.0.0.0:47100",
        "[::]:47100",
        "192.168.1.5:47100",
        "",
    ],
)
def test_agent_never_dials_a_reachable_controller_in_plaintext(address: str) -> None:
    with pytest.raises(Exception):  # noqa: B017
        AgentSettings(security=DEV, agent=AgentSection(controller_address=address))


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.com\\@127.0.0.1:47110",  # parser differential candidates
        "http://evil.com%2f@127.0.0.1:47110",
        "http://evil.com:80@127.0.0.1:47110",
    ],
)
def test_cli_url_host_is_what_the_http_client_dials(url: str) -> None:
    """The host checked by the guard (``urlsplit``) must be the one ``httpx``
    connects to - otherwise a loopback-looking URL could dial elsewhere."""
    checked = urlsplit(url).hostname
    try:
        dialled = httpx.URL(url).host
    except httpx.InvalidURL:
        return  # the client refuses it: nothing to dial
    assert dialled == checked, (checked, dialled)


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.1.5:47110",
        "http://evil.com:47110",
        "http://[::ffff:10.0.0.1]:47110",
    ],
)
def test_cli_insecure_dev_refuses_remote_urls(url: str) -> None:
    with pytest.raises(Exception):  # noqa: B017
        CliSettings(security=DEV, cli={"controller_url": url})
