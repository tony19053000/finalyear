"""Unit tests for ``lab/spikes/_s3/protocol.py``: sizes, chunks, control-message
validation, framing (over a socketpair), address helpers and typed failures."""

from __future__ import annotations

import errno
import socket
import struct
from collections.abc import Iterator

import pytest

from spikes._harness.protocol import ProtocolError
from spikes._s3 import protocol as p


# ------------------------------------------------------------------ sizes
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4096", 4096),
        ("4KiB", 4 * p.KIB),
        ("64MiB", 64 * p.MIB),
        ("64mib", 64 * p.MIB),
        (" 1 GiB ", p.GIB),
        ("2GiB", 2 * p.GIB),
        ("1MB", 1_000_000),
        ("1GB", 1_000_000_000),
        ("1.5MiB", int(1.5 * p.MIB)),
        ("8m", 8 * p.MIB),
        ("8k", 8 * p.KIB),
        ("8192B", 8192),
    ],
)
def test_parse_size_units(text: str, expected: int) -> None:
    assert p.parse_size(text) == expected


@pytest.mark.parametrize(
    "text", ["", "abc", "1XB", "-1MiB", "1KiB", "5GiB", "0", "1e6", "1MiB,2MiB"]
)
def test_parse_size_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        p.parse_size(text)


def test_parse_sizes_sorts_and_dedups() -> None:
    assert p.parse_sizes("8MiB, 1MiB,8MiB,") == (p.MIB, 8 * p.MIB)
    with pytest.raises(ValueError, match="no sizes"):
        p.parse_sizes(" , ")


def test_parse_chunk_range() -> None:
    assert p.parse_chunk("4KiB") == p.MIN_CHUNK_BYTES
    assert p.parse_chunk("4MiB") == p.MAX_CHUNK_BYTES
    with pytest.raises(ValueError, match="chunk"):
        p.parse_chunk("8MiB")
    with pytest.raises(ValueError):
        p.parse_chunk("1KiB")  # below MIN_SIZE_BYTES already


@pytest.mark.parametrize(
    ("num", "text"),
    [
        (p.GIB, "1GiB"),
        (2 * p.GIB, "2GiB"),
        (64 * p.MIB, "64MiB"),
        (256 * p.KIB, "256KiB"),
        (1536 * p.KIB, "1536KiB"),
        (5000, "5000B"),
        (0, "0B"),
    ],
)
def test_format_size(num: int, text: str) -> None:
    assert p.format_size(num) == text


def test_select_sizes_keeps_smallest_when_cap_is_tiny() -> None:
    sizes = (p.MIB, 8 * p.MIB, 64 * p.MIB)
    assert p.select_sizes(sizes, 8 * p.MIB) == (p.MIB, 8 * p.MIB)
    assert p.select_sizes(sizes, 100 * p.MIB) == sizes
    assert p.select_sizes(sizes, 4 * p.KIB) == (p.MIB,)


def test_default_sizes_span_1mib_to_2gib_and_chunk_sweep_fits_payload() -> None:
    assert p.DEFAULT_SIZES[0] == p.MIB and p.DEFAULT_SIZES[-1] == 2 * p.GIB
    assert tuple(sorted(p.DEFAULT_SIZES)) == p.DEFAULT_SIZES
    assert all(p.MIN_CHUNK_BYTES <= c <= p.MAX_CHUNK_BYTES for c in p.CHUNK_SWEEP)
    assert p.DEFAULT_CHUNK_BYTES in p.CHUNK_SWEEP


# ------------------------------------------------------------------- gRPC
def test_grpc_limit_and_target() -> None:
    assert p.grpc_limit(p.MIB) == p.MIB + p.GRPC_LIMIT_SLACK
    assert p.grpc_limit(p.MAX_CHUNK_BYTES) > p.GRPC_DEFAULT_LIMIT
    assert p.grpc_target("10.0.0.2", 47302) == "10.0.0.2:47302"
    assert p.grpc_target("::1", 47302) == "[::1]:47302"


@pytest.mark.parametrize(
    ("peer", "host"),
    [
        ("ipv4:10.0.0.2:53422", "10.0.0.2"),
        ("ipv6:[::1]:53422", "::1"),
        ("ipv6:[fe80::1%25eth0]:1", "fe80::1%eth0"),
        ("ipv4:10.0.0.2", "10.0.0.2"),
        ("unix:/tmp/sock", None),
        ("ipv6:[::1", None),
        ("", None),
        ("ipv4:", None),
        ("ipv4::5", None),
    ],
)
def test_peer_host(peer: str, host: str | None) -> None:
    assert p.peer_host(peer) == host


# --------------------------------------------------------------- messages
def test_transfer_request_accepts_valid() -> None:
    msg = {"transfer_id": "tcp-abc123", "size": p.MIB, "chunk": 64 * p.KIB}
    assert p.transfer_request(msg) == ("tcp-abc123", p.MIB, 64 * p.KIB)


