"""V0.7 G2 tester: the enrolment rate limiter (brief 5b item 3, checklist 19).

Per-IP isolation, refill arithmetic, bounded memory under many sources, and how
the *real* gRPC peer string turns into a key. T-1 (fixed in fix round 1, D152): IPv6 peers
arrive as ``ipv6:%5B::1%5D:<port>`` (percent-encoded brackets), the parser does
not match, and the whole string - port included - becomes the bucket key, so
every IPv6 connection gets a fresh full bucket and the table fills with junk.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

pytest.importorskip("grpc")

import grpc

from meshforge.common.proto.meshforge.v1 import enroll_pb2, enroll_pb2_grpc
from meshforge.controller.enrollment.listener import (
    SourceRateLimiter,
    source_ip,
    status_for,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(clock: Clock, **kw: float) -> SourceRateLimiter:
    args: dict[str, float] = {"rate_per_minute": 6, "burst": 3, "max_sources": 16}
    args.update(kw)
    return SourceRateLimiter(
        rate_per_minute=args["rate_per_minute"],
        burst=int(args["burst"]),
        max_sources=int(args["max_sources"]),
        clock=clock,
    )


def test_burst_then_limit_then_refill_arithmetic() -> None:
    clock = Clock()
    lim = _limiter(clock)  # 6/min = one token per 10 s, burst 3
    assert [lim.allow("a") for _ in range(4)] == [True, True, True, False]
    clock.now += 9.9
    assert lim.allow("a") is False  # 0.99 tokens
    clock.now += 0.1
    assert lim.allow("a") is True
    assert lim.allow("a") is False
    clock.now += 10_000  # long idle never exceeds the burst
    assert [lim.allow("a") for _ in range(4)] == [True, True, True, False]


def test_a_limited_source_does_not_affect_another_and_denials_are_free() -> None:
    clock = Clock()
    lim = _limiter(clock)
    for _ in range(50):
        lim.allow("flood")
    assert all(lim.allow("quiet") for _ in range(3))
    # Hammering while denied does not push the recovery further away.
    clock.now += 10.0
    assert lim.allow("flood") is True


def test_clock_going_backwards_never_mints_tokens_or_crashes() -> None:
    clock = Clock()
    lim = _limiter(clock)
    for _ in range(3):
        lim.allow("a")
    clock.now -= 500
    assert lim.allow("a") is False  # (time.monotonic cannot really go back)


def test_memory_is_bounded_under_a_million_distinct_sources() -> None:
    clock = Clock()
    lim = _limiter(clock, max_sources=64)
    for i in range(200_000):
        lim.allow(f"10.{i >> 16 & 255}.{i >> 8 & 255}.{i & 255}")
    assert len(lim) <= 64


def test_an_active_source_is_not_evicted_before_idle_ones() -> None:
    clock = Clock()
    lim = _limiter(clock, max_sources=16, burst=2, rate_per_minute=0.001)
    assert lim.allow("busy") and lim.allow("busy") and not lim.allow("busy")
    for i in range(15):  # 15 others: the table is full but "busy" is recent
        lim.allow(f"other{i}")
    assert lim.allow("busy") is False  # still remembered, still limited


@pytest.mark.parametrize(
    "kw",
    [{"rate_per_minute": 0}, {"rate_per_minute": -1}, {"burst": 0}, {"max_sources": 0}],
)
def test_invalid_configuration_is_refused(kw: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        _limiter(Clock(), **kw)


def test_source_ip_ipv4_and_oddities() -> None:
    assert source_ip("ipv4:10.0.0.7:5555") == "10.0.0.7"
    assert source_ip("unix:/tmp/x.sock") == "unix:/tmp/x.sock"
    assert len(source_ip("x" * 10_000)) <= 128  # a hostile string cannot bloat a key
    assert source_ip("") == ""


def test_status_for_every_reason_is_a_real_grpc_status() -> None:
    from meshforge.controller.enrollment.service import EnrollReason

    for reason in EnrollReason:
        assert isinstance(status_for(str(reason)), grpc.StatusCode)
    assert status_for("csr_wrong_curve") is grpc.StatusCode.INVALID_ARGUMENT
    assert status_for("something_new") is grpc.StatusCode.INTERNAL


async def _real_peers(bind: str, target: str, calls: int = 3) -> list[str]:
    peers: list[str] = []

    class Servicer(enroll_pb2_grpc.EnrollmentServicer):
        async def Enroll(self, request, context):  # type: ignore[no-untyped-def]  # noqa: N802
            peers.append(context.peer())
            return enroll_pb2.EnrollResponse()

    server = grpc.aio.server()
    enroll_pb2_grpc.add_EnrollmentServicer_to_server(Servicer(), server)
    port = server.add_insecure_port(bind)
    await server.start()
    try:
        for _ in range(calls):  # a new connection (new source port) each time
            async with grpc.aio.insecure_channel(f"{target}:{port}") as channel:
                await enroll_pb2_grpc.EnrollmentStub(channel).Enroll(
                    enroll_pb2.EnrollRequest()
                )
    finally:
        await server.stop(0)
    return peers


def _has_ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
    except OSError:
        return False
    return True


def test_real_ipv4_peers_collapse_to_one_key() -> None:
    peers = asyncio.run(_real_peers("127.0.0.1:0", "127.0.0.1"))
    assert {source_ip(p) for p in peers} == {"127.0.0.1"}


@pytest.mark.skipif(not _has_ipv6(), reason="no IPv6 loopback")
def test_real_ipv6_peers_collapse_to_one_key() -> None:
    peers = asyncio.run(_real_peers("[::1]:0", "[::1]"))
    assert {source_ip(p) for p in peers} == {"::1"}, peers
