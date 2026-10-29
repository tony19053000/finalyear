"""V1.0 G8 F1: a certificate refusal after the TLS 1.3 handshake is told apart
from a network fault (:mod:`meshforge.agent.http_refusal`,
:mod:`meshforge.common.tls_refusal`).

Unit level: a scripted inner stream stands in for the TLS stream so every
branch (EOF, reset, broken pipe, alert, timeout, before/after the first byte)
is exercised deterministically. The real servers are in
``tests/component/agent/test_v10_g8_f1_cert_refused.py``.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import Any

import httpcore
import httpx
import pytest
from structlog.testing import capture_logs

from meshforge.agent.http_refusal import (
    CONTROLLER_LABEL,
    SMALL_REQUEST_BYTES,
    RefusalWatchStream,
    RefusedProtocolError,
    RefusedReadError,
    server_label,
    transport_failure,
)
from meshforge.common.fabric_tls import peer_server_name
from meshforge.common.tls_refusal import (
    CERT_REFUSED_HINT,
    CLOSED_AFTER_HANDSHAKE,
    CertRefused,
    find_refusal,
    refusal_message,
)

NODE = "node_01J0HARNESS000000000000000"


class _Inner(httpcore.AsyncNetworkStream):
    """Reads return / raise the scripted items in order; writes likewise."""

    def __init__(
        self, reads: list[bytes | BaseException], writes: list[BaseException | None]
    ) -> None:
        self.reads = reads
        self.writes = writes
        self.closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        item = self.reads.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        item = self.writes.pop(0) if self.writes else None
        if item is not None:
            raise item

    async def aclose(self) -> None:
        self.closed = True

    def get_extra_info(self, info: str) -> Any:
        return f"extra:{info}"

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        self.tunnelled = _Inner(list(self.reads), [])
        return self.tunnelled


def _stream(
    reads: list[bytes | BaseException],
    writes: list[BaseException | None] | None = None,
    host: str | None = "127.0.0.1",
) -> RefusalWatchStream:
    return RefusalWatchStream(_Inner(reads, writes or []), host)


def _alert(reason: str) -> ssl.SSLError:
    exc = ssl.SSLError(1, f"[SSL: {reason}] alert")
    exc.reason = reason
    return exc


def _reset() -> httpcore.ReadError:
    try:
        raise httpcore.ReadError("reset") from ConnectionResetError(104, "reset")
    except httpcore.ReadError as exc:
        return exc


# ------------------------------------------------------------------ the stream


@pytest.mark.asyncio
async def test_eof_before_any_byte_is_a_marked_remote_protocol_error() -> None:
    with pytest.raises(RefusedProtocolError) as caught:
        await _stream([b""]).read(100)
    assert isinstance(caught.value, httpcore.RemoteProtocolError)
    assert caught.value.reason == CLOSED_AFTER_HANDSHAKE
    assert caught.value.server == "the controller"
    assert CERT_REFUSED_HINT in str(caught.value)


@pytest.mark.asyncio
async def test_eof_after_the_server_answered_is_an_ordinary_eof() -> None:
    stream = _stream([b"HTTP/1.1 200 OK\r\n", b""])
    assert await stream.read(100) == b"HTTP/1.1 200 OK\r\n"
    assert await stream.read(100) == b""  # keep-alive close: not a refusal


@pytest.mark.parametrize(
    "error",
    [
        lambda: _reset(),
        lambda: httpcore.ReadError("broken"),
    ],
)
@pytest.mark.asyncio
async def test_read_error_before_any_byte_is_marked_with_its_cause(
    error: Callable[[], httpcore.ReadError],
) -> None:
    original = error()
    with pytest.raises(RefusedReadError) as caught:
        await _stream([original]).read(100)
    assert isinstance(caught.value, httpcore.ReadError)
    assert caught.value.__cause__ is original


@pytest.mark.asyncio
async def test_read_error_after_the_server_answered_is_untouched() -> None:
    original = _reset()
    stream = _stream([b"x", original])
    await stream.read(1)
    with pytest.raises(httpcore.ReadError) as caught:
        await stream.read(1)
    assert caught.value is original
    assert find_refusal(caught.value) is None


def _epipe() -> httpcore.WriteError:
    try:
        raise httpcore.WriteError("epipe") from BrokenPipeError(32, "Broken pipe")
    except httpcore.WriteError as exc:
        return exc


@pytest.mark.asyncio
async def test_a_write_error_is_never_marked_by_itself() -> None:
    """Tester D1: a server answering early (413) and closing makes our write
    fail although it accepted the certificate."""
    original = _epipe()
    with capture_logs() as logs, pytest.raises(httpcore.WriteError) as caught:
        await _stream([], [original]).write(b"GET / HTTP/1.1\r\n\r\n")
    assert caught.value is original
    assert find_refusal(caught.value) is None
    assert not [e for e in logs if e["event"] == "agent.http.cert_refused"]


@pytest.mark.asyncio
async def test_a_failed_write_then_an_early_answer_is_not_a_refusal() -> None:
    stream = _stream([b"HTTP/1.1 413 Too Large\r\n", b""], [_epipe()])
    with pytest.raises(httpcore.WriteError):
        await stream.write(b"PUT / HTTP/1.1\r\n\r\n")
    assert await stream.read(100) == b"HTTP/1.1 413 Too Large\r\n"
    assert await stream.read(100) == b""


@pytest.mark.parametrize("after", [b"", "reset"])
@pytest.mark.asyncio
async def test_a_failed_write_then_a_zero_byte_read_is_a_refusal(
    after: bytes | str,
) -> None:
    item: bytes | BaseException = _reset() if after == "reset" else b""
    stream = _stream([item], [_epipe()])
    with pytest.raises(httpcore.WriteError) as write_caught:
        await stream.write(b"GET / HTTP/1.1\r\n\r\n")
    assert not isinstance(write_caught.value, CertRefused)
    with pytest.raises(CertRefused) as caught:
        await stream.read(100)
    assert caught.value.reason == CLOSED_AFTER_HANDSHAKE


@pytest.mark.parametrize("after", [b"", "reset"])
@pytest.mark.asyncio
async def test_a_zero_byte_close_after_a_large_request_is_not_marked(
    after: bytes | str,
) -> None:
    """Tester D1: past SMALL_REQUEST_BYTES our own asyncio transport may have
    dropped an early answer (413) the server sent before resetting."""
    item: bytes | BaseException = _reset() if after == "reset" else b""
    stream = _stream([item])
    await stream.write(b"h" * 200)
    await stream.write(b"x" * SMALL_REQUEST_BYTES)
    with capture_logs() as logs:
        if after == "reset":
            with pytest.raises(httpcore.ReadError) as caught:
                await stream.read(100)
            assert find_refusal(caught.value) is None
        else:
            assert await stream.read(100) == b""
    assert not [e for e in logs if e["event"] == "agent.http.cert_refused"]


@pytest.mark.asyncio
async def test_a_request_of_exactly_the_limit_still_counts() -> None:
    stream = _stream([b""])
    await stream.write(b"x" * SMALL_REQUEST_BYTES)
    with pytest.raises(RefusedProtocolError):
        await stream.read(100)


@pytest.mark.asyncio
async def test_a_write_error_after_an_answer_is_untouched() -> None:
    original = httpcore.WriteError("gone")
    stream = _stream([b"x"], [None, original])
    await stream.write(b"a")
    await stream.read(1)
    with pytest.raises(httpcore.WriteError) as caught:
        await stream.write(b"b")
    assert caught.value is original


@pytest.mark.asyncio
async def test_timeouts_are_never_refusals() -> None:
    original = httpcore.ReadTimeout("slow")
    with pytest.raises(httpcore.ReadTimeout) as caught:
        await _stream([original]).read(1)
    assert caught.value is original
    assert find_refusal(caught.value) is None


@pytest.mark.parametrize(
    "reason", ["TLSV1_ALERT_UNKNOWN_CA", "SSLV3_ALERT_CERTIFICATE_EXPIRED"]
)
@pytest.mark.asyncio
async def test_a_refusal_alert_is_marked_with_its_reason(reason: str) -> None:
    with pytest.raises(RefusedReadError) as caught:
        await _stream([_alert(reason)]).read(1)
    assert caught.value.reason == reason
    assert f"TLS alert {reason}" in str(caught.value)


@pytest.mark.asyncio
async def test_an_alert_on_write_is_an_unmarked_write_error_carrying_the_alert() -> (
    None
):
    """The stream does not mark writes; the alert itself (an explicit refusal
    from the server) stays in the chain for :func:`find_refusal`."""
    alert = _alert("TLSV1_ALERT_UNKNOWN_CA")
    with pytest.raises(httpcore.WriteError) as caught:
        await _stream([], [alert]).write(b"x")
    assert not isinstance(caught.value, CertRefused)
    assert caught.value.__cause__ is alert
    refusal = find_refusal(caught.value)
    assert refusal is not None
    assert refusal.reason == "TLSV1_ALERT_UNKNOWN_CA"


@pytest.mark.asyncio
async def test_other_tls_errors_become_network_errors_not_local_oserrors() -> None:
    """anyio lets a raw ``ssl.SSLError`` through httpcore unmapped; to httpx
    callers it would be an ``OSError`` ("cannot store the bundle")."""
    alert = _alert("SSLV3_ALERT_HANDSHAKE_FAILURE")
    with pytest.raises(httpcore.ReadError) as caught:
        await _stream([alert]).read(1)
    assert not isinstance(caught.value, CertRefused)
    assert caught.value.__cause__ is alert
    assert find_refusal(caught.value) is None
    with pytest.raises(httpcore.WriteError):
        await _stream([], [ssl.SSLError(1, "odd")]).write(b"x")


@pytest.mark.asyncio
async def test_the_refusal_is_logged_once_per_connection() -> None:
    stream = _stream([b"", b""])
    with capture_logs() as logs:
        with pytest.raises(RefusedProtocolError):
            await stream.read(1)
        with pytest.raises(RefusedProtocolError):
            await stream.read(1)
    refused = [e for e in logs if e["event"] == "agent.http.cert_refused"]
    assert len(refused) == 1
    assert refused[0]["server"] == "the controller"
    assert refused[0]["log_level"] == "warning"


@pytest.mark.asyncio
async def test_the_wrapper_delegates_the_rest() -> None:
    inner = _Inner([], [])
    stream = RefusalWatchStream(inner, None)
    assert stream.get_extra_info("ssl_object") == "extra:ssl_object"
    await stream.aclose()
    assert inner.closed


# ----------------------------------------------------- recognising and wording


def test_server_label_names_a_peer_or_the_controller() -> None:
    assert server_label(peer_server_name(NODE)) == f"peer node {NODE}"
    assert server_label("127.0.0.1") == "the controller"
    assert server_label(None) == "the controller"


def test_find_refusal_through_httpx_mapping_and_chains() -> None:
    marked = RefusedProtocolError("x")
    marked.mark_refused(server="the controller", reason=CLOSED_AFTER_HANDSHAKE)
    with pytest.raises(httpx.RemoteProtocolError) as caught:
        raise httpx.RemoteProtocolError("Server disconnected") from marked
    assert find_refusal(caught.value) is marked
    text = transport_failure(caught.value)
    assert "most likely refused this laptop's certificate" in text


def test_find_refusal_recognises_a_raw_alert_in_the_context() -> None:
    try:
        try:
            raise _alert("SSLV3_ALERT_CERTIFICATE_EXPIRED")
        except ssl.SSLError:
            raise httpx.ConnectError("handshake") from None
    except httpx.ConnectError as exc:
        # ``from None`` hides the cause, but __context__ still has it
        refusal = find_refusal(exc)
    assert refusal is not None
    assert refusal.reason == "SSLV3_ALERT_CERTIFICATE_EXPIRED"


def test_network_errors_without_a_handshake_are_not_refusals() -> None:
    with pytest.raises(httpx.ConnectError) as caught:
        raise httpx.ConnectError("refused") from ConnectionRefusedError(111, "no")
    assert find_refusal(caught.value) is None
    assert transport_failure(caught.value) == "ConnectError"
    verify = ssl.SSLCertVerificationError(1, "wrong server")
    assert find_refusal(verify) is None  # *we* refused the server: other problem


def test_find_refusal_survives_a_cycle() -> None:
    a, b = ValueError("a"), ValueError("b")
    a.__context__, b.__context__ = b, a
    assert find_refusal(a) is None


def test_refusal_message_wording() -> None:
    refusal = CertRefused()
    refusal.mark_refused(server=f"peer node {NODE}", reason="TLSV1_ALERT_UNKNOWN_CA")
    text = refusal_message(refusal)
    assert text.startswith(f"peer node {NODE} refused this laptop's certificate")
    assert "wrong fabric or expired certificate?" in text


@pytest.mark.asyncio
async def test_tls_inside_a_proxy_tunnel_is_watched_too() -> None:
    """Review point 1: httpcore's HTTPS-proxy path calls start_tls on the
    already-wrapped stream; the inner TLS stream carries the requests."""
    outer = RefusalWatchStream(_Inner([b""], []), "proxy.example")
    inner = await outer.start_tls(ssl.create_default_context(), peer_server_name(NODE))
    assert isinstance(inner, RefusalWatchStream)
    with pytest.raises(RefusedProtocolError) as caught:
        await inner.read(10)
    assert caught.value.server == f"peer node {NODE}"


def test_a_raw_alert_refusal_is_worded_with_the_callers_label() -> None:
    """Review point 3: no marker (the alert reached us outside the watched
    stream) - the caller's label names who refused, not "the server"."""
    with pytest.raises(httpx.ConnectError) as caught:
        raise httpx.ConnectError("x") from _alert("TLSV1_ALERT_UNKNOWN_CA")
    assert find_refusal(caught.value).server == "the server"  # type: ignore[union-attr]
    text = transport_failure(caught.value, server=f"peer node {NODE}")
    assert text.startswith(f"peer node {NODE} refused this laptop's certificate")
    assert transport_failure(caught.value, server=CONTROLLER_LABEL).startswith(
        "the controller refused"
    )


def test_a_transport_marker_keeps_its_own_label() -> None:
    marked = RefusedReadError("x")
    marked.mark_refused(server=f"peer node {NODE}", reason=CLOSED_AFTER_HANDSHAKE)
    with pytest.raises(httpx.ReadError) as caught:
        raise httpx.ReadError("r") from marked
    text = transport_failure(caught.value, server=CONTROLLER_LABEL)
    assert text.startswith(f"peer node {NODE} closed the connection")


def test_a_cas_source_label_names_the_controller_or_the_peer() -> None:
    from meshforge.agent.cas.sources import Source

    class _Src:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

    controller = Source(src=_Src(""), stats=None)  # type: ignore[arg-type]
    peer = Source(src=_Src(NODE), stats=None)  # type: ignore[arg-type]
    assert controller.label == "the controller"
    assert peer.label == f"peer node {NODE}"
