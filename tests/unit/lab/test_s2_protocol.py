"""Unit tests for ``lab/spikes/_s2/protocol.py``: wire contract, keepalive
option sets, and the typed-failure classification tables (roadmap 0.2.6)."""

from __future__ import annotations

import errno
import socket

import pytest

from spikes._s2 import protocol as p


# --------------------------------------------------------------- encode/decode
def test_encode_decode_roundtrip() -> None:
    msg = {"op": "ping", "seq": 3}
    assert p.decode(p.encode(msg)) == msg


def test_encode_rejects_oversized_message() -> None:
    huge = {"op": "x", "pad": "a" * (p.MAX_MESSAGE_BYTES + 1)}
    with pytest.raises(p.ProtocolError):
        p.encode(huge)


def test_decode_rejects_oversized_body_without_parsing() -> None:
    body = b"a" * (p.MAX_MESSAGE_BYTES + 1)
    with pytest.raises(p.ProtocolError):
        p.decode(body)


def test_decode_rejects_non_utf8() -> None:
    with pytest.raises(p.ProtocolError):
        p.decode(b"\xff\xfe\x00\x01")


def test_decode_rejects_non_json() -> None:
    with pytest.raises(p.ProtocolError):
        p.decode(b"not json at all")


def test_decode_rejects_json_that_is_not_an_object() -> None:
    with pytest.raises(p.ProtocolError):
        p.decode(b"[1, 2, 3]")
    with pytest.raises(p.ProtocolError):
        p.decode(b'"a string"')
    with pytest.raises(p.ProtocolError):
        p.decode(b"42")


# ------------------------------------------------------------------ blob_size
@pytest.mark.parametrize(
    "value", [p.MIN_BLOB_BYTES, p.MAX_BLOB_BYTES, 1024, p.MIN_BLOB_BYTES + 0.0]
)
def test_blob_size_accepts_in_range(value: int | float) -> None:
    assert p.blob_size(value) == int(value)


@pytest.mark.parametrize(
    "value",
    [
        p.MIN_BLOB_BYTES - 1,
        p.MAX_BLOB_BYTES + 1,
        -1,
        "1024",
        None,
        True,
        False,
        [1024],
    ],
)
def test_blob_size_rejects_out_of_range_or_wrong_type(value: object) -> None:
    with pytest.raises(p.ProtocolError):
        p.blob_size(value)


def test_blob_size_nan_is_a_protocol_error() -> None:
    """(Was a defect.) ``blob_size``'s docstring promises it "raises :class:`ProtocolError`" for
    any invalid ``bytes`` value, and callers rely on that: ``grpcsrv._download``
    catches only ``ProtocolError`` around it (``httpsrv._blob`` happens to also
    catch bare ``ValueError``, but the gRPC path does not). ``float('nan')``
    passes the ``isinstance(value, int | float)`` check (NaN *is* a float) and
    then ``int(value)`` raises a plain, unclassified ``ValueError`` -
    unhandled by ``S2GrpcServer._download``, so a peer sending
    ``{"op": "download", "bytes": NaN}`` (accepted by ``json.loads``, which
    parses the non-standard ``NaN`` literal even though this module's own
    ``encode()`` forbids emitting it) crashes the RPC handler with an
    unclassified exception instead of a typed ``INVALID_ARGUMENT`` abort.
    Non-finite values are now rejected as ``ProtocolError`` like any other.
    """
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(p.ProtocolError, match="finite"):
            p.blob_size(value)


# ------------------------------------------------------------------ keepalive
def test_channel_options_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="keepalive must be one of"):
        p.channel_options("maybe")


def test_channel_options_on_includes_permit_without_calls() -> None:
    opts = dict(p.channel_options("on"))
    assert opts.get("grpc.keepalive_permit_without_calls") == 1


def test_channel_options_off_sends_no_keepalive_pings() -> None:
    opts = dict(p.channel_options("off"))
    assert "grpc.keepalive_time_ms" not in opts


def test_server_options_tolerates_client_pings() -> None:
    opts = dict(p.server_options())
    assert opts["grpc.keepalive_permit_without_calls"] == 1


def test_keepalive_settings_reports_both_sides_and_mode() -> None:
    settings = p.keepalive_settings("on")
    assert settings["mode"] == "on"
    assert "grpc.keepalive_time_ms" in settings["client_options"]
    assert "grpc.keepalive_time_ms" in settings["server_options"]


# ------------------------------------------------------- classify_os_error
def test_classify_connection_refused() -> None:
    err = p.classify_os_error(ConnectionRefusedError(), "connect", connecting=True)
    assert err.failure_type == p.FailureType.CONNECTION_REFUSED
    assert err.fatal


def test_classify_timeout_while_connecting_is_connect_timeout() -> None:
    err = p.classify_os_error(TimeoutError(), "connect", connecting=True)
    assert err.failure_type == p.FailureType.CONNECT_TIMEOUT
    assert err.fatal


def test_classify_timeout_mid_transfer_is_not_fatal() -> None:
    err = p.classify_os_error(TimeoutError(), "read", connecting=False)
    assert err.failure_type == p.FailureType.TIMEOUT
    assert not err.fatal


