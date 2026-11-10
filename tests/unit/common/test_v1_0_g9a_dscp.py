"""V1.0 G9-A item 3 (D430): :mod:`meshforge.common.dscp` rules.

Platform-independent: the Linux decision is patched, the socket is a fake
whose ``setsockopt`` records or fails. (Real sockets: the component tests
``test_v1_0_g9a_dscp_http.py`` / ``test_v1_0_g9a_dscp_agent.py``.)
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from structlog.testing import capture_logs

from meshforge.common import dscp

pytestmark = pytest.mark.unit


class FakeSock:
    def __init__(self, family: int = socket.AF_INET, *, fail: bool = False) -> None:
        self.family = family
        self.fail = fail
        self.set: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, name: int, value: int) -> None:
        if self.fail:
            raise OSError(92, "Protocol not available")
        self.set.append((level, name, value))


@pytest.fixture
def linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dscp, "dscp_supported", lambda: True)
    monkeypatch.setattr(dscp, "_warned", set())


def _mark(sock: FakeSock, mode: Any, where: str = "t") -> bool:
    return dscp.mark_listener(sock, mode, where=where)  # type: ignore[arg-type]


@pytest.mark.usefixtures("linux")
def test_cs1_marks_an_ipv4_listener_and_an_ipv6_one_both_ways() -> None:
    v4 = FakeSock()
    assert _mark(v4, "cs1") is True
    assert v4.set == [(socket.IPPROTO_IP, socket.IP_TOS, 0x20)]
    if not hasattr(socket, "IPV6_TCLASS"):  # pragma: no cover - not Linux
        return
    v6 = FakeSock(socket.AF_INET6)
    assert _mark(v6, "cs1") is True
    assert (socket.IPPROTO_IPV6, socket.IPV6_TCLASS, 0x20) in v6.set
    assert (socket.IPPROTO_IP, socket.IP_TOS, 0x20) in v6.set


@pytest.mark.usefixtures("linux")
def test_off_sets_nothing() -> None:
    sock = FakeSock()
    assert _mark(sock, "off") is False
    assert sock.set == []
    assert dscp.bulk_socket_options("off", where="t") == ()


@pytest.mark.usefixtures("linux")
def test_a_failing_setsockopt_warns_once_per_place_and_continues() -> None:
    with capture_logs() as logs:
        assert _mark(FakeSock(fail=True), "cs1", "peer") is False
        assert _mark(FakeSock(fail=True), "cs1", "peer") is False
        assert _mark(FakeSock(fail=True), "cs1", "http") is False
    warned = [e for e in logs if e["event"] == "common.dscp.unsupported"]
    assert [e["where"] for e in warned] == ["peer", "http"]
    assert all(e["log_level"] == "warning" for e in warned)


def test_off_linux_nothing_is_set_and_nothing_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dscp, "dscp_supported", lambda: False)
    sock = FakeSock(fail=True)
    with capture_logs() as logs:
        assert _mark(sock, "cs1") is False
        assert dscp.bulk_socket_options("cs1", where="t") == ()
    assert logs == []


@pytest.mark.usefixtures("linux")
def test_client_options_are_left_out_when_the_kernel_refuses_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = socket.socket

    class Refusing(real):  # type: ignore[misc, valid-type]
        def setsockopt(self, *args: Any) -> None:
            raise OSError(92, "Protocol not available")

    monkeypatch.setattr(dscp.socket, "socket", Refusing)
    with capture_logs() as logs:
        assert dscp.bulk_socket_options("cs1", where="fetch") == ()
    assert [e["where"] for e in logs] == ["fetch"]


def test_the_platform_rule() -> None:
    import sys

    assert dscp.dscp_supported() is sys.platform.startswith("linux")
    if sys.platform.startswith("linux"):
        assert dscp.bulk_socket_options("cs1", where="t") == (
            (socket.IPPROTO_IP, socket.IP_TOS, 0x20),
        )
