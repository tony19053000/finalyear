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
