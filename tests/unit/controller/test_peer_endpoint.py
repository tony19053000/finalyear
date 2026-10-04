"""V0.5 G4 D98/D99: Hello.peer_endpoint validation and the key in HelloAck."""

from __future__ import annotations

import datetime as dt

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.session.admission import (
    MAX_PEER_ENDPOINT_CHARS,
    hello_ack,
    peer_endpoint_of,
)

pytestmark = pytest.mark.unit


def _of(
    endpoint: str, peer: str = "ipv4:192.168.1.20:51000", *, trust: bool = False
) -> tuple[str, str | None]:
    return peer_endpoint_of(agent_pb2.Hello(peer_endpoint=endpoint), peer, trust=trust)


@pytest.mark.parametrize(
    ("endpoint", "peer", "expected"),
    [
        ("", "ipv4:192.168.1.20:5", ("", None)),
        ("192.168.1.20:47120", "ipv4:192.168.1.20:5", ("192.168.1.20:47120", None)),
        ("192.168.1.20:1", "ipv4:192.168.1.20:5", ("192.168.1.20:1", None)),
        ("192.168.1.20:65535", "ipv4:192.168.1.20:5", ("192.168.1.20:65535", None)),
        ("192.168.1.20:0", "ipv4:192.168.1.20:5", ("", "bad_port")),
        ("192.168.1.20:65536", "ipv4:192.168.1.20:5", ("", "bad_port")),
        ("192.168.1.21:47120", "ipv4:192.168.1.20:5", ("", "host_mismatch")),
        ("[::1]:47120", "ipv6:[::1]:5", ("[::1]:47120", None)),
        ("127.0.0.1:47120", "ipv6:[::ffff:127.0.0.1]:5", ("127.0.0.1:47120", None)),
        ("host.lan:47120", "ipv4:192.168.1.20:5", ("", "bad_host")),
        ("0.0.0.0:47120", "ipv4:0.0.0.0:5", ("", "bad_host")),
        ("192.168.1.20", "ipv4:192.168.1.20:5", ("", "malformed")),
        ("192.168.1.20:47120x", "ipv4:192.168.1.20:5", ("", "malformed")),
        ("::1:47120", "ipv6:[::1]:5", ("", "malformed")),
        ("192.168.1.20:47120", "unix:/tmp/sock", ("", "host_mismatch")),
        ("192.168.1.20:4\n7", "ipv4:192.168.1.20:5", ("", "malformed")),
    ],
)
def test_peer_endpoint_rules(
    endpoint: str, peer: str, expected: tuple[str, str | None]
) -> None:
    assert _of(endpoint, peer) == expected


def test_trust_accepts_another_host_but_not_garbage() -> None:
    assert _of("10.0.0.9:47120", trust=True) == ("10.0.0.9:47120", None)
    assert _of("10.0.0.9:0", trust=True) == ("", "bad_port")
    assert _of("nonsense", trust=True) == ("", "malformed")


def test_length_bound_max_and_max_plus_one() -> None:
    exact = "1" * (MAX_PEER_ENDPOINT_CHARS - 6) + ":47120"
    assert len(exact) == MAX_PEER_ENDPOINT_CHARS
    # At the bound it is parsed (and refused for its host, not its size) ...
    assert _of(exact, trust=True) == ("", "bad_host")
    # ... one more character is refused before parsing.
    assert _of(exact + "0", trust=True) == ("", "malformed")


def test_hello_ack_carries_the_peer_token_key() -> None:
    liveness = LivenessConfig(
        heartbeat_interval_s=1.0, miss_allowance_s=0.5, lease_multiplier=3
    )
    msg = hello_ack("sess", liveness, dt.datetime.now(dt.UTC), peer_token_key=b"k" * 32)
    assert msg.hello_ack.peer_token_key == b"k" * 32
    assert (
        hello_ack("sess", liveness, dt.datetime.now(dt.UTC)).hello_ack.peer_token_key
        == b""
    )
