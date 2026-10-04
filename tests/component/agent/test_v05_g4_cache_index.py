"""V0.5 G4 D100/D101: the cache location index with a real controller
(PostgreSQL + gRPC + REST) and a real agent (session client + CAS + reporter).

Checked: the snapshot after Hello lists the agent's blobs and complete trees;
deltas add a new blob and withdraw a tree when a member is evicted; the index
converges again after an agent restart (trees.idx re-checked) and after a
controller restart (new snapshot on the new session); REST node detail shows
the per-kind counts.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent
from mf_jobs_harness import token_of

from meshforge.agent.session.state import AgentState
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import Digest
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(90)]

CACHE = {"cache": {"flush_s": 0.1}}


def _settings(pg: DatabaseSettings, **binds: str) -> ControllerSettings:
    return make_controller_settings(pg, **tolerant_liveness(), **binds)


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return _settings(pg_settings)


def _tree(root: Path) -> BuiltTree:
    root.mkdir()
    (root / "a.txt").write_bytes(b"alpha\n")
    (root / "b.bin").write_bytes(os.urandom(MIN_CHUNK_SIZE * 2 + 7))
    return build_tree(root, chunk_size=MIN_CHUNK_SIZE)


def _hold(agent: ExecAgent, built: BuiltTree) -> None:
    store = agent.executor.cas.store
    for hex_, path in built.sources.items():
        store.put_file(path, Digest(hex_))
    store.put_bytes(built.manifest, built.tree.tree_id)
    agent.executor.cas.tree_staged(built.tree)


async def _rows(runtime: ControllerRuntime, node_id: str) -> dict[str, tuple[int, int]]:
    assert runtime.cache_index is not None
    return await runtime.cache_index.store.summary(node_id)


async def _holds_tree(runtime: ControllerRuntime, node_id: str, tree: Digest) -> bool:
    assert runtime.cache_index is not None
    found = await runtime.cache_index.store.holders("tree", str(tree), among=[node_id])
    return bool(found)


async def _until(check: Callable[[], Awaitable[bool]], timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir, agent_overrides=CACHE)
    yield running
    await running.stop()


async def test_snapshot_deltas_and_withdrawal(
    runtime: ControllerRuntime, agent: ExecAgent, tmp_path: Path
) -> None:
    built = _tree(tmp_path / "p")
    await asyncio.to_thread(_hold, agent, built)
    node = agent.node_id
    tree = built.tree.tree_id
    # Deltas after the first snapshot: 3 blobs + the tree.
    await _until(lambda: _holds_tree(runtime, node, tree))
    rows = await _rows(runtime, node)
    assert rows["blob"][0] == 3 and rows["tree"][0] == 1
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        trust_env=False,
    ) as http:
        detail = (await http.get(f"/v1/nodes/{node}")).json()
    assert detail["cache"]["blob"]["count"] == 3
    assert detail["cache"]["tree"] == {
        "count": 1,
        "bytes": built.tree.total_bytes + built.tree.manifest_bytes,
    }

    # Evict one member: the tree is withdrawn, the blob row goes.
    member = next(h for h in built.sources)
    await asyncio.to_thread(agent.executor.cas.store.evict, Digest(member))

    async def withdrawn() -> bool:
        return not await _holds_tree(runtime, node, tree)

    await _until(withdrawn)
    assert (await _rows(runtime, node))["blob"][0] == 2

    # Session fence: a write queued by another (superseded) session is a no-op.
    assert runtime.cache_index is not None
    written = await runtime.cache_index.store.apply(
        node, "sess_not_the_live_one", replace=True, added=[], removed=[]
    )
    assert written is False
    assert (await _rows(runtime, node))["blob"][0] == 2


async def test_converges_after_agent_restart_and_controller_restart(
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    grpc_target: str,
    data_dir: Path,
    tmp_path: Path,
) -> None:
    first = await ExecAgent.start(grpc_target, data_dir, agent_overrides=CACHE)
    built = _tree(tmp_path / "p")
    await asyncio.to_thread(_hold, first, built)
    node = first.node_id
    tree = built.tree.tree_id
    await _until(lambda: _holds_tree(runtime, node, tree))
    await first.stop()

    # Agent restart: a new process, same data dir. Its snapshot (seq from 1
    # again, a new session) replaces the rows and still lists the tree.
    second = await ExecAgent.start(grpc_target, data_dir, agent_overrides=CACHE)
    try:
        assert second.cache is not None
        await _until(lambda: _snapshot_after(second))
        assert await _holds_tree(runtime, node, tree)
        extra = await asyncio.to_thread(second.executor.cas.store.put_bytes, b"later")

        async def has_four() -> bool:
            return (await _rows(runtime, node)).get("blob", (0, 0))[0] == 4

        await _until(has_four)

        # Controller restart: same database, same ports; the agent reconnects,
        # Reconcile, a new snapshot on the new session -> same rows.
        grpc_port, http_port = runtime.grpc_port, runtime.http_port
        await runtime.stop()
        await asyncio.to_thread(second.executor.cas.store.evict, extra.digest)
        fresh_settings = _settings(
            pg_settings,
            grpc_bind=f"127.0.0.1:{grpc_port}",
            http_bind=f"127.0.0.1:{http_port}",
        )
        async with ControllerRuntime.start(fresh_settings) as fresh:
            await second.client.wait_for_state(AgentState.ONLINE, timeout_s=30)

            async def back_to_three() -> bool:
                rows = await _rows(fresh, node)
                return (
                    rows.get("blob", (0, 0))[0] == 3
                    and rows.get("tree", (0, 0))[0] == 1
                )

            await _until(back_to_three)
            assert fresh.cache_index is not None
            state = fresh.cache_index.state(node)
            assert state is not None and state.phase.value == "live"
    finally:
        await second.stop()


async def _snapshot_after(agent: ExecAgent) -> bool:
    assert agent.cache is not None
    return agent.cache.snapshots_sent >= 1
