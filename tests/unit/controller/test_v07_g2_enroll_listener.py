"""V0.7 G2 (D135): the enrolment listener's pure parts - per-IP token bucket
(injected clock), peer parsing, reason -> gRPC status."""

from __future__ import annotations

import grpc
import pytest

from meshforge.controller.enrollment.listener import (
    SourceRateLimiter,
    source_ip,
    status_for,
)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_bucket_burst_refill_and_isolation() -> None:
    clock = Clock()
    limiter = SourceRateLimiter(rate_per_minute=6, burst=2, max_sources=16, clock=clock)
    assert [limiter.allow("10.0.0.1") for _ in range(3)] == [True, True, False]
    assert limiter.allow("10.0.0.2")  # another IP has its own bucket
    clock.now += 9.9  # 6/min = one token per 10 s
    assert not limiter.allow("10.0.0.1")
    clock.now += 0.2
    assert limiter.allow("10.0.0.1")
    assert not limiter.allow("10.0.0.1")
    clock.now += 3600  # refill caps at the burst
    assert [limiter.allow("10.0.0.1") for _ in range(3)] == [True, True, False]


def test_table_is_bounded() -> None:
    limiter = SourceRateLimiter(
        rate_per_minute=1, burst=1, max_sources=4, clock=Clock()
    )
    for i in range(10):
        limiter.allow(f"10.0.0.{i}")
    assert len(limiter) == 4


@pytest.mark.parametrize(
    ("rate", "burst", "size"), [(0, 1, 1), (1, 0, 1), (1, 1, 0), (-1, 1, 1)]
)
def test_bad_limits_refused(rate: float, burst: int, size: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        SourceRateLimiter(
            rate_per_minute=rate, burst=burst, max_sources=size, clock=Clock()
        )


def test_source_ip() -> None:
    assert source_ip("ipv4:192.168.1.5:5123") == "192.168.1.5"
    assert source_ip("ipv6:[::1]:5123") == "::1"
    assert source_ip("ipv6:[fe80::1%eth0]:1") == "fe80::1%eth0"
    assert source_ip("unix:/tmp/x") == "unix:/tmp/x"
    assert len(source_ip("x" * 1000)) == 128


def test_status_for() -> None:
    assert status_for("csr_bad_signature") is grpc.StatusCode.INVALID_ARGUMENT
    assert status_for("token_used") is grpc.StatusCode.UNAUTHENTICATED
    assert status_for("node_key_conflict") is grpc.StatusCode.ALREADY_EXISTS
    assert status_for("node_revoked") is grpc.StatusCode.PERMISSION_DENIED
    assert status_for("rate_limited") is grpc.StatusCode.RESOURCE_EXHAUSTED
    assert status_for("unheard_of") is grpc.StatusCode.INTERNAL


# ------------------------------------------------------------- D152 (T-1)


@pytest.mark.security
def test_rate_key_decodes_grpc_peers_and_buckets_ipv6_per_64() -> None:
    from meshforge.controller.enrollment.listener import UNKNOWN_SOURCE, rate_key

    assert rate_key("ipv4:10.0.0.7:5555") == "10.0.0.7"
    assert rate_key("ipv4:10.0.0.7:1") == rate_key("ipv4:10.0.0.7:2")
    assert rate_key("ipv4:10.0.0.8:1") != rate_key("ipv4:10.0.0.7:1")
    # gRPC's real form is percent-encoded.
    assert rate_key("ipv6:%5B::1%5D:40000") == "::/64"
    assert source_ip("ipv6:%5B::1%5D:40000") == "::1"
    # Privacy addresses inside one /64 share a bucket; another /64 does not.
    a = rate_key("ipv6:%5B2001:db8:1:2::aaaa%5D:1")
    b = rate_key("ipv6:%5B2001:db8:1:2:ffff:ffff:ffff:ffff%5D:9")
    c = rate_key("ipv6:%5B2001:db8:1:3::1%5D:1")
    assert a == b == "2001:db8:1:2::/64" and c != a
    assert rate_key("ipv6:%5Bfe80::1%25eth0%5D:1") == "fe80::/64"
    assert rate_key("ipv6:%5B::ffff:10.0.0.7%5D:1") == "10.0.0.7"
    # Unparseable peers share ONE bucket (never a fresh one each).
    for peer in ("", "unix:/tmp/x", "ipv4:999.1.1.1:1", "garbage" * 50):
        assert rate_key(peer) == UNKNOWN_SOURCE, peer


@pytest.mark.security
def test_unknown_peers_exhaust_one_shared_bucket() -> None:
    from meshforge.controller.enrollment.listener import rate_key

    limiter = SourceRateLimiter(
        rate_per_minute=0.01, burst=2, max_sources=16, clock=Clock()
    )
    results = [limiter.allow(rate_key(f"unix:/tmp/s{i}")) for i in range(4)]
    assert results == [True, True, False, False]
    v6 = [limiter.allow(rate_key(f"ipv6:%5B2001:db8::{i:x}%5D:1")) for i in range(4)]
    assert v6 == [True, True, False, False]
