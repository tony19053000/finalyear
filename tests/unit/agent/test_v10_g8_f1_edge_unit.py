"""V1.0 G8 F1 tester: unit-level edge cases."""

from __future__ import annotations

import asyncio
import ssl

import httpcore
import pytest
from test_v10_g8_f1_http_refusal import _Inner

from meshforge.agent.http_refusal import RefusalWatchStream, transport_failure
from meshforge.common.tls_refusal import CertRefused, find_refusal

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_cancelled_error_passes_through_and_does_not_mark() -> None:
    s = RefusalWatchStream(_Inner([asyncio.CancelledError()], []), "127.0.0.1")
    with pytest.raises(asyncio.CancelledError):
        await s.read(10)


@pytest.mark.asyncio
async def test_non_refusal_ssl_reasons_are_unmarked() -> None:
    for reason in (
        "DECRYPTION_FAILED_OR_BAD_RECORD_MAC",
        "TLSV1_ALERT_PROTOCOL_VERSION",
        "SSLV3_ALERT_HANDSHAKE_FAILURE",
        "UNEXPECTED_EOF_WHILE_READING",
        None,
    ):
        e = ssl.SSLError(1, "x")
        e.reason = reason  # type: ignore[attr-defined]
        s = RefusalWatchStream(_Inner([e], []), "127.0.0.1")
        with pytest.raises(httpcore.ReadError) as c:
            await s.read(1)
        assert find_refusal(c.value) is None, reason
        assert transport_failure(c.value) == "ReadError"


@pytest.mark.asyncio
async def test_ssl_eof_error_before_any_byte() -> None:
    """Python raises SSLEOFError (reason UNEXPECTED_EOF_WHILE_READING) on an
    unclean close; httpcore normally maps that before us. Must stay ordinary or
    be marked, never crash."""
    e = ssl.SSLEOFError(8, "EOF occurred in violation of protocol")
    s = RefusalWatchStream(_Inner([e], []), "127.0.0.1")
    with pytest.raises(httpcore.ReadError):
        await s.read(1)


def test_long_and_mutual_cycles() -> None:
    a, b, c = ValueError("a"), KeyError("b"), OSError("c")
    a.__cause__, b.__context__, c.__cause__ = b, c, a
    assert find_refusal(a) is None
    assert transport_failure(a) == "ValueError"


def test_deep_chain_with_refusal_beyond_limit_is_ignored_safely() -> None:
    root: BaseException = CertRefused()
    for _ in range(40):
        nxt = RuntimeError("x")
        nxt.__cause__ = root
        root = nxt
    find_refusal(root)  # must terminate; result unspecified


@pytest.mark.asyncio
async def test_answered_flag_is_per_connection() -> None:
    a = RefusalWatchStream(_Inner([b"x"], []), "127.0.0.1")
    await a.read(1)
    b = RefusalWatchStream(_Inner([httpcore.ReadError("r")], []), "127.0.0.1")
    with pytest.raises(CertRefused):
        await b.read(1)


@pytest.mark.asyncio
@pytest.mark.parametrize(("size", "marked"), [(65536, True), (65537, False), (0, True)])
async def test_small_request_boundary(size: int, marked: bool) -> None:
    from meshforge.agent.http_refusal import SMALL_REQUEST_BYTES

    assert SMALL_REQUEST_BYTES == 65536
    s = RefusalWatchStream(_Inner([b""], []), "127.0.0.1")
    if size:
        await s.write(b"x" * size)
    if marked:
        with pytest.raises(CertRefused):
            await s.read(1)
    else:
        assert await s.read(1) == b""


@pytest.mark.asyncio
async def test_bytes_count_cumulatively_over_several_writes() -> None:
    s = RefusalWatchStream(_Inner([httpcore.ReadError("r")], []), "127.0.0.1")
    for _ in range(3):
        await s.write(b"x" * 30000)  # 90000 total
    with pytest.raises(httpcore.ReadError) as c:
        await s.read(1)
    assert find_refusal(c.value) is None


@pytest.mark.asyncio
async def test_write_errors_never_marked() -> None:
    for exc in (BrokenPipeError(), ConnectionResetError()):
        err = httpcore.WriteError("w")
        err.__cause__ = exc
        s = RefusalWatchStream(_Inner([], [err]), "127.0.0.1")
        with pytest.raises(httpcore.WriteError) as c:
            await s.write(b"x")
        assert find_refusal(c.value) is None


@pytest.mark.asyncio
async def test_after_answer_big_cumulative_irrelevant_and_small_after_answer_unmarked() -> (
    None
):
    s = RefusalWatchStream(_Inner([b"HTTP", httpcore.ReadError("r")], []), "127.0.0.1")
    await s.write(b"x")
    await s.read(10)
    with pytest.raises(httpcore.ReadError) as c:
        await s.read(10)
    assert find_refusal(c.value) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("extra", "marked"), [(0, True), (1, False)])
async def test_exact_boundary_via_two_writes_then_reset(
    extra: int, marked: bool
) -> None:
    """64 KiB written in two writes then a reset: marked; 64 KiB + 1: not."""
    s = RefusalWatchStream(_Inner([httpcore.ReadError("r")], []), "127.0.0.1")
    await s.write(b"h" * 100)
    await s.write(b"b" * (65536 - 100 + extra))
    with pytest.raises(httpcore.ReadError) as c:
        await s.read(1)
    assert (find_refusal(c.value) is not None) is marked


@pytest.mark.asyncio
async def test_second_request_eof_after_answer_not_marked() -> None:
    s = RefusalWatchStream(_Inner([b"HTTP/1.1 200", b""], []), "127.0.0.1")
    await s.write(b"GET")
    assert await s.read(20)
    await s.write(b"GET")
    assert await s.read(20) == b""
