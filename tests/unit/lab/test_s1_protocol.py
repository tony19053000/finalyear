"""Unit tests for the S1 wire protocol, address safety rules and pure helpers (0.2.5).

No real sockets: framing is exercised against an in-memory fake socket.
"""

# ruff: noqa: S104 - wildcard addresses are test data

from __future__ import annotations

import errno
import json
import socket
import struct

import pytest

from spikes._s1 import iperf
from spikes._s1.client import mbps, ping_summary
from spikes._s1.protocol import (
    MAX_HEADER_BYTES,
    FailureType,
    ProtocolError,
    check_bind_address,
    classify_os_error,
    family_of,
    is_allowed_peer,
    is_lan_address,
    recv_msg,
    send_msg,
    valid_test_id,
)


class FakeSock:
    """Just enough of ``socket.socket`` for the framing functions."""

    def __init__(self, incoming: bytes = b"", chunk: int = 3) -> None:
        self.incoming = bytearray(incoming)
        self.sent = bytearray()
        self.chunk = chunk  # deliver in small pieces: framing must reassemble

    def recv(self, size: int) -> bytes:
        n = min(size, self.chunk, len(self.incoming))
        out = bytes(self.incoming[:n])
        del self.incoming[:n]
        return out

    def sendall(self, data: bytes) -> None:
        self.sent += data


def _frame(body: bytes) -> bytes:
    return struct.pack("!I", len(body)) + body


def test_send_then_recv_round_trip() -> None:
    out = FakeSock()
    send_msg(out, {"op": "hello", "protocol": 1})  # type: ignore[arg-type]
    (size,) = struct.unpack("!I", out.sent[:4])
    assert size == len(out.sent) - 4
    assert json.loads(out.sent[4:]) == {"op": "hello", "protocol": 1}
    back = recv_msg(FakeSock(bytes(out.sent)))  # type: ignore[arg-type]
    assert back == {"op": "hello", "protocol": 1}


@pytest.mark.parametrize(
    ("wire", "match"),
    [
        (struct.pack("!I", 0), "bad header length"),
        (struct.pack("!I", MAX_HEADER_BYTES + 1), "bad header length"),
        (_frame(b"not json"), "not JSON"),
        (_frame(b"\xff\xfe"), "not JSON"),
        (_frame(b"[1, 2]"), "not a JSON object"),
    ],
)
def test_recv_rejects_bad_frames(wire: bytes, match: str) -> None:
    with pytest.raises(ProtocolError, match=match):
        recv_msg(FakeSock(wire))  # type: ignore[arg-type]


def test_recv_eof_mid_frame_is_connection_reset() -> None:
    with pytest.raises(ConnectionResetError):
        recv_msg(FakeSock(_frame(b'{"a": 1}')[:6]))  # type: ignore[arg-type]


def test_send_rejects_oversized_and_nan() -> None:
    with pytest.raises(ProtocolError):
        send_msg(FakeSock(), {"x": "a" * MAX_HEADER_BYTES})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        send_msg(FakeSock(), {"x": float("nan")})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("abc-123", True),
        ("a" * 64, True),
        ("a" * 65, False),
        ("", False),
        ("a b", False),
        ("../x", False),
        (5, False),
        (None, False),
    ],
)
def test_valid_test_id(value: object, ok: bool) -> None:
    assert valid_test_id(value) is ok


# ------------------------------------------------------------ address rules
@pytest.mark.parametrize(
    "addr",
    [
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.20",
        "127.0.0.1",
        "169.254.3.4",
        "fd00::1",
        "fe80::1",
        "::1",
        "::ffff:192.168.1.5",
    ],
)
def test_lan_addresses_allowed(addr: str) -> None:
    assert is_lan_address(addr)
    assert is_allowed_peer(addr)


@pytest.mark.parametrize(
    "addr",
    [
        "8.8.8.8",
        "1.1.1.1",
        "2001:4860:4860::8888",
        "224.0.0.1",
        "0.0.0.0",
        "::",
        "not-an-ip",
        "",
        "::ffff:8.8.8.8",
    ],
)
def test_non_lan_addresses_refused(addr: str) -> None:
    assert not is_lan_address(addr)
    assert not is_allowed_peer(addr)


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::"])
def test_bind_wildcard_refused_without_allow_any(wildcard: str) -> None:
    with pytest.raises(ValueError, match="wildcard"):
        check_bind_address(wildcard, allow_any=False)
    assert check_bind_address(wildcard, allow_any=True) == wildcard


