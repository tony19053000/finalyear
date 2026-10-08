"""V0.7 G3 deliverable 3 (0.7.5, D137) from the agent's side, with the real
``AgentSessionClient``:

* CG-S3 (rogue controller): a gRPC server whose certificate is from another
  CA, or is an enrolled **node's** certificate from our own CA (it has
  ``serverAuth`` but no ``meshforge-controller`` name), never sees a Hello -
  the agent refuses the handshake and keeps retrying;
* the agent finds the real controller through any address (the certificate is
  checked against ``meshforge-controller``, not the dialled IP): here via
  ``localhost``, which is not in the server certificate's SANs;
* an agent that is not enrolled (or whose certificate is not valid now) stops
  at once with the exact ``meshforge-agent join`` remedy - before any network
  activity, never a plaintext fallback.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import grpc
import pytest
from mf_agent_harness import AgentFactory, make_agent_settings
from mf_fabric import fabric_ca

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.identity import load_or_create
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.state import AgentState
from meshforge.agent.session.tls import SessionTlsError
from meshforge.common import pki
from meshforge.common.ids import new_fabric_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]


class _Spy(agent_pb2_grpc.AgentControlServicer):
    def __init__(self) -> None:
        self.sessions = 0

    async def Session(self, request_iterator: Any, context: Any) -> Any:  # noqa: N802
        self.sessions += 1
        async for _ in request_iterator:
            break
        if False:  # pragma: no cover - makes this an async generator
            yield agent_pb2.ControllerMessage()


async def _rogue(key: Any, cert_chain: bytes) -> tuple[grpc.aio.Server, int, _Spy]:
    spy = _Spy()
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentControlServicer_to_server(spy, server)
    credentials = grpc.ssl_server_credentials(
        [(pki.key_to_pem(key), cert_chain)], require_client_auth=False
    )
    port = server.add_secure_port("127.0.0.1:0", credentials)
    await server.start()
    return server, port, spy


def _other_ca_server() -> tuple[Any, bytes]:
    other = pki.create_ca(new_fabric_id())
    issued = pki.issue_server_cert(other, hosts=["127.0.0.1"])
    return issued.key, pki.cert_to_pem(issued.cert) + pki.cert_to_pem(other.cert)


def _enrolled_node_as_server() -> tuple[Any, bytes]:
    from meshforge.common.ids import new_node_id

    ca = fabric_ca()
    key = pki.generate_key()
    node = str(new_node_id())
    cert = pki.issue_node_cert(
        ca,
        pki.build_csr(key, fabric_id=ca.fabric_id, node_id=node),
        node_id=node,
        fabric_id=ca.fabric_id,
    )
    return key, pki.cert_to_pem(cert) + pki.cert_to_pem(ca.cert)


@pytest.mark.parametrize("rogue", ["other_ca", "enrolled_node"])
async def test_agent_never_says_hello_to_a_rogue_controller(
    agents: AgentFactory, rogue: str
) -> None:
    key, chain = (
        _other_ca_server() if rogue == "other_ca" else _enrolled_node_as_server()
    )
    server, port, spy = await _rogue(key, chain)
    try:
        running = await agents(f"127.0.0.1:{port}")
        await asyncio.sleep(1.5)  # several connection attempts (backoff 0.1-2 s)
        assert running.client.stats.cycles >= 2
        assert running.client.state is not AgentState.ONLINE
        assert running.client.stats.sessions_accepted == 0
        assert spy.sessions == 0, "the rogue server received a stream"
        assert not running.task.done()  # keeps retrying, does not crash
    finally:
        await server.stop(None)


async def test_agent_verifies_the_name_not_the_dialled_address(
    runtime: ControllerRuntime, agents: AgentFactory
) -> None:
    running = await agents(f"localhost:{runtime.grpc_port}")
    await running.client.wait_for_state(AgentState.ONLINE, timeout_s=10)


async def test_unenrolled_agent_stops_with_the_join_command(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    settings = make_agent_settings(
        tmp_path / "fresh", f"127.0.0.1:{runtime.grpc_port}", enroll=False
    )
    identity = load_or_create(settings.identity_dir)
    client = AgentSessionClient(settings, identity)
    with pytest.raises(SessionTlsError) as info:
        await asyncio.wait_for(client.run(), 5)
    assert "meshforge-agent join" in info.value.message
    assert client.stats.cycles == 0  # refused before any connection attempt
    assert client.state is AgentState.STOPPED


async def test_agent_with_an_expired_certificate_stops_with_the_rejoin_command(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    from meshforge.agent import enrollment

    settings = make_agent_settings(
        tmp_path / "old", f"127.0.0.1:{runtime.grpc_port}", enroll=False
    )
    identity = load_or_create(settings.identity_dir)
    now = dt.datetime.now(dt.UTC)
    old_ca = pki.create_ca(new_fabric_id(), now=now - dt.timedelta(days=2))
    cert = pki.issue_node_cert(
        old_ca,
        pki.build_csr(
            identity.private_key, fabric_id=old_ca.fabric_id, node_id=identity.node_id
        ),
        node_id=identity.node_id,
        fabric_id=old_ca.fabric_id,
        now=now - dt.timedelta(days=1),
        lifetime=dt.timedelta(hours=1),
    )
    enrollment.store(identity, cert, old_ca.cert, force=False)
    client = AgentSessionClient(settings, identity)
    with pytest.raises(SessionTlsError) as info:
        await asyncio.wait_for(client.run(), 5)
    assert "join --force" in info.value.message
