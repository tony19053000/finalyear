"""Unit tests for the S2 wire contract and - the valuable part - the error
classification: every blocked path must produce an exact, actionable diagnosis
(roadmap 0.2.6). No sockets are opened here."""

from __future__ import annotations

import errno
import socket

import pytest

from spikes._s2 import protocol as proto
from spikes._s2.grpcsrv import peer_address

FT = proto.FailureType


# ------------------------------------------------------------------ messages
def test_encode_decode_round_trip() -> None:
    assert proto.decode(proto.encode({"op": "ping", "seq": 3})) == {
        "op": "ping",
        "seq": 3,
    }


def test_encode_rejects_oversized_messages() -> None:
    with pytest.raises(proto.ProtocolError, match="too large"):
        proto.encode({"blob": "x" * (proto.MAX_MESSAGE_BYTES + 1)})


@pytest.mark.parametrize("body", [b"not json", b"[]", b"\xff\xfe", b'"text"'])
def test_decode_rejects_non_objects(body: bytes) -> None:
    with pytest.raises(proto.ProtocolError):
        proto.decode(body)


def test_decode_rejects_oversized_bodies() -> None:
    with pytest.raises(proto.ProtocolError, match="too large"):
        proto.decode(b"x" * (proto.MAX_MESSAGE_BYTES + 1))


@pytest.mark.parametrize(
    "value", [0, -1, proto.MAX_BLOB_BYTES + 1, "10", None, True, float("inf")]
)
def test_blob_size_rejects_out_of_range(value: object) -> None:
    with pytest.raises((proto.ProtocolError, OverflowError, ValueError)):
        proto.blob_size(value)


def test_blob_size_accepts_the_bounds() -> None:
    assert proto.blob_size(proto.MIN_BLOB_BYTES) == proto.MIN_BLOB_BYTES
    assert proto.blob_size(proto.MAX_BLOB_BYTES) == proto.MAX_BLOB_BYTES


# ----------------------------------------------------------------- keepalive
def test_keepalive_on_sends_pings_without_calls() -> None:
    options = dict(proto.channel_options("on"))
    assert options["grpc.keepalive_permit_without_calls"] == 1
    assert options["grpc.keepalive_time_ms"] > 0


def test_keepalive_off_sets_no_keepalive_option() -> None:
    options = dict(proto.channel_options("off"))
    assert not [k for k in options if "keepalive" in k]


def test_server_tolerates_client_pings() -> None:
    """Otherwise the server answers GOAWAY too_many_pings and we would be
    measuring our own configuration instead of the network."""
    options = dict(proto.server_options())
    assert options["grpc.keepalive_permit_without_calls"] == 1
    assert options["grpc.http2.min_ping_interval_without_data_ms"] > 0


def test_channel_options_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="keepalive"):
        proto.channel_options("maybe")


def test_keepalive_settings_records_both_sides() -> None:
    settings = proto.keepalive_settings("on")
    assert settings["mode"] == "on"
    assert settings["client_options"] and settings["server_options"]


# ------------------------------------------------------------ classification
@pytest.mark.parametrize(
    ("exc", "connecting", "expected"),
    [
        (ConnectionRefusedError(111, "refused"), True, FT.CONNECTION_REFUSED),
        (TimeoutError("timed out"), True, FT.CONNECT_TIMEOUT),
        (TimeoutError("timed out"), False, FT.TIMEOUT),
        (ConnectionResetError("reset"), True, FT.CONNECTION_RESET),
        (ConnectionResetError("reset"), False, FT.STREAM_DROPPED),
        (BrokenPipeError("pipe"), False, FT.STREAM_DROPPED),
        (OSError(errno.EHOSTUNREACH, "no route"), True, FT.HOST_UNREACHABLE),
        (OSError(errno.ENETUNREACH, "no net"), False, FT.HOST_UNREACHABLE),
        (socket.gaierror("name or service not known"), True, FT.HOST_UNREACHABLE),
    ],
)
def test_os_errors_are_classified(
    exc: BaseException, connecting: bool, expected: proto.FailureType
) -> None:
    error = proto.classify_os_error(exc, "HTTP /ping", connecting=connecting)
    assert error.failure_type is expected
    assert "HTTP /ping" in str(error)
    assert error.fix  # every diagnosis carries a fix


