"""V1.0 G9-A item 3 (D430): the controller's REST/blob port sends CS1.

A real :class:`ControllerRuntime` (PostgreSQL, uvicorn, TLS): a client opens
a TLS connection to the HTTP port and the test reads ``IP_TOS`` of the socket
uvicorn **accepted** for it - so the inheritance from the listening socket is
proven on the real server, not assumed. ``dscp_bulk = "off"`` leaves 0.
Linux only (elsewhere nothing is marked; Windows needs a QoS policy).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import sys
from typing import Any

import pytest
from mf_controller_harness import make_settings

from meshforge.common.dscp import CS1_TOS
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(60),
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="IP_TOS is set on Linux only"
    ),
]


@pytest.fixture(params=["cs1", "off"])
def settings(
    request: pytest.FixtureRequest, pg_settings: DatabaseSettings
) -> ControllerSettings:
    return make_settings(pg_settings, http={"dscp_bulk": request.param})


def _tls() -> ssl.SSLContext:
    # only the handshake matters here (uvicorn hands a connection to its
    # protocol once TLS is up); what is verified is not under test
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


async def _accepted_socket(runtime: ControllerRuntime) -> Any:
    server = runtime._http_server
    assert server is not None
    for _ in range(500):
        connections = list(server.server_state.connections)
        if connections:
            transport = connections[0].transport
            return transport.get_extra_info("socket")
        await asyncio.sleep(0.01)
    raise AssertionError("uvicorn never registered the connection")


async def test_the_http_port_marks_accepted_sockets_cs1_unless_off(
    runtime: ControllerRuntime, settings: ControllerSettings
) -> None:
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", runtime.http_port, ssl=_tls(), server_hostname="localhost"
    )
    try:
        sock = await _accepted_socket(runtime)
        tos = sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS)
        want = CS1_TOS if settings.controller.http.dscp_bulk == "cs1" else 0
        assert tos == want
        listening = runtime._http_sock
        assert listening is not None
        assert listening.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) == want
        # the client end is the test's own socket: never marked by the server
        client = writer.get_extra_info("socket")
        assert client.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) == 0
        # a real request still works on the marked connection
        writer.write(b"GET /healthz HTTP/1.1\r\nhost: localhost\r\n\r\n")
        await writer.drain()
        status = await asyncio.wait_for(reader.readline(), 10)
        assert status.startswith(b"HTTP/1.1 200")
    finally:
        writer.close()
        # a TLS close may race the server's: not under test
        with contextlib.suppress(OSError, ssl.SSLError):
            await writer.wait_closed()
