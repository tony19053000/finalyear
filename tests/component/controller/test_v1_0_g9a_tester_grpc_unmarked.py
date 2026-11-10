"""G9-A D430 (tester): the gRPC session port is never marked CS1.

Spec: "Never on the gRPC session port" - heartbeats are what CS1 protects.
Read ``IP_TOS`` of the real gRPC listening socket (found by port through this
process's own fds) while the HTTP port is marked; also with ``dscp_bulk = cs1``.
"""

from __future__ import annotations

import os
import socket
import sys

import pytest
from mf_controller_harness import make_settings

from meshforge.common.dscp import CS1_TOS
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(60),
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only"),
]


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_settings(pg_settings, http={"dscp_bulk": "cs1"})


def _listener_tos(port: int) -> list[int]:
    found: list[int] = []
    for name in os.listdir("/proc/self/fd"):
        try:
            fd = os.dup(int(name))
        except OSError:
            continue
        try:
            sock = socket.socket(fileno=fd)
        except OSError:
            os.close(fd)
            continue
        with sock:
            try:
                if (
                    sock.type == socket.SOCK_STREAM
                    and sock.family in (socket.AF_INET, socket.AF_INET6)
                    and sock.getsockname()[1] == port
                    and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                ):
                    found.append(sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS))
            except OSError:
                continue
    return found


async def test_grpc_listener_is_unmarked_while_http_is_cs1(
    runtime: ControllerRuntime,
) -> None:
    http = _listener_tos(runtime.http_port)
    grpc_ = _listener_tos(runtime.grpc_port)
    assert http and set(http) == {CS1_TOS}, http
    assert grpc_, "gRPC listening socket not found"
    assert set(grpc_) == {0}, grpc_
