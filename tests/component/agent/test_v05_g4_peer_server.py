"""V0.5 G4 D97-D99 with a real controller (PostgreSQL + gRPC + REST) and a real
agent whose peer blob server listens on loopback.

Checked: the agent advertises its endpoint (Hello.peer_endpoint) and the REST
node detail shows it; the agent installs the controller's peer-token key from
HelloAck; a token the controller signs reads the tree's blobs from the peer,
while a forged, expired, other-holder or other-tree token gets 403.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from meshforge.common.fabric_tls import peer_server_name

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent
from mf_fabric import peer_client_context
from mf_jobs_harness import token_of

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.common.peer_token import sign_peer_token
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.peer_signing import (
    PeerTokenIssuer,
    load_or_create_signing_key,
)
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(60)]

PEER = {"peer": {"bind": "127.0.0.1", "port": 0}}


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(pg_settings, **tolerant_liveness())


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir, agent_overrides=PEER)
    yield running
    await running.stop()


def _tree(root: Path, name: str, data: bytes) -> BuiltTree:
    root.mkdir()
    (root / name).write_bytes(data)
    return build_tree(root, chunk_size=MIN_CHUNK_SIZE)


def _hold(agent: ExecAgent, built: BuiltTree) -> None:
    store = agent.executor.cas.store
    store.prepare()
    for hex_, path in built.sources.items():
        store.put_file(path, Digest(hex_))
    store.put_bytes(built.manifest, built.tree.tree_id)


async def test_endpoint_advertised_key_installed_and_tokens_checked(
    runtime: ControllerRuntime, agent: ExecAgent, tmp_path: Path
) -> None:
    assert agent.peer is not None
    endpoint = f"127.0.0.1:{agent.peer.port}"
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        trust_env=False,
    ) as http:
        detail = (await http.get(f"/v1/nodes/{agent.node_id}")).json()
    assert detail["peer_endpoint"] == endpoint
    assert runtime.manager is not None
    live = runtime.manager.get(agent.node_id)
    assert live is not None and live.peer_endpoint == endpoint

    x = _tree(tmp_path / "x", "a.bin", os.urandom(MIN_CHUNK_SIZE * 2 + 5))
    y = _tree(tmp_path / "y", "secret.txt", b"only in Y\n")
    _hold(agent, x)
    _hold(agent, y)
    issuer = runtime.peer_tokens
    assert issuer is not None

    def token(tree: Digest = x.tree.tree_id, holder: str = agent.node_id) -> str:
        return issuer.issue(
            tree=tree,
            requester=new_node_id(),
            holder=holder,  # type: ignore[arg-type]
            attempt_id=new_attempt_id(),
        )

    blob_x = next(iter(x.tree.files())).digest
    blob_y = next(iter(y.tree.files())).digest
    expired = PeerTokenIssuer(
        # The controller's own key (its file), but issued 20 minutes ago.
        load_or_create_signing_key(runtime.settings.peer_key_path)[0],
        ttl_s=600,
        clock=lambda: time.time() - 1200,
    ).issue(
        tree=x.tree.tree_id,
        requester=new_node_id(),
        holder=agent.node_id,  # type: ignore[arg-type]
        attempt_id=new_attempt_id(),
    )
    forged = sign_peer_token(
        Ed25519PrivateKey.generate(),
        tree=x.tree.tree_id,
        requester=new_node_id(),
        holder=agent.node_id,  # type: ignore[arg-type]
        attempt_id=new_attempt_id(),
        now=time.time(),
    )
    base = f"https://{endpoint}/v1/blobs"
    async with httpx.AsyncClient(
        trust_env=False, verify=peer_client_context(), timeout=10
    ) as peer:

        async def get(digest: Digest, tok: str) -> int:
            r = await peer.get(
                f"{base}/{digest.hex}",
                headers={"authorization": f"Bearer {tok}"},
                extensions={"sni_hostname": peer_server_name(agent.node_id)},
            )
            if r.status_code == 200:
                assert sha256_bytes(r.content) == digest
            return r.status_code

        assert await get(blob_x, token()) == 200
        assert await get(x.tree.tree_id, token()) == 200
        assert await get(blob_x, forged) == 403
        assert await get(blob_x, expired) == 403
        assert await get(blob_x, token(holder=new_node_id())) == 403
        assert await get(blob_y, token()) == 403  # tree X token, blob only in Y
        assert await get(blob_y, token(tree=y.tree.tree_id)) == 200
