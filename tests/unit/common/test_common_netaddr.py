"""V0.3 G2 (0.3.6): bind-address rules shared by every MeshForge listener.

Pure logic; nothing here touches this machine's interfaces (checklist item 2).
"""

from __future__ import annotations

import pytest

from meshforge.common.errors import ConfigError, ExitCode, FailureType
from meshforge.common.netaddr import (
    BindAddress,
    check_bind_address,
    is_lan_address,
    parse_bind,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "text",
    [
        "127.0.0.1",
        "10.0.0.7",
        "172.16.5.9",
        "192.168.1.20",
        "169.254.10.10",  # link-local
        "::1",
        "fd00::1",  # ULA
        "fe80::1",  # link-local v6
        "::ffff:192.168.1.20",  # v4-mapped private
    ],
)
def test_lan_addresses(text: str) -> None:
    assert is_lan_address(text) is True
    assert check_bind_address(text, allow_any=False)


@pytest.mark.parametrize(
    "text",
    [
        "8.8.8.8",
        "1.1.1.1",
        "2001:4860:4860::8888",
        "224.0.0.1",
        "ff02::1",
        "0.0.0.0",  # noqa: S104 - the wildcard is exactly what is being refused
        "::",
    ],
)
def test_non_lan_addresses(text: str) -> None:
    assert is_lan_address(text) is False


def test_garbage_is_not_lan_and_not_a_bind_address() -> None:
    assert is_lan_address("laptop.local") is False
    with pytest.raises(ConfigError, match="IP literal") as info:
        check_bind_address("laptop.local", allow_any=True)
    assert info.value.failure_type is FailureType.CONFIG_ERROR
    assert info.value.exit_code is ExitCode.CONFIG_ERROR
    assert info.value.details == {"bind": "laptop.local"}


@pytest.mark.parametrize("text", ["0.0.0.0", "::"])  # noqa: S104 - tests the refusal
def test_wildcard_refused_unless_allow_any(text: str) -> None:
    with pytest.raises(ConfigError, match="wildcard") as info:
        check_bind_address(text, allow_any=False)
    assert "LAN IP" in str(info.value)  # operator-readable remedy
    assert check_bind_address(text, allow_any=True) == text


@pytest.mark.parametrize("text", ["8.8.8.8", "2001:4860:4860::8888"])
def test_public_refused_unless_allow_any(text: str) -> None:
    with pytest.raises(ConfigError, match="non-private"):
        check_bind_address(text, allow_any=False)
    assert check_bind_address(text, allow_any=True) == text


def test_check_normalises_and_strips() -> None:
    assert check_bind_address(" 127.0.0.1 ", allow_any=False) == "127.0.0.1"
    assert check_bind_address("::FFFF:10.0.0.1", allow_any=False) == "10.0.0.1"
    assert check_bind_address("0:0:0:0:0:0:0:1", allow_any=False) == "::1"


def test_multicast_is_never_acceptable() -> None:
    # allow_any lifts the LAN rule, but is_lan_address stays False for multicast;
    # the parser accepts it only because the OS will refuse the bind itself.
    assert is_lan_address("224.0.0.1") is False


# ------------------------------------------------------------------ parse_bind


def test_parse_ipv4_and_ipv6() -> None:
    v4 = parse_bind("127.0.0.1:47100")
    assert isinstance(v4, BindAddress)
    assert (v4.host, v4.port) == ("127.0.0.1", 47100)
    assert v4.grpc_target() == "127.0.0.1:47100"
    v6 = parse_bind("[::1]:47110")
    assert (v6.host, v6.port) == ("::1", 47110)
    assert v6.grpc_target() == "[::1]:47110"


def test_parse_port_zero_means_any() -> None:
    assert parse_bind("127.0.0.1:0").port == 0


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("127.0.0.1", "ip:port"),
        ("::1:47100", "bracket IPv6"),  # unbracketed v6 is ambiguous
        ("[::1]47100", r"\[ipv6\]:port"),
        ("[::1]", r"\[ipv6\]:port"),
        ("127.0.0.1:abc", "non-numeric"),
        ("127.0.0.1:65536", "0..65535"),
        ("127.0.0.1:-1", "0..65535"),
        ("", "ip:port"),
    ],
)
def test_parse_rejects_malformed(text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match) as info:
        parse_bind(text)
    assert info.value.details == {"bind": text}


def test_parse_rejects_hostname() -> None:
    with pytest.raises(ConfigError, match="IP literal") as info:
        parse_bind("laptop:47100")
    assert info.value.details == {"bind": "laptop"}


def test_parse_applies_lan_rule_with_allow_any() -> None:
    with pytest.raises(ConfigError, match="wildcard"):
        parse_bind("0.0.0.0:47100")
    assert parse_bind("0.0.0.0:47100", allow_any=True).host == "0.0.0.0"  # noqa: S104
    with pytest.raises(ConfigError, match="non-private"):
        parse_bind("8.8.8.8:47100")


def test_bind_address_is_a_plain_tuple() -> None:
    host, port = parse_bind("10.1.2.3:5")
    assert (host, port) == ("10.1.2.3", 5)
    assert parse_bind("10.1.2.3:5") == ("10.1.2.3", 5)