def test_classify_connection_reset_while_connecting_vs_mid_transfer() -> None:
    connecting = p.classify_os_error(ConnectionResetError(), "connect", connecting=True)
    mid = p.classify_os_error(ConnectionResetError(), "read", connecting=False)
    assert connecting.failure_type == p.FailureType.CONNECTION_RESET
    assert mid.failure_type == p.FailureType.STREAM_DROPPED


def test_classify_host_unreachable_errno() -> None:
    exc = OSError()
    exc.errno = errno.EHOSTUNREACH
    err = p.classify_os_error(exc, "connect", connecting=True)
    assert err.failure_type == p.FailureType.HOST_UNREACHABLE
    assert err.fatal


def test_classify_gaierror_is_host_unreachable() -> None:
    err = p.classify_os_error(socket.gaierror("nope"), "connect", connecting=True)
    assert err.failure_type == p.FailureType.HOST_UNREACHABLE


def test_classify_generic_oserror_distinguishes_connect_vs_transfer() -> None:
    connecting = p.classify_os_error(OSError("weird"), "connect", connecting=True)
    mid = p.classify_os_error(OSError("weird"), "read", connecting=False)
    assert connecting.failure_type == p.FailureType.CONNECT_TIMEOUT
    assert mid.failure_type == p.FailureType.STREAM_DROPPED


def test_classify_unknown_exception_is_protocol_error() -> None:
    err = p.classify_os_error(ValueError("huh"), "parse", connecting=False)
    assert err.failure_type == p.FailureType.PROTOCOL_ERROR


def test_every_failure_type_has_a_fix_hint() -> None:
    for kind in p.FailureType:
        assert kind in p.FIX_HINT
        assert p.FIX_HINT[kind]


# ---------------------------------------------------- classify_grpc_error
def test_grpc_marker_priority_ssl_before_generic_timeout() -> None:
    """'handshake' is a TLS/HTTP2-mismatch marker; it must win even though the
    detail string also happens to contain a generic word list order matters
    for: the specific transport causes must be checked before the generic
    ones (module docstring)."""
    err = p.classify_grpc_error(
        "UNAVAILABLE", "ssl handshake failed", "", "connect", connecting=True
    )
    assert err.failure_type == p.FailureType.TLS_OR_HTTP2_MISMATCH


def test_grpc_marker_too_many_pings_is_stream_dropped() -> None:
    err = p.classify_grpc_error(
        "UNAVAILABLE", "", "too_many_pings received", "session", connecting=False
    )
    assert err.failure_type == p.FailureType.STREAM_DROPPED


def test_grpc_stream_dropped_marker_while_connecting_becomes_connection_reset() -> None:
    err = p.classify_grpc_error(
        "UNAVAILABLE", "goaway received", "", "connect", connecting=True
    )
    assert err.failure_type == p.FailureType.CONNECTION_RESET


def test_grpc_connect_timeout_marker_while_not_connecting_becomes_timeout() -> None:
    err = p.classify_grpc_error(
        "DEADLINE_EXCEEDED", "timed out waiting", "", "read", connecting=False
    )
    assert err.failure_type == p.FailureType.TIMEOUT


def test_grpc_deadline_exceeded_without_marker() -> None:
    connecting = p.classify_grpc_error(
        "DEADLINE_EXCEEDED", "", "", "connect", connecting=True
    )
    mid = p.classify_grpc_error("DEADLINE_EXCEEDED", "", "", "read", connecting=False)
    assert connecting.failure_type == p.FailureType.CONNECT_TIMEOUT
    assert mid.failure_type == p.FailureType.TIMEOUT


def test_grpc_unavailable_without_marker() -> None:
    connecting = p.classify_grpc_error(
        "UNAVAILABLE", "", "", "connect", connecting=True
    )
    mid = p.classify_grpc_error("UNAVAILABLE", "", "", "read", connecting=False)
    assert connecting.failure_type == p.FailureType.CONNECT_TIMEOUT
    assert mid.failure_type == p.FailureType.STREAM_DROPPED


def test_grpc_protocol_error_codes() -> None:
    for code in ("UNIMPLEMENTED", "INVALID_ARGUMENT", "INTERNAL", "UNKNOWN"):
        err = p.classify_grpc_error(code, "", "", "call", connecting=False)
        assert err.failure_type == p.FailureType.PROTOCOL_ERROR


def test_grpc_unrecognised_code_is_server_error() -> None:
    err = p.classify_grpc_error("PERMISSION_DENIED", "", "", "call", connecting=False)
    assert err.failure_type == p.FailureType.SERVER_ERROR


def test_grpc_marker_matching_is_case_insensitive_via_lowercasing() -> None:
    err = p.classify_grpc_error(
        "UNAVAILABLE", "Connection Refused", "", "connect", connecting=True
    )
    assert err.failure_type == p.FailureType.CONNECTION_REFUSED


def test_probe_error_to_json_round_trip_shape() -> None:
    err = p.ProbeError(p.FailureType.TIMEOUT, "boom", detail="d")
    doc = err.to_json()
    assert doc["failure_type"] == "timeout"
    assert doc["fatal"] is False
    assert doc["fix"] == p.FIX_HINT[p.FailureType.TIMEOUT]


def test_fatal_set_matches_documented_three_types() -> None:
    assert {
        p.FailureType.CONNECTION_REFUSED,
        p.FailureType.CONNECT_TIMEOUT,
        p.FailureType.HOST_UNREACHABLE,
    } == p.FATAL
