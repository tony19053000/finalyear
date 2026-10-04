"""V0.5 G4 D97: the REST listener is created with proto IPPROTO_TCP, so asyncio
turns TCP_NODELAY on for every accepted connection (the ~40 ms delayed-ACK stall
on small responses came from proto 0). Real loopback sockets, no controller."""

from __future__ import annotations

import socket
import sys

import pytest

from meshforge.common.netaddr import parse_bind
from meshforge.controller.server import _bind_socket

pytestmark = pytest.mark.unit


def test_listener_socket_is_tcp_proto() -> None:
    sock = _bind_socket(parse_bind("127.0.0.1:0"))
    try:
        assert sock.proto == socket.IPPROTO_TCP
    finally:
        sock.close()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="accept() proto on POSIX")
@pytest.mark.asyncio
async def test_accepted_connections_get_nodelay() -> None:
    import asyncio

    sock = _bind_socket(parse_bind("127.0.0.1:0"))
    sock.listen(4)
    seen: list[int] = []

    async def on_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        raw = writer.get_extra_info("socket")
        seen.append(raw.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))
        writer.close()

    server = await asyncio.start_server(on_client, sock=sock)
    try:
        port = sock.getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await reader.read()
        writer.close()
    finally:
        server.close()
        await server.wait_closed()
    assert seen and seen[0] != 0
