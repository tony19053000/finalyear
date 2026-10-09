"""V0.7 G3 deliverable 5 (D147) end to end: a real controller (PostgreSQL +
mTLS gRPC + HTTPS) and two real agents with TLS peer servers.

A holds the tree; B runs a job on it. Peer tokens live 6 s here
(``[controller.jobs] peer_token_ttl_s``, the production cap is 60 s) and the
verifier's skew allowance is set to 0 for this test, so a token is refused
6 s after it was minted. A's peer server answers slowly and the controller's
link is throttled, so B's transfer lasts well over one token life and most
bytes come from A. The dispatcher's ``RefreshPeerTokens`` every 2 s keeps
every range B asks of A on a fresh token: A refuses nothing, A is never
dropped, the job succeeds byte-exact.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, wait_job
from mf_fabric import client_ssl_context
from mf_faultproxy import FaultLink, FaultLoop
from mf_jobs_harness import HARNESS_NODE_ID, token_of
from pydantic import SecretStr

from meshforge.agent.peer import server as peer_server
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common import peer_token
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(180)]

CS = MIN_CHUNK_SIZE
CHUNKS = 40
TTL_S = 6
PEER_DELAY_S = 0.4

MAIN = b"""\
import hashlib, sys
data = open("data.bin", "rb").read()
open(sys.argv[1], "w").write(hashlib.sha256(data).hexdigest())
"""


@pytest.fixture
def fault_loop() -> Iterator[FaultLoop]:
    loop = FaultLoop("g3-peer-remint")
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture
def ctl_link(fault_loop: FaultLoop) -> FaultLink:
    return fault_loop.link(("127.0.0.1", 9), name="controller-blobs")


@pytest.fixture
def settings(pg_settings: DatabaseSettings, ctl_link: FaultLink) -> ControllerSettings:
    host, port = ctl_link.address
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "peer_token_ttl_s": TTL_S},
        blobs={"max_blob_bytes": 64 * 1024 * 1024},
        api={"advertise_url": f"https://{host}:{port}"},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def wired(
    runtime: ControllerRuntime, ctl_link: FaultLink
) -> AsyncIterator[ControllerRuntime]:
    ctl_link.retarget("127.0.0.1", runtime.http_port)
    yield runtime


async def _agent(grpc_target: str, data_dir: Path) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        execution={"staging_retry_s": 30},
        agent_overrides={
            "peer": {"bind": "127.0.0.1", "port": 0},
            "cache": {"flush_s": 0.1},
            "fetch": {
                "per_source": 2,
                "endgame_after_s": 600.0,
                "slow_min_units": 1000,  # A is slow on purpose: never dropped for it
            },
        },
    )


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
        trust_env=False,
        verify=client_ssl_context(node_id=HARNESS_NODE_ID),
    )


async def _submit(
    http: httpx.AsyncClient, built: BuiltTree, node_id: str, out: Path
) -> dict[str, Any]:
    posted = await http.post(
        "/v1/jobs",
        json={
            "manifest": {
                "name": "remint",
                "entrypoint": ["python", "main.py", str(out)],
            },
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": node_id},
        },
    )
    assert posted.status_code == 201, posted.text
    return await wait_job(http, str(posted.json()["job_id"]), timeout_s=120)


async def test_a_peer_transfer_longer_than_the_token_life_runs_on_reminted_tokens(
    wired: ControllerRuntime,
    grpc_target: str,
    ctl_link: FaultLink,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peer_token, "SKEW_S", 0)  # a token dies TTL_S after minting
    root = tmp_path / "p"
    root.mkdir()
    (root / "main.py").write_bytes(MAIN)
    data = os.urandom(CHUNKS * CS)
    (root / "data.bin").write_bytes(data)
    built = build_tree(root, chunk_size=CS)
    client = ControllerClient(
        f"https://127.0.0.1:{wired.http_port}",
        timeout_s=30,
        token=SecretStr(token_of(wired)),
    )
    try:
        await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()
    a = await _agent(grpc_target, tmp_path / "agent-a")
    b = await _agent(grpc_target, tmp_path / "agent-b")
    try:
        async with _http(wired) as http:
            first = await _submit(http, built, a.node_id, tmp_path / "a.out")
            assert first["state"] == "succeeded", first
            deadline = time.monotonic() + 30
            assert wired.cache_index is not None
            while not await wired.cache_index.store.holders(
                "tree", str(built.tree.tree_id), among=[a.node_id]
            ):
                assert time.monotonic() < deadline, "A never listed as a holder"
                await asyncio.sleep(0.05)

            assert a.peer is not None
            real = peer_server.PeerBlobServer._respond

            async def slow(self: Any, *args: Any, **kwargs: Any) -> None:
                if self is a.peer:
                    await asyncio.sleep(PEER_DELAY_S)
                await real(self, *args, **kwargs)

            monkeypatch.setattr(peer_server.PeerBlobServer, "_respond", slow)
            ctl_link.bandwidth(16 * 1024)  # the controller's link is very slow
            reminted_before = wired.dispatcher.stats.peer_tokens_reminted
            out = tmp_path / "b.out"
            job = await _submit(http, built, b.node_id, out)
        assert job["state"] == "succeeded", job
        assert out.read_text() == hashlib.sha256(data).hexdigest()
        stage = b.executor.last_stage
        assert stage is not None
        by_a = stage.sources[a.node_id]
        assert by_a.dropped is None, by_a
        assert stage.duration_s > TTL_S + 1, stage.duration_s
        assert by_a.units > CHUNKS // 2, (by_a.units, stage.sources)
        assert a.peer.stats.refused.get("expired", 0) == 0, a.peer.stats.refused
        assert wired.dispatcher.stats.peer_tokens_reminted > reminted_before
    finally:
        await b.stop()
        await a.stop()
