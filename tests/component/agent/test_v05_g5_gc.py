"""V0.5 G5 D91 on a real agent: GC never evicts what a running attempt holds,
leases are seen across processes, and a crash mid-eviction leaves old or new
state. Real PostgreSQL + gRPC + REST + agent (subprocess runtime); real child
processes for the cross-process and crash cases.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, wait_job
from mf_jobs_harness import token_of
from pydantic import SecretStr

from meshforge.agent.gc.collector import GarbageCollector, GcConfig
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import MIN_CHUNK_SIZE, CasStore
from meshforge.common.gc_index import GcIndex
from meshforge.common.hashing import Digest
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

CS = MIN_CHUNK_SIZE
WAITER = b"""\
import pathlib, sys, time
flag = pathlib.Path(sys.argv[1])
pathlib.Path(sys.argv[2]).write_text("started")
deadline = time.monotonic() + 60
while not flag.exists() and time.monotonic() < deadline:
    time.sleep(0.05)
print(open("lib.txt").read())
"""


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        blobs={"max_blob_bytes": 64 * 1024 * 1024},
        # not a liveness test: in-loop agent, a loaded runner expires a 0.6 s lease (D399)
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={
            "gc": {"cas_bytes": 1, "envs_bytes": 1, "min_age_s": 0, "interval_s": 3600}
        },
    )
    yield running
    await running.stop()


async def _upload(runtime: ControllerRuntime, built: BuiltTree) -> None:
    client = ControllerClient(
        f"https://127.0.0.1:{runtime.http_port}",
        timeout_s=30,
        token=SecretStr(token_of(runtime)),
    )
    try:
        await asyncio.to_thread(upload_tree, client, built)
    finally:
        client.close()


async def _wait_for(path: Path, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not await asyncio.to_thread(path.exists):
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        await asyncio.sleep(0.05)


async def test_a_running_attempt_keeps_its_tree_through_gc_passes(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    assert agent.gc is not None
    store = agent.executor.cas.store
    stale = store.put_bytes(b"nobody uses this blob" * 10).digest
    project = tmp_path / "p"
    project.mkdir()
    (project / "main.py").write_bytes(WAITER)
    (project / "lib.txt").write_bytes(b"library\n")
    (project / "data.bin").write_bytes(os.urandom(3 * CS + 1))  # chunked
    built = build_tree(project, chunk_size=CS)
    await _upload(runtime, built)
    flag, started = tmp_path / "go", tmp_path / "started"
    posted = await _post_pinned(
        http,
        built,
        agent.node_id,
        {"name": "gc", "entrypoint": ["python", "main.py", str(flag), str(started)]},
    )
    assert posted.status_code == 201, posted.text
    await _wait_for(started)  # the workload runs: staging is over, lease held
    report = await agent.gc.run_now("test")
    members = [Digest.parse(f"sha256:{h}") for h in built.sources]
    assert not store.has(stale)  # evicted: nobody holds it
    assert store.has(built.tree.tree_id)
    assert all(store.has(d) for d in members)
    assert report.over_quota == ["blob"]  # only leased blobs left
    flag.write_text("go")
    job = await wait_job(http, str(posted.json()["job_id"]))
    assert job["state"] == "succeeded", job
    # The attempt ended: its lease is released, the next pass may evict.
    await agent.gc.run_now("test")
    assert not store.has(built.tree.tree_id)
    assert not any(store.has(d) for d in members)
    # Withdrawn from the cache reports (D100): the index loses the tree.
    assert agent.cache is not None
    assert not agent.cache._inv.has_tree(built.tree.tree_id)


async def test_the_next_job_after_eviction_fetches_again_and_succeeds(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    assert agent.gc is not None
    project = tmp_path / "p"
    project.mkdir()
    (project / "main.py").write_bytes(b"print(open('lib.txt').read())\n")
    (project / "lib.txt").write_bytes(b"again\n")
    built = build_tree(project, chunk_size=CS)
    await _upload(runtime, built)
    for _ in range(2):
        posted = await _post_pinned(
            http,
            built,
            agent.node_id,
            {"name": "gc2", "entrypoint": ["python", "main.py"]},
        )
        job = await wait_job(http, str(posted.json()["job_id"]))
        assert job["state"] == "succeeded", job
        stage = agent.executor.last_stage
        assert stage is not None and stage.blobs_fetched == 2 and stage.blobs_hit == 0
        await agent.gc.run_now("test")  # evicts everything (quota 1 byte)


async def _post_pinned(
    http: httpx.AsyncClient,
    built: BuiltTree,
    node_id: str,
    manifest: dict[str, object],
) -> httpx.Response:
    """POST a job pinned to ``node_id``. Under full-tier load the agent's
    session can expire and re-enrol (this module's fixture runs controller
    and agent in one process with a 0.6 s lease, so a blocked event loop
    alone can expire it); a pinned job is then refused 409 (node offline)
    until it is back, so retry 409 for a bounded time."""
    deadline = time.monotonic() + 30.0
    while True:
        posted = await http.post(
            "/v1/jobs",
            json={
                "manifest": manifest,
                "tree_id": str(built.tree.tree_id),
                "placement": {"node_id": node_id},
            },
        )
        if posted.status_code != 409 or time.monotonic() > deadline:
            assert posted.status_code in (200, 201), posted.text
            return posted
        await asyncio.sleep(0.5)


HOLDER = """
import sys, time
from pathlib import Path
from meshforge.common.gc_index import GcIndex
root, tree, members = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
index = GcIndex(Path(root))
index.open()
index.lease("att_other_process", [("tree", tree)])
index.record_tree(tree, members)
print("leased", flush=True)
time.sleep(120)
"""


def _populate(tmp_path: Path) -> tuple[CasStore, BuiltTree]:
    project = tmp_path / "proj"
    project.mkdir()
    for i in range(6):
        (project / f"f{i}.bin").write_bytes(bytes([i]) * 5000)
    built = build_tree(project, chunk_size=CS)
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    store.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        store.put_file(path)
    return store, built


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    return env


async def test_a_lease_held_by_another_process_is_honoured_until_it_dies(
    tmp_path: Path,
) -> None:
    store, built = _populate(tmp_path)
    extra = store.put_bytes(b"x" * 999).digest
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        HOLDER,
        str(store.root),
        built.tree.tree_id.hex,
        ",".join(built.sources),
        stdout=asyncio.subprocess.PIPE,
        env=_child_env(),
    )
    index = GcIndex(store.root)
    try:
        assert child.stdout is not None
        line = await asyncio.wait_for(child.stdout.readline(), 30)
        assert line.strip() == b"leased"
        index.open()
        gc = GarbageCollector(index, store, GcConfig(cas_bytes=1, min_age_s=0))
        report = gc.run_pass("test")
        assert not store.has(extra)
        assert store.has(built.tree.tree_id)
        assert all(store.has(Digest.parse(f"sha256:{h}")) for h in built.sources)
        assert report.over_quota == ["blob"]
        child.kill()  # SIGKILL: dies without releasing anything
        await asyncio.wait_for(child.wait(), 30)
        gc.run_pass("test")
        assert not store.has(built.tree.tree_id)
        assert not any(store.has(Digest.parse(f"sha256:{h}")) for h in built.sources)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        index.close()


CRASHER = """
import os, sys
from pathlib import Path
from meshforge.agent.gc.collector import GarbageCollector, GcConfig
from meshforge.common.gc_index import GcIndex
from meshforge.common.cas import CasStore
root, after = Path(sys.argv[1]), int(sys.argv[2])
store = CasStore(root, max_blob_bytes=1 << 30)
store.prepare()
index = GcIndex(root)
index.open()
done = 0
real = store.evict
def evict(digest):
    global done
    if done == after:
        os._exit(137)  # killed in the middle of an eviction batch
    done += 1
    return real(digest)
