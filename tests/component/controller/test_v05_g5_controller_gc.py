"""V0.5 G5: GC and scrub of the controller's CAS over real PostgreSQL + HTTP.

* a pass never evicts what a non-terminal job references (tree, members,
  bundle) or a pinned named input; the same blobs go once the job is terminal;
* a ``have`` answer counts as a use (``min_age_s`` protects it until submit);
* the scrubber finds real on-disk corruption (same size, other bytes), removes
  the blob and ``have`` reports it missing; a manifest whose bytes do not match
  its name is removed when the controller reads it.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.data import NamedInput

pytestmark = pytest.mark.asyncio

CS = MIN_CHUNK_SIZE


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,  # type: ignore[arg-type]
        blobs={"max_blob_bytes": 16 * 1024 * 1024},
        gc={"cas_bytes": 1, "min_age_s": 0, "interval_s": 3600},
    )
    async with running_controller(settings) as rt:
        yield rt


def _tree(root: Path, files: dict[str, bytes]) -> BuiltTree:
    root.mkdir()
    for name, data in files.items():
        (root / name).write_bytes(data)
    return build_tree(root, chunk_size=CS)


def _store(runtime: ControllerRuntime, built: BuiltTree) -> None:
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)


def _all_there(runtime: ControllerRuntime, built: BuiltTree) -> bool:
    assert runtime.blobs is not None
    return runtime.blobs.cas.has(built.tree.tree_id) and all(
        runtime.blobs.cas.has(Digest.parse(f"sha256:{h}")) for h in built.sources
    )


def _none_there(runtime: ControllerRuntime, built: BuiltTree) -> bool:
    assert runtime.blobs is not None
    return not runtime.blobs.cas.has(built.tree.tree_id) and not any(
        runtime.blobs.cas.has(Digest.parse(f"sha256:{h}")) for h in built.sources
    )


async def _queued_job(
    runtime: ControllerRuntime, *, tree_id: str | None, bundle: str | None
) -> str:
    assert runtime.jobs is not None
    job, created = await runtime.jobs.create_queued_job(
        job_id=str(new_job_id()),
        name="gc",
        manifest={"name": "gc", "entrypoint": ["python", "main.py"]},
        bundle_digest=bundle,
        placement={"mode": "offload", "node_id": str(new_node_id())},
        request_hash=os.urandom(32).hex(),
        idempotency_key=None,
        tree_id=tree_id,
    )
    assert created
    return job.id


async def test_live_jobs_and_pinned_inputs_are_never_evicted(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    assert runtime.gc is not None and runtime.blobs is not None
    live = _tree(
        tmp_path / "live", {"a.txt": b"live tree\n", "b.bin": os.urandom(3 * CS)}
    )
    pinned = _tree(tmp_path / "pin", {"data.csv": b"1,2,3\n"})
    loose = _tree(tmp_path / "loose", {"x.txt": b"nobody\n"})
    for built in (live, pinned, loose):
        _store(runtime, built)
    bundle = runtime.blobs.cas.put_bytes(b"\x28\xb5\x2f\xfd" + os.urandom(64)).digest
    job_id = await _queued_job(runtime, tree_id=str(live.tree.tree_id), bundle=None)
    await _queued_job(runtime, tree_id=None, bundle=str(bundle))
    assert runtime._engine is not None
    async with create_session_factory(runtime._engine)() as db, db.begin():
        db.add(
            NamedInput(
                name="ds",
                version=1,
                tree_id=str(pinned.tree.tree_id),
                size=6,
                files=1,
                pinned=True,
            )
        )
    report = await runtime.gc.run_pass("test")
    assert _all_there(runtime, live) and _all_there(runtime, pinned)
    assert runtime.blobs.cas.has(bundle)
    assert _none_there(runtime, loose)
    assert report.over_quota and report.protected_trees == 2
    assert report.protected_blobs == 1
    # The job ends: its tree is no longer protected.
    assert runtime.jobs is not None
    await runtime.jobs.request_cancel(job_id)
    await runtime.gc.run_pass("test")
    assert _none_there(runtime, live)
    assert _all_there(runtime, pinned)


async def test_a_have_answer_is_a_use_that_min_age_protects(
    pg_settings: object, tmp_path: Path
) -> None:
    settings = g2_settings(
        pg_settings,  # type: ignore[arg-type]
        gc={"cas_bytes": 1, "min_age_s": 3600, "interval_s": 3600},
    )
    async with running_controller(settings) as runtime:
        assert runtime.gc is not None and runtime.blobs is not None
        digest = runtime.blobs.cas.put_bytes(b"uploaded a while ago").digest
        # Age the row: as if it was stored two hours ago.
        with runtime.gc.index.tx() as db:
            db.execute("UPDATE item SET last_use = last_use - 7200")
        async with api_client(runtime, token=token_of(runtime)) as client:
            got = await client.post("/v1/blobs/have", json={"digests": [str(digest)]})
            assert got.json() == {"missing": []}
        await runtime.gc.run_pass("test")
        assert runtime.blobs.cas.has(digest)  # 'have' just touched it
        with runtime.gc.index.tx() as db:
            db.execute("UPDATE item SET last_use = last_use - 7200")
        await runtime.gc.run_pass("test")
        assert not runtime.blobs.cas.has(digest)


async def test_scrub_removes_a_blob_corrupted_on_disk(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    assert runtime.scrubber is not None and runtime.blobs is not None
    good = runtime.blobs.cas.put_bytes(b"good bytes").digest
    bad = runtime.blobs.cas.put_bytes(b"fresh bytes\n").digest
    path = runtime.blobs.path_for(bad)
    path.chmod(0o600)
    path.write_bytes(b"FRESH BYTES\n")  # same size: real on-disk corruption
    await runtime.scrubber.run_cycle()
    assert runtime.blobs.cas.has(good)
    assert not runtime.blobs.cas.has(bad)
    assert runtime.scrubber.corrupt == 1
    async with api_client(runtime, token=token_of(runtime)) as client:
        got = await client.post(
            "/v1/blobs/have", json={"digests": [str(good), str(bad)]}
        )
        assert got.json() == {"missing": [str(bad)]}
        # Never served: a GET is 404 now, so the next upload replaces it.
        assert (await client.get(f"/v1/blobs/{bad.hex}")).status_code == 404


async def test_a_suspected_tree_is_scrubbed_first(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    assert runtime.scrubber is not None and runtime.blobs is not None
    built = _tree(tmp_path / "p", {"lib.txt": b"library\n"})
    _store(runtime, built)
    lib = sha256_bytes(b"library\n")
    path = runtime.blobs.path_for(lib)
    path.chmod(0o600)
    path.write_bytes(b"LIBRARY\n")
    # What the dispatcher does when an attempt fails integrity_error.
    runtime.scrubber.suspect_tree_soon(str(built.tree.tree_id), why="test")
    deadline = asyncio.get_running_loop().time() + 10
    while runtime.scrubber.corrupt < 1:
        assert asyncio.get_running_loop().time() < deadline, "never scrubbed"
        await asyncio.sleep(0.02)
    assert not runtime.blobs.cas.has(lib)
    assert runtime.blobs.cas.has(built.tree.tree_id)


async def test_a_manifest_that_does_not_match_its_name_is_removed_on_read(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    assert runtime.blobs is not None
    built = _tree(tmp_path / "p", {"a": b"a"})
    _store(runtime, built)
    path = runtime.blobs.path_for(built.tree.tree_id)
    path.chmod(0o600)
    data = bytearray(built.manifest)
    data[-2] ^= 0x01
    path.write_bytes(bytes(data))
    async with api_client(runtime, token=token_of(runtime)) as client:
        submit = await client.post(
            "/v1/jobs",
            json={
                "manifest": {"name": "x", "entrypoint": ["python", "a"]},
                "tree_id": str(built.tree.tree_id),
                "placement": {"node_id": str(new_node_id())},
            },
        )
        assert submit.status_code == 409, submit.text  # "not in the blob store"
    assert not runtime.blobs.cas.has(built.tree.tree_id)
    assert runtime.blobs.corrupt_evicted == 1