def test_refused_and_dropped_syn_are_distinguished() -> None:
    """The whole point: RST means "nothing listening", silence means "firewall"."""
    refused = proto.classify_os_error(
        ConnectionRefusedError(111, "refused"), "connect", connecting=True
    )
    dropped = proto.classify_os_error(TimeoutError(), "connect", connecting=True)
    assert refused.failure_type is not dropped.failure_type
    assert "firewall" in dropped.fix.lower()
    assert "listening" in refused.fix.lower()


@pytest.mark.parametrize(
    ("code", "details", "connecting", "expected"),
    [
        (
            "UNAVAILABLE",
            "failed to connect to all addresses; last error: UNKNOWN: "
            "ipv4:192.168.1.20:47203: Failed to connect to remote host: "
            "connect: Connection refused (111)",
            True,
            FT.CONNECTION_REFUSED,
        ),
        (
            "UNAVAILABLE",
            "failed to connect to all addresses; last error: UNKNOWN: "
            "ipv4:192.168.1.20:47203: Timeout occurred: FD shutdown",
            True,
            FT.CONNECT_TIMEOUT,
        ),
        (
            "UNAVAILABLE",
            "ipv4:192.168.1.20:47203: No route to host",
            True,
            FT.HOST_UNREACHABLE,
        ),
        ("UNAVAILABLE", "Socket closed", False, FT.STREAM_DROPPED),
        ("UNAVAILABLE", "keepalive watchdog timeout", False, FT.STREAM_DROPPED),
        ("UNAVAILABLE", "Endpoint read failed", False, FT.STREAM_DROPPED),
        ("UNAVAILABLE", "GOAWAY received; Error code: 11", False, FT.STREAM_DROPPED),
        # an explicit RST is reported as a reset, not as a silent idle drop
        ("UNAVAILABLE", "Connection reset by peer", False, FT.CONNECTION_RESET),
        (
            "UNAVAILABLE",
            "empty address list: WRONG_VERSION_NUMBER",
            True,
            FT.TLS_OR_HTTP2_MISMATCH,
        ),
        ("DEADLINE_EXCEEDED", "", False, FT.TIMEOUT),
        ("DEADLINE_EXCEEDED", "", True, FT.CONNECT_TIMEOUT),
        ("UNIMPLEMENTED", "Method not found", False, FT.PROTOCOL_ERROR),
        ("PERMISSION_DENIED", "LAN peers only", False, FT.SERVER_ERROR),
        ("UNAVAILABLE", "something new we have never seen", False, FT.STREAM_DROPPED),
        ("UNAVAILABLE", "something new we have never seen", True, FT.CONNECT_TIMEOUT),
    ],
)
def test_grpc_statuses_are_classified(
    code: str, details: str, connecting: bool, expected: proto.FailureType
) -> None:
    error = proto.classify_grpc_error(
        code, details, "debug string", "gRPC Session", connecting=connecting
    )
    assert error.failure_type is expected
    assert error.detail
    assert error.fix


def test_reset_during_connect_is_not_reported_as_an_idle_drop() -> None:
    error = proto.classify_grpc_error(
        "UNAVAILABLE", "Socket closed", "", "gRPC connect", connecting=True
    )
    assert error.failure_type is FT.CONNECTION_RESET


def test_every_failure_type_has_a_fix() -> None:
    assert set(proto.FIX_HINT) == set(proto.FailureType)


def test_fatal_failures_stop_a_case_early() -> None:
    fatal = proto.ProbeError(FT.CONNECT_TIMEOUT, "blocked")
    assert fatal.fatal is True
    assert proto.ProbeError(FT.TIMEOUT, "slow").fatal is False


def test_probe_error_json_is_self_describing() -> None:
    doc = proto.ProbeError(FT.STREAM_DROPPED, "gone", detail="raw").to_json()
    assert doc["failure_type"] == "stream_dropped"
    assert doc["detail"] == "raw"
    assert "keepalive" in doc["fix"]


# ------------------------------------------------------------- peer parsing
@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        ("ipv4:192.168.1.20:51234", "192.168.1.20"),
        ("ipv6:[::1]:51234", "::1"),
        ("ipv6:[fe80::1%eth0]:51234", "fe80::1%eth0"),
        ("unix:/tmp/sock", None),
        ("", None),
        ("ipv4:192.168.1.20", None),
    ],
)
def test_peer_address_parsing(peer: str, expected: str | None) -> None:
    assert peer_address(peer) == expected
