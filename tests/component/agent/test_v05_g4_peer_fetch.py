"""V0.5 G4 criterion 6 (and 3, 4 with peers): real controller (PostgreSQL + gRPC
+ REST) and two real agents (A, B) in this process, each with its peer blob
server on loopback.

The controller's blob URL is advertised through a FaultLink proxy
(``[controller.api] advertise_url``) so the controller path can be throttled
like a slow laptop link while the A->B peer path is not. A first runs a job on
the tree (it then holds it and reports ``tree <id>``); then B runs one:

* throttled controller: most bytes come from A (per-source bytes), job succeeds;
* A's peer server killed mid-transfer: B finishes from the controller and no
  chunk it already verified is fetched again (criterion 3);
* A serving corrupt bytes: detected per chunk, A dropped, the job succeeds with
  the right data (criterion 4) - corrupt bytes never reach the workdir;
* a controller restart mid-fetch: the RefreshFetchToken carries the peer
  sources again (fresh peer tokens) and the job succeeds.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, wait_job
from mf_faultproxy import FaultLink, FaultLoop
from mf_jobs_harness import token_of
from pydantic import SecretStr

from meshforge.agent.cas.fetcher import BlobFetcher
from meshforge.agent.execution.executor import Executor
from meshforge.agent.peer import server as peer_server
from meshforge.agent.session.state import AgentState
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

CS = MIN_CHUNK_SIZE
CHUNKS = 48  # 3 MiB of data in 64 KiB chunks
PEER = {"bind": "127.0.0.1", "port": 0}
NO_ENDGAME = {"endgame_after_s": 600.0}

MAIN = b"""\
import hashlib, sys
data = open("data.bin", "rb").read()
open(sys.argv[1], "w").write(hashlib.sha256(data).hexdigest())
"""


@pytest.fixture
def fault_loop() -> Iterator[FaultLoop]:
    loop = FaultLoop("g4-peer-fetch")
    try:
        yield loop
    finally:
        loop.close()


@pytest.fixture
def ctl_link(fault_loop: FaultLoop) -> FaultLink:
    # Upstream fixed after the controller started (its REST port is 0 = any).
    return fault_loop.link(("127.0.0.1", 9), name="controller-blobs")


def _settings(
    pg: DatabaseSettings, link: FaultLink, **binds: str
) -> ControllerSettings:
    host, port = link.address
    return make_controller_settings(
        pg,
        jobs={"dispatch_interval_s": 0.1},
        blobs={"max_blob_bytes": 64 * 1024 * 1024},
        api={"advertise_url": f"http://{host}:{port}"},
        **tolerant_liveness(),
        **binds,
    )


@pytest.fixture
def settings(pg_settings: DatabaseSettings, ctl_link: FaultLink) -> ControllerSettings:
    return _settings(pg_settings, ctl_link)


@pytest_asyncio.fixture
async def wired(
    runtime: ControllerRuntime, ctl_link: FaultLink
) -> AsyncIterator[ControllerRuntime]:
    ctl_link.retarget("127.0.0.1", runtime.http_port)
    yield runtime


async def _agent(grpc_target: str, data_dir: Path, **fetch: Any) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        execution={"staging_retry_s": 30},
        agent_overrides={
            "peer": PEER,
            "cache": {"flush_s": 0.1},
            "fetch": {"per_source": 2, **fetch},
        },
    )


@pytest_asyncio.fixture
async def agents(
    wired: ControllerRuntime, grpc_target: str, tmp_path: Path
) -> AsyncIterator[tuple[ExecAgent, ExecAgent]]:
    a = await _agent(grpc_target, tmp_path / "agent-a")
    b = await _agent(grpc_target, tmp_path / "agent-b", **NO_ENDGAME)
    try:
        yield a, b
    finally:
        await b.stop()
        await a.stop()


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
        trust_env=False,
    )


def _project(root: Path) -> tuple[BuiltTree, bytes]:
    root.mkdir()
    (root / "main.py").write_bytes(MAIN)
    data = os.urandom(CHUNKS * CS)
    (root / "data.bin").write_bytes(data)
    return build_tree(root, chunk_size=CS), data


async def _upload(runtime: ControllerRuntime, built: BuiltTree) -> None:
    client = ControllerClient(
        f"http://127.0.0.1:{runtime.http_port}",
        timeout_s=30,
        token=SecretStr(token_of(runtime)),
    )
    try:
        await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()


async def _submit(
    http: httpx.AsyncClient, built: BuiltTree, node_id: str, out: Path
) -> str:
    posted = await http.post(
        "/v1/jobs",
        json={
            "manifest": {"name": "peer", "entrypoint": ["python", "main.py", str(out)]},
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": node_id},
        },
    )
    assert posted.status_code == 201, posted.text
    return str(posted.json()["job_id"])


async def _until(check: Callable[[], Awaitable[bool]], timeout_s: float = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def _a_holds(
    runtime: ControllerRuntime, a: ExecAgent, built: BuiltTree, out: Path
) -> None:
    """Run the tree on A, then wait until the index lists A as a holder."""
    async with _http(runtime) as http:
        job_id = await _submit(http, built, a.node_id, out)
        job = await wait_job(http, job_id, timeout_s=60)
    assert job["state"] == "succeeded", job

    async def listed() -> bool:
        assert runtime.cache_index is not None
        rows = await runtime.cache_index.store.holders(
            "tree", str(built.tree.tree_id), among=[a.node_id]
        )
        return bool(rows)

    await _until(listed)


async def _run_on_b(
    runtime: ControllerRuntime, b: ExecAgent, built: BuiltTree, out: Path
) -> dict[str, Any]:
    async with _http(runtime) as http:
        return await wait_job(
            http, await _submit(http, built, b.node_id, out), timeout_s=120
        )


async def test_criterion_6_most_bytes_from_the_peer_when_the_controller_is_slow(
    wired: ControllerRuntime,
    agents: tuple[ExecAgent, ExecAgent],
    ctl_link: FaultLink,
    tmp_path: Path,
) -> None:
    a, b = agents
    built, data = _project(tmp_path / "p")
    await _upload(wired, built)
    await _a_holds(wired, a, built, tmp_path / "a.out")
    ctl_link.bandwidth(256 * 1024)  # the controller's link is now slow
    out = tmp_path / "b.out"
    job = await _run_on_b(wired, b, built, out)
    assert job["state"] == "succeeded", job
    assert out.read_text() == hashlib.sha256(data).hexdigest()
    stage = b.executor.last_stage
    assert stage is not None
    by_a = stage.sources[a.node_id].bytes
    by_ctl = stage.sources["controller"].bytes
    assert by_a > 2 * by_ctl, (by_a, by_ctl)
    assert stage.sources[a.node_id].dropped is None


async def test_peer_killed_mid_transfer_controller_finishes_without_resending(
    wired: ControllerRuntime,
    agents: tuple[ExecAgent, ExecAgent],
    ctl_link: FaultLink,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = agents
    built, data = _project(tmp_path / "p")
    await _upload(wired, built)
    await _a_holds(wired, a, built, tmp_path / "a.out")
    ctl_link.bandwidth(512 * 1024)
    assert a.peer is not None
    served = [0]
    real = peer_server.PeerBlobServer._respond
    killer: list[asyncio.Task[None]] = []

    async def counting(self: Any, *args: Any, **kwargs: Any) -> None:
        served[0] += 1
        if self is a.peer and served[0] == 10:
            killer.append(asyncio.create_task(a.peer.close()))  # A dies mid-way
        await real(self, *args, **kwargs)

    monkeypatch.setattr(peer_server.PeerBlobServer, "_respond", counting)
    out = tmp_path / "b.out"
    job = await _run_on_b(wired, b, built, out)
    assert job["state"] == "succeeded", job
    assert out.read_text() == hashlib.sha256(data).hexdigest()
    stage = b.executor.last_stage
    assert stage is not None
    per = stage.sources
    assert per[a.node_id].dropped is not None
    assert per[a.node_id].units >= 1 and per["controller"].units >= 1
    # Every unit was won exactly once; the controller sent nothing but its own
    # units and the manifest - no chunk A had delivered was fetched again.
    units = CHUNKS + 1  # data.bin chunks + main.py
    assert per[a.node_id].units + per["controller"].units == units
    manifest = len(built.manifest)
    ctl_payload = per["controller"].bytes - manifest
    assert ctl_payload <= per["controller"].units * CS
    await asyncio.gather(*killer)


async def test_corrupt_peer_detected_per_chunk_dropped_never_materialised(
    wired: ControllerRuntime,
    agents: tuple[ExecAgent, ExecAgent],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = agents
    built, data = _project(tmp_path / "p")
    await _upload(wired, built)
    await _a_holds(wired, a, built, tmp_path / "a.out")
    real = peer_server._pread

    def corrupt(fh: Any, start: int, length: int) -> bytes:
        body = real(fh, start, length)
        return body[:-1] + bytes([body[-1] ^ 0x01])

    monkeypatch.setattr(peer_server, "_pread", corrupt)
    out = tmp_path / "b.out"
    job = await _run_on_b(wired, b, built, out)
    assert job["state"] == "succeeded", job
    assert out.read_text() == hashlib.sha256(data).hexdigest()
    stage = b.executor.last_stage
    assert stage is not None
    assert stage.sources[a.node_id].dropped == "integrity_error"
    assert stage.sources[a.node_id].units == 0
    # B's cache holds only verified bytes.
    digest = next(e.digest for e in built.tree.files() if e.chunks)
    assert b.executor.cas.store.verify(digest)


async def test_controller_restart_mid_fetch_refresh_carries_peer_sources(
    wired: ControllerRuntime,
    agents: tuple[ExecAgent, ExecAgent],
    pg_settings: DatabaseSettings,
    ctl_link: FaultLink,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = agents
    built, data = _project(tmp_path / "p")
    await _upload(wired, built)
    await _a_holds(wired, a, built, tmp_path / "a.out")
    ctl_link.bandwidth(256 * 1024)
    reached, release = asyncio.Event(), asyncio.Event()
    calls = [0]
    real_range = BlobFetcher._range

    async def gated(self: BlobFetcher, source: Any, *args: Any) -> bytes:
        calls[0] += 1
        if calls[0] == 6:
            reached.set()
            await release.wait()
        return await real_range(self, source, *args)

    monkeypatch.setattr(BlobFetcher, "_range", gated)
    refreshes: list[list[str]] = []
    real_refresh = Executor._refresh_token

    def recording(self: Executor, msg: Any) -> None:
        refreshes.append([src.node_id for src in msg.sources])
        real_refresh(self, msg)

    monkeypatch.setattr(Executor, "_refresh_token", recording)
    out = tmp_path / "b.out"
    async with _http(wired) as http:
        job_id = await _submit(http, built, b.node_id, out)
    await asyncio.wait_for(reached.wait(), 60)
    # B reconnects only once A is back on the new controller: the refresh then
    # deterministically finds A ONLINE (D100 lists ONLINE holders only).
    a_back = asyncio.Event()
    real_attempt = b.client._attempt

    async def held_attempt(*args: Any) -> Any:
        await a_back.wait()
        return await real_attempt(*args)

    b.client._attempt = held_attempt  # type: ignore[method-assign]
    grpc_port, http_port = wired.grpc_port, wired.http_port
    await wired.stop()
    release.set()
    fresh_settings = _settings(
        pg_settings,
        ctl_link,
        grpc_bind=f"127.0.0.1:{grpc_port}",
        http_bind=f"127.0.0.1:{http_port}",
    )
    async with ControllerRuntime.start(fresh_settings) as fresh:
        assert fresh.manager is not None
        manager = fresh.manager

        async def a_online() -> bool:
            live = manager.get(a.node_id)
            return live is not None and bool(live.peer_endpoint)

        await _until(a_online)
        a_back.set()
        await b.client.wait_for_state(AgentState.ONLINE, timeout_s=30)
        async with _http(fresh) as http:
            job = await wait_job(http, job_id, timeout_s=120)
        assert job["state"] == "succeeded", job
        assert fresh.dispatcher is not None
        assert fresh.dispatcher.stats.fetch_tokens_refreshed >= 1
    assert out.read_text() == hashlib.sha256(data).hexdigest()
    # The refresh re-minted the whole list: the controller first, then A.
    assert refreshes, "no RefreshFetchToken reached B"
    assert refreshes[-1][0] == "" and a.node_id in refreshes[-1]
    stage = b.executor.last_stage
    assert stage is not None and stage.sources[a.node_id].units >= 1
    with contextlib.suppress(Exception):
        await wired.stop()
