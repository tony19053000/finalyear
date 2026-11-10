"""V1.0 G9-A item 3 (D430) on real sockets, agent side (Linux only).

* the peer blob server: ``IP_TOS`` of the socket it **accepted** for a real
  client connection is CS1 (``0x20``) - inherited from its listener, marked
  before the first accept - and 0 with ``[agent.peer] dscp_bulk = "off"``;
* the blob-fetch client: a request through the executor's own transport
  (``Executor._transport()``, the HTTPS one every fetch and upload of an
  attempt uses) runs on a socket marked CS1, and 0 with ``[agent.fetch]
  dscp_bulk = "off"``; the request itself still works.
"""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_blob_range_server import BlobRangeServer

from meshforge.agent.cas import AgentCas
from meshforge.agent.execution.executor import Executor
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.agent.execution.selection import execution_offer
from meshforge.agent.http_tls import AgentHttpTls
from meshforge.agent.peer import PeerAuthorizer, PeerBlobServer
from meshforge.agent.settings import AgentSettings, PeerSettings
from meshforge.common.cas import CasStore
from meshforge.common.dscp import CS1_TOS
from meshforge.common.ids import new_command_id, new_node_id

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(60),
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="IP_TOS is set on Linux only"
    ),
]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


def _tos(sock: Any) -> int:
    return int(sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS))


@pytest.mark.parametrize("mode", ["cs1", "off"])
async def test_the_peer_server_marks_its_accepted_sockets(
    tmp_path: Path, mode: str
) -> None:
    cas = AgentCas(CasStore(tmp_path / "cas", max_blob_bytes=1 << 20))
    cas.store.prepare()
    holder = new_node_id()
    cfg = PeerSettings.model_validate(
        {"bind": "127.0.0.1", "port": 0, "dscp_bulk": mode}
    )
    server = PeerBlobServer(
        cas, PeerAuthorizer(cas, node_id=holder), cfg, node_id=holder
    )
    accepted: list[Any] = []
    real = server._on_client

    async def capture(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        accepted.append(writer.get_extra_info("socket"))
        await real(reader, writer)

    server._on_client = capture  # type: ignore[method-assign]
    await server.start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(b"GET /healthz HTTP/1.1\r\nhost: x\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.readline(), 10)  # served on that socket
        assert len(accepted) == 1
        assert _tos(accepted[0]) == (CS1_TOS if mode == "cs1" else 0)
        writer.close()
    finally:
        await server.close()


def _executor(tmp_path: Path, mode: str) -> Executor:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data",
        agent={
            "controller_address": "127.0.0.1:1",
            "execution": {"enabled": True, "allow_unsandboxed": True},
            "fetch": {"dscp_bulk": mode},
        },
    )
    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=None,
        subprocess=RuntimeAvailability("subprocess", True),
    )

    async def report(_name: str, _payload: Any) -> str:
        return str(new_command_id())

    return Executor(
        settings,
        ExecutionSetup(offer=offer, runtimes={}),
        node_id="node_x",
        report=report,
        controller_host=lambda: "127.0.0.1",
        # the HTTPS client of an enrolled agent; the request below is plain
        # HTTP to a loopback test server, so the context only builds it
        http_tls=AgentHttpTls(context=ssl.create_default_context()),
    )


@pytest.mark.parametrize("mode", ["cs1", "off"])
async def test_blob_fetch_client_sockets_are_marked(tmp_path: Path, mode: str) -> None:
    blob = os.urandom(4096)
    server = BlobRangeServer({"ab" * 32: blob})
    await server.start()
    executor = _executor(tmp_path, mode)
    try:
        async with (
            httpx.AsyncClient(**executor._transport()) as client,
            client.stream(
                "GET",
                f"{server.url}/v1/blobs/{'ab' * 32}",
                headers={"authorization": "Bearer tok"},
            ) as response,
        ):
            stream = response.extensions["network_stream"]
            sock = stream.get_extra_info("socket")
            assert sock is not None
            assert _tos(sock) == (CS1_TOS if mode == "cs1" else 0)
            body = await response.aread()
        assert response.status_code == 200 and body == blob
        # the server's accepted end is the test server's: not marked by it
        assert _tos(server.accepted[0]) == 0
    finally:
        await executor.shutdown()
        await server.close()