def test_bind_public_refused_without_allow_any() -> None:
    with pytest.raises(ValueError, match="non-private"):
        check_bind_address("8.8.8.8", allow_any=False)
    assert check_bind_address("8.8.8.8", allow_any=True) == "8.8.8.8"


@pytest.mark.parametrize("text", ["my-laptop", "192.168.1", "", "192.168.1.300"])
def test_bind_needs_ip_literal(text: str) -> None:
    with pytest.raises(ValueError, match="needs an IP"):
        check_bind_address(text, allow_any=True)


def test_bind_normalises() -> None:
    assert check_bind_address(" 192.168.1.20 ", allow_any=False) == "192.168.1.20"
    assert check_bind_address("::ffff:10.0.0.2", allow_any=False) == "10.0.0.2"
    assert check_bind_address("127.0.0.1", allow_any=False) == "127.0.0.1"


def test_family_of() -> None:
    assert family_of("10.0.0.1") == socket.AF_INET
    assert family_of("fd00::1") == socket.AF_INET6
    assert family_of("garbage") == socket.AF_INET


# ------------------------------------------------------------ error typing
@pytest.mark.parametrize(
    ("exc", "failure", "fatal"),
    [
        (TimeoutError(), FailureType.TIMEOUT, True),
        (ConnectionRefusedError(), FailureType.SERVER_UNREACHABLE, True),
        (ConnectionResetError(), FailureType.CONNECTION_LOST, True),
        (BrokenPipeError(), FailureType.CONNECTION_LOST, True),
        (ConnectionAbortedError(), FailureType.CONNECTION_LOST, True),
        (OSError(errno.EHOSTUNREACH, "no route"), FailureType.SERVER_UNREACHABLE, True),
        (OSError(errno.ENETUNREACH, "no net"), FailureType.SERVER_UNREACHABLE, True),
        (socket.gaierror(-2, "unknown"), FailureType.SERVER_UNREACHABLE, True),
        (OSError(errno.EINVAL, "odd"), FailureType.CONNECTION_LOST, True),
    ],
)
def test_classify_os_error(exc: OSError, failure: FailureType, fatal: bool) -> None:
    err = classify_os_error(exc, "doing x")
    assert err.failure_type is failure
    assert err.fatal is fatal
    assert str(err).startswith("doing x")


# ------------------------------------------------------------ pure metrics
def test_mbps() -> None:
    assert mbps(125_000_000, 1.0) == pytest.approx(1000.0)


def test_ping_summary_loss_and_jitter() -> None:
    sample = ping_summary(4, {3: 2.0, 0: 1.0, 1: 3.0})  # seq 2 lost; unordered input
    assert sample["sent"] == 4 and sample["received"] == 3
    assert sample["loss_pct"] == pytest.approx(25.0)
    assert sample["rtts_ms"] == [1.0, 3.0, 2.0]  # sequence order
    assert sample["jitter_ms"] == pytest.approx(1.5)  # (|3-1| + |2-3|) / 2
    assert sample["rtt_mean_ms"] == pytest.approx(2.0)
    assert sample["rtt_min_ms"] == 1.0 and sample["rtt_max_ms"] == 3.0
    assert sample["jitter_rfc3550_ms"] > 0


def test_ping_summary_single_reply_and_errors() -> None:
    sample = ping_summary(10, {5: 0.5})
    assert sample["loss_pct"] == pytest.approx(90.0)
    assert sample["jitter_ms"] == 0.0 and sample["rtt_stdev_ms"] == 0.0
    with pytest.raises(ValueError):
        ping_summary(3, {})


# ------------------------------------------------------------ iperf3 parsing
def test_parse_iperf3_json() -> None:
    doc = {
        "end": {
            "sum_sent": {"bits_per_second": 941_000_000.0},
            "sum_received": {"bits_per_second": 939_000_000.0},
        }
    }
    assert iperf.parse_iperf3_json(json.dumps(doc)) == {
        "receiver_mbps": pytest.approx(939.0),
        "sender_mbps": pytest.approx(941.0),
    }


@pytest.mark.parametrize(
    "text",
    ['{"error": "unable to connect to server"}', '{"end": {}}', "[]", "nonsense"],
)
def test_parse_iperf3_json_errors(text: str) -> None:
    with pytest.raises(ValueError):
        iperf.parse_iperf3_json(text)
