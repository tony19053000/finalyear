"""V0.8 G1 tester r1: ``[controller.http] loopback_plain`` must refuse every
address that is not exactly loopback (Q1: plain HTTP never leaves the host)."""

# ruff: noqa: RUF001, S104

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meshforge.controller.settings import HttpSettings

pytestmark = [pytest.mark.unit, pytest.mark.security]

REFUSED = [
    "0.0.0.0:47111",
    "0.0.0.0",
    "[::]:47111",
    "::",
    ":47111",
    "",
    " ",
    "[::ffff:0.0.0.0]:47111",
    "[::ffff:192.168.1.5]:47111",
    "127.0.0.2:47111",  # other 127/8
    "127.1:47111",
    "127.0.0.1.nip.io:47111",
    "0177.0.0.1:47111",  # octal
    "2130706433:47111",  # decimal 127.0.0.1
    "0x7f000001:47111",
    "localhost:47111",
    "LOCALHOST:47111",
    "[localhost]:47111",
    "10.0.0.5:47111",
    "192.168.1.10:47111",
    "169.254.1.1:47111",
    "8.8.8.8:47111",
    "[fe80::1]:47111",
    "[fe80::1%eth0]:47111",
    "[::1%eth0]:47111",
    "[2001:db8::1]:47111",
    "127.0.0.1:99999",
    "127.0.0.1:-1",
    "127.0.0.1:abc",
    "127.0.0.1;0.0.0.0:47111",
    "127.0.0.1,0.0.0.0:47111",
    "127.0.0.1:47111\n0.0.0.0:1",
    "１２７.0.0.1:47111",  # full-width digits
    "http://127.0.0.1:47111",
]

ACCEPTED = ["127.0.0.1:47111", "[::1]:47111", "127.0.0.1:0", "[::1]:0"]


@pytest.mark.parametrize("text", REFUSED)
def test_every_non_loopback_or_odd_bind_is_refused(text: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        HttpSettings(loopback_plain=text)


@pytest.mark.parametrize("text", ACCEPTED)
def test_exact_loopback_binds_are_accepted(text: str) -> None:
    assert HttpSettings(loopback_plain=text).loopback_plain == text


def test_off_by_default() -> None:
    assert HttpSettings().loopback_plain is None


def test_an_ipv4_mapped_loopback_is_accepted_only_if_it_normalises_to_real_loopback() -> (
    None
):
    """Accepted by the settings (it IS loopback); what the server binds is the
    normalised ``127.0.0.1`` - never the mapped form, never a wildcard."""
    from meshforge.common.netaddr import parse_bind

    text = HttpSettings(loopback_plain="[::ffff:127.0.0.1]:47111").loopback_plain
    assert text is not None
    assert parse_bind(text).host == "127.0.0.1"
