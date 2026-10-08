"""V0.7 G2 tester round 2: D152 rate-limit keys (IPv6 per /64, IPv4 per address,
IPv4-mapped = IPv4, zone dropped, unparseable = one shared 'unknown' bucket)."""

# ruff: noqa: S311, E501

from __future__ import annotations

import ipaddress
import random

import pytest

pytest.importorskip("grpc")

from meshforge.controller.enrollment.listener import (
    UNKNOWN_SOURCE,
    SourceRateLimiter,
    rate_key,
    source_ip,
)


def _v6(addr: str, port: int = 5000) -> str:
    return f"ipv6:%5B{addr}%5D:{port}"


def test_ipv4_per_address_and_ports_ignored() -> None:
    assert rate_key("ipv4:10.0.0.7:1") == rate_key("ipv4:10.0.0.7:60000") == "10.0.0.7"
    assert rate_key("ipv4:10.0.0.8:1") != rate_key("ipv4:10.0.0.7:1")


def test_ipv6_same_slash64_shares_a_key_other_slash64_does_not() -> None:
    rng = random.Random(1)
    keys = {
        rate_key(
            _v6(
                str(
                    ipaddress.IPv6Address(
                        int(ipaddress.IPv6Address("2001:db8:1:2::"))
                        | rng.getrandbits(64)
                    )
                ),
                rng.randint(1, 65535),
            )
        )
        for _ in range(3000)
    }
    assert keys == {"2001:db8:1:2::/64"}
    assert rate_key(_v6("2001:db8:1:3::1")) != rate_key(_v6("2001:db8:1:2::1"))
    # /63 neighbours are different /64s
    assert rate_key(_v6("2001:db8:1:0::1")) != rate_key(_v6("2001:db8:1:1::1"))
    # spelling differences do not matter
    assert rate_key(_v6("2001:DB8:1:2:0:0:0:9")) == rate_key(_v6("2001:db8:1:2::9"))
    # the raw bracket form (older gRPC) too
    assert rate_key("ipv6:[2001:db8:1:2::9]:5") == "2001:db8:1:2::/64"


def test_mapped_ipv4_counts_as_ipv4_and_zone_is_dropped() -> None:
    assert rate_key(_v6("::ffff:1.2.3.4")) == rate_key("ipv4:1.2.3.4:9") == "1.2.3.4"
    assert rate_key(_v6("::ffff:0102:0304")) == "1.2.3.4"
    assert (
        rate_key("ipv6:%5Bfe80::1%25eth0%5D:5")
        == rate_key(_v6("fe80::2"))
        == "fe80::/64"
    )
    assert rate_key("ipv6:%5Bfe80::1%25other%5D:5") == "fe80::/64"


@pytest.mark.parametrize(
    "peer",
    [
        "", "unknown", "unix:/tmp/x", "unix-abstract:abc", "ipv4:999.1.1.1:5", "ipv4:01.2.3.4:5",
        "ipv4:1.2.3:5", "ipv6:%5Bzz%5D:5", "ipv6:[]:5", "ipv4:1.2.3.4", "ipv4:1.2.3.4:5\n",
        "ipv6:%255B::1%255D:5", "x" * 100_000, "ipv4:" + "1" * 5000 + ":5", "\x00", "ipv4:١.٢.٣.٤:5",
    ],
)  # fmt: skip
def test_unparseable_peers_all_share_the_unknown_bucket(peer: str) -> None:
    assert rate_key(peer) == UNKNOWN_SOURCE
    assert (
        len(source_ip(peer)) <= 5100
    )  # unreachable over TCP; only digit-dot runs exceed 128


def _lim(
    rate: float = 0.001, burst: int = 3, max_sources: int = 64
) -> SourceRateLimiter:
    return SourceRateLimiter(
        rate_per_minute=rate, burst=burst, max_sources=max_sources, clock=lambda: 1.0
    )


def test_rotating_addresses_inside_a_64_is_limited_across_64s_is_not() -> None:
    lim = _lim()
    base = int(ipaddress.IPv6Address("2001:db8:aa:bb::"))
    inside = [
        lim.allow(rate_key(_v6(str(ipaddress.IPv6Address(base + i + 1)), 1000 + i)))
        for i in range(50)
    ]
    assert inside.count(True) == 3
    across = [
        lim.allow(rate_key(_v6(str(ipaddress.IPv6Address(base + (i << 64) + 1)))))
        for i in range(1, 6)
    ]
    assert all(across)  # a different /64 is a different laptop (documented)


def test_mapped_and_plain_ipv4_cannot_double_the_budget() -> None:
    lim = _lim()
    results = []
    for i in range(10):
        peer = "ipv4:1.2.3.4:5" if i % 2 else _v6("::ffff:1.2.3.4")
        results.append(lim.allow(rate_key(peer)))
    assert results.count(True) == 3


def test_unknown_flood_is_one_bucket_and_cannot_starve_real_peers() -> None:
    """Unparseable peers share ONE budget (fail closed). They can exhaust only their
    own bucket: real TCP peers always parse (ipv4:/ipv6:), so a remote attacker cannot
    produce an 'unknown' peer and no legitimate client is starved by it."""
    lim = _lim()
    junk = [lim.allow(rate_key(f"weird-{i}")) for i in range(100)]
    assert junk.count(True) == 3
    assert lim.allow(rate_key("ipv4:192.168.1.5:1")) and lim.allow(
        rate_key(_v6("2001:db8::1"))
    )


def test_many_slash64s_keep_memory_bounded() -> None:
    lim = _lim(max_sources=128)
    for i in range(50_000):
        lim.allow(
            rate_key(
                _v6(str(ipaddress.IPv6Address((0x20010DB8 << 96) + (i << 64) + 1)))
            )
        )
    assert len(lim) <= 128
