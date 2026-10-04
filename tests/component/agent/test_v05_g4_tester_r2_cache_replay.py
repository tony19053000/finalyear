"""V0.5 G4 tester round 2: the contiguity rule (fix round 1, defect 5) against the
REAL agent's replay path.

The new rule makes a non-contiguous snapshot page mark the node stale until the
next snapshot (default 900 s). A real agent whose stream breaks while it keeps
adding blobs replays unacked reports and then sends a new snapshot; the node
must come back LIVE with exactly the CAS content - not sit stale for minutes.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent

from meshforge.agent.session.state import AgentState
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

CACHE = {"cache": {"flush_s": 0.05, "page_items": 7}}  # many small pages


async def _until(check: Callable[[], Awaitable[bool]], timeout_s: float) -> None:
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


async def _blob_keys(runtime: ControllerRuntime, node: str) -> set[str]:
    from sqlalchemy import select

    from meshforge.controller.store.models.cache import CacheLocation

    assert runtime.cache_index is not None
    async with runtime.cache_index.store._sessions() as db:
        rows = await db.scalars(
            select(CacheLocation.key).where(
                CacheLocation.node_id == node, CacheLocation.kind == "blob"
            )
        )
        return {k.removeprefix("sha256:") for k in rows.all()}


async def test_controller_restarts_during_a_delta_flood_end_live_and_exact(
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    store = agent.executor.cas.store
    node = agent.node_id
    stop = asyncio.Event()
    added: list[str] = []

    async def flood() -> None:
        while not stop.is_set():
            res = await asyncio.to_thread(store.put_bytes, os.urandom(64))
            added.append(res.digest.hex)
            await asyncio.sleep(0.01)

    flooder = asyncio.create_task(flood())
    current = runtime
    stale_seen = 0.0
    cms: list[object] = []
    try:
        for _round in range(4):
            await asyncio.sleep(0.8)
            grpc_port, http_port = current.grpc_port, current.http_port
            await current.stop()
            await asyncio.sleep(0.3)  # the flood keeps adding while it is down
            fresh_settings = make_controller_settings(
                pg_settings,
                **tolerant_liveness(),
                grpc_bind=f"127.0.0.1:{grpc_port}",
                http_bind=f"127.0.0.1:{http_port}",
            )
            cm = ControllerRuntime.start(fresh_settings)
            current = await cm.__aenter__()
            cms.append(cm)
            await agent.client.wait_for_state(AgentState.ONLINE, timeout_s=30)
        await asyncio.sleep(0.5)
    finally:
        stop.set()
        await flooder
    try:
        started = time.monotonic()

        async def exact() -> bool:
            return await _blob_keys(current, node) == set(
                await asyncio.to_thread(_cas_hexes, store)
            )

        try:
            await _until(exact, 25.0)
        except AssertionError:
            state = current.cache_index.state(node)  # type: ignore[union-attr]
            have = await _blob_keys(current, node)
            want = await asyncio.to_thread(_cas_hexes, store)
            raise AssertionError(
                f"missing={len(want - have)} extra={len(have - want)} "
                f"index never matched the CAS within 25 s; phase="
                f"{state.phase.value if state else None}; "
                f"agent snapshots_sent={agent.cache.snapshots_sent if agent.cache else None}"
            ) from None
        stale_seen = time.monotonic() - started
        state = current.cache_index.state(node)  # type: ignore[union-attr]
        assert state is not None and state.phase.value == "live", state
        assert stale_seen < 20.0
    finally:
        if cms:
            await current.stop()


def _cas_hexes(store: object) -> set[str]:
    from meshforge.agent.cache.inventory import scan_cas

    return set(scan_cas(store))  # type: ignore[arg-type]