@pytest.mark.parametrize(
    "msg",
    [
        {},
        {"transfer_id": "x", "size": p.MIB},
        {"transfer_id": "../x", "size": p.MIB, "chunk": p.MIB},
        {"transfer_id": "x" * 65, "size": p.MIB, "chunk": p.MIB},
        {"transfer_id": 5, "size": p.MIB, "chunk": p.MIB},
        {"transfer_id": "x", "size": p.MAX_SIZE_BYTES + 1, "chunk": p.MIB},
        {"transfer_id": "x", "size": 0, "chunk": p.MIB},
        {"transfer_id": "x", "size": -p.MIB, "chunk": p.MIB},
        {"transfer_id": "x", "size": 1.5e6, "chunk": p.MIB},
        {"transfer_id": "x", "size": True, "chunk": p.MIB},
        {"transfer_id": "x", "size": "1048576", "chunk": p.MIB},
        {"transfer_id": "x", "size": p.MIB, "chunk": p.MAX_CHUNK_BYTES + 1},
        {"transfer_id": "x", "size": p.MIB, "chunk": 0},
        {"transfer_id": "x", "size": p.MIB, "chunk": None},
    ],
)
def test_transfer_request_rejects_hostile_input(msg: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        p.transfer_request(msg)


def test_valid_transfer_id() -> None:
    assert p.valid_transfer_id("grpc-0123abcd")
    assert not p.valid_transfer_id("")
    assert not p.valid_transfer_id("a b")
    assert not p.valid_transfer_id(None)


# ---------------------------------------------------------------- framing
@pytest.fixture
def pair() -> Iterator[tuple[socket.socket, socket.socket]]:
    a, b = socket.socketpair()
    a.settimeout(5)
    b.settimeout(5)
    try:
        yield a, b
    finally:
        a.close()
        b.close()


def test_framing_round_trip(pair: tuple[socket.socket, socket.socket]) -> None:
    a, b = pair
    p.send_msg(a, {"op": "hello", "protocol": p.PROTOCOL, "n": [1, 2]})
    assert p.recv_msg(b) == {"op": "hello", "protocol": p.PROTOCOL, "n": [1, 2]}


def test_framing_rejects_oversized_and_zero_length(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    a, b = pair
    a.sendall(struct.pack("!I", p.MAX_HEADER_BYTES + 1))
    with pytest.raises(ProtocolError, match="bad header length"):
        p.recv_msg(b)
    a.sendall(struct.pack("!I", 0))
    with pytest.raises(ProtocolError, match="bad header length"):
        p.recv_msg(b)
    with pytest.raises(ProtocolError, match="too large"):
        p.send_msg(a, {"pad": "x" * p.MAX_HEADER_BYTES})


def test_framing_rejects_non_json_and_eof(
    pair: tuple[socket.socket, socket.socket],
) -> None:
    a, b = pair
    body = b"\xff\xfe not json"
    a.sendall(struct.pack("!I", len(body)) + body)
    with pytest.raises(ProtocolError, match="not JSON"):
        p.recv_msg(b)
    body = b"[1, 2]"
    a.sendall(struct.pack("!I", len(body)) + body)
    with pytest.raises(ProtocolError, match="not a JSON object"):
        p.recv_msg(b)
    a.sendall(struct.pack("!I", 10) + b"abc")
    a.close()
    with pytest.raises(ConnectionResetError):
        p.recv_msg(b)


# --------------------------------------------------------------- failures
def test_fatal_failure_types() -> None:
    assert p.TransferError(p.FailureType.TIMEOUT, "x").fatal
    assert p.TransferError(p.FailureType.SERVER_UNREACHABLE, "x").fatal
    assert p.TransferError(p.FailureType.CONNECTION_LOST, "x").fatal
    for kind in (
        p.FailureType.CHECKSUM_MISMATCH,
        p.FailureType.SHORT_TRANSFER,
        p.FailureType.GRPC_UNAVAILABLE,
        p.FailureType.TRANSPORT_ERROR,
        p.FailureType.NO_STATS,
        p.FailureType.SERVER_ERROR,
        p.FailureType.PROTOCOL_ERROR,
    ):
        assert not p.TransferError(kind, "x").fatal, kind
    assert str(p.FailureType.GRPC_UNAVAILABLE) == "grpc_unavailable"


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (TimeoutError(), p.FailureType.TIMEOUT),
        (ConnectionRefusedError(), p.FailureType.SERVER_UNREACHABLE),
        (ConnectionResetError(), p.FailureType.CONNECTION_LOST),
        (BrokenPipeError(), p.FailureType.CONNECTION_LOST),
        (ConnectionAbortedError(), p.FailureType.CONNECTION_LOST),
        (OSError(errno.EHOSTUNREACH, "no route"), p.FailureType.SERVER_UNREACHABLE),
        (OSError(errno.ENETUNREACH, "no net"), p.FailureType.SERVER_UNREACHABLE),
        (socket.gaierror(8, "name"), p.FailureType.SERVER_UNREACHABLE),
        (OSError(errno.EIO, "io"), p.FailureType.CONNECTION_LOST),
    ],
)
def test_classify_os_error(exc: OSError, kind: p.FailureType) -> None:
    err = p.classify_os_error(exc, "doing")
    assert err.failure_type is kind
    assert str(err).startswith("doing")


def test_address_helpers_reexported() -> None:
    assert p.is_lan_address("192.168.1.5") and not p.is_lan_address("8.8.8.8")
    assert p.check_bind_address("127.0.0.1", allow_any=False) == "127.0.0.1"
    with pytest.raises(ValueError):
        p.check_bind_address("0.0.0.0", allow_any=False)  # noqa: S104 - test data
    assert p.family_of("::1") == socket.AF_INET6