store.evict = evict
GarbageCollector(index, store, GcConfig(cas_bytes=1, min_age_s=0)).run_pass("crash")
"""


@pytest.mark.parametrize("after", [0, 3])
async def test_a_crash_mid_eviction_leaves_old_or_new_state(
    tmp_path: Path, after: int
) -> None:
    store, built = _populate(tmp_path)
    before = {h: store.path_for(Digest.parse(f"sha256:{h}")) for h in built.sources}
    before[built.tree.tree_id.hex] = store.path_for(built.tree.tree_id)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", CRASHER, str(store.root), str(after), env=_child_env()
    )
    assert await asyncio.wait_for(proc.wait(), 60) == 137
    # Every blob is either fully there (and still verifies) or gone - never a
    # half-written file under its real name.
    gone = 0
    for hex_, path in before.items():
        if path.exists():
            assert store.verify(Digest.parse(f"sha256:{hex_}"))
        else:
            gone += 1
    assert gone == after
    # The rolled-back index is repaired at the next start (rows == disk).
    index = GcIndex(store.root)
    index.open()
    try:
        GarbageCollector(index, store, GcConfig()).reconcile()
        with index.tx() as db:
            rows = {k for (k,) in db.execute("SELECT key FROM item WHERE kind='blob'")}
        on_disk = {h for h, p in before.items() if p.exists()}
        assert rows == on_disk
    finally:
        index.close()
