"""Tester pass, round G11 (D454) over real PostgreSQL + HTTPS: deletion safety.

Complements ``test_v1_0_g11_data_sweep.py`` with the cases a deletion feature
must never get wrong: the same tree under two names, concurrent rm of versions
that share blobs, two sweeps at once, a referenced tree whose manifest is gone,
404s that must not sweep, and dry-run being a pure read.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.component, pytest.mark.timeout(180)]

CS = MIN_CHUNK_SIZE


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,  # type: ignore[arg-type]
        blobs={"max_blob_bytes": 16 * 1024 * 1024},
        gc={"cas_bytes": 0, "min_age_s": 86_400, "sweep_min_age_s": 0},
    )
    async with running_controller(settings) as rt:
        yield rt


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(runtime, token=token_of(runtime)) as client:
        yield client


def _tree(root: Path, files: dict[str, bytes]) -> BuiltTree:
    root.mkdir(parents=True)
    for name, data in files.items():
        (root / name).write_bytes(data)
    return build_tree(root, chunk_size=CS)


def _store(runtime: ControllerRuntime, built: BuiltTree) -> None:
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)


def _has(runtime: ControllerRuntime, data: bytes) -> bool:
    assert runtime.blobs is not None
    return runtime.blobs.cas.has(Digest(hashlib.sha256(data).hexdigest()))


def _has_tree(runtime: ControllerRuntime, built: BuiltTree) -> bool:
    assert runtime.blobs is not None
    return runtime.blobs.cas.has(built.tree.tree_id)


async def _push(http: httpx.AsyncClient, name: str, built: BuiltTree) -> None:
    got = await http.post(
        "/v1/data", json={"name": name, "tree_id": str(built.tree.tree_id)}
    )
    assert got.status_code == 201, got.text


async def _job(runtime: ControllerRuntime, built: BuiltTree) -> str:
    assert runtime.jobs is not None
    job_id = str(new_job_id())
    await runtime.jobs.create_queued_job(
        job_id=job_id,
        name="g11t",
        manifest={"name": "g11t", "entrypoint": ["python", "main.py"]},
        bundle_digest=None,
        placement={"mode": "offload", "node_id": str(new_node_id())},
        request_hash=os.urandom(32).hex(),
        idempotency_key=None,
        tree_id=str(built.tree.tree_id),
    )
    return job_id


async def _sql(pg: Any, statement: str, **params: Any) -> None:
    from sqlalchemy import text

    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


async def test_the_same_tree_under_two_names_is_freed_only_by_the_last_rm(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    data = b"identical dataset" * 80
    ds = _tree(tmp_path / "ds", {"d": data})
    _store(runtime, ds)
    await _push(http, "first", ds)
    await _push(http, "second", ds)
    one = (await http.delete("/v1/data/first/1")).json()
    assert one["swept"] is True and one["freed_blobs"] == 0, one
    assert _has(runtime, data) and _has_tree(runtime, ds)
    two = (await http.delete("/v1/data/second/1")).json()
    assert two["freed_blobs"] == 2 and two["freed_bytes"] == len(data) + len(
        ds.manifest
    )
    assert not _has(runtime, data) and not _has_tree(runtime, ds)


async def test_two_versions_of_one_name_share_blobs_until_both_are_gone(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    shared, only1, only2 = b"shared" * 90, b"v1 only" * 90, b"v2 only" * 90
    v1 = _tree(tmp_path / "v1", {"s": shared, "x": only1})
    v2 = _tree(tmp_path / "v2", {"s": shared, "x": only2})
    for built in (v1, v2):
        _store(runtime, built)
        await _push(http, "ds", built)
    r1 = (await http.delete("/v1/data/ds/1")).json()
    assert r1["freed_blobs"] == 2  # v1 manifest + only1
    assert _has(runtime, shared) and _has(runtime, only2) and not _has(runtime, only1)
    r2 = (await http.delete("/v1/data/ds/2")).json()
    assert r2["freed_blobs"] == 3  # v2 manifest + shared + only2
    assert not _has(runtime, shared)


async def test_concurrent_rm_of_versions_sharing_blobs_ends_clean(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    shared = b"s" * 700
    trees = []
    for i in range(6):
        built = _tree(
            tmp_path / f"v{i}", {"s": shared, "u": f"unique {i}".encode() * 40}
        )
        _store(runtime, built)
        await _push(http, "ds", built)
        trees.append(built)
    got = await asyncio.gather(*(http.delete(f"/v1/data/ds/{i}") for i in range(1, 7)))
    assert [g.status_code for g in got] == [200] * 6, [g.text for g in got]
    assert all(g.json()["swept"] is True for g in got)
    freed = sum(g.json()["freed_blobs"] for g in got)
    # 6 manifests + 6 unique files + the shared one, each unlinked exactly once
    assert freed == 13
    assert not _has(runtime, shared)
    again = (await http.post("/v1/data/gc")).json()
    assert again["removed_blobs"] == 0


async def test_two_simultaneous_data_gc_sweeps_free_everything_exactly_once(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    for i in range(5):
        _store(runtime, _tree(tmp_path / f"junk{i}", {"j": f"junk {i}".encode() * 50}))
    keep = _tree(tmp_path / "keep", {"k": b"keep" * 50})
    _store(runtime, keep)
    await _push(http, "keep", keep)
    a, b = await asyncio.gather(http.post("/v1/data/gc"), http.post("/v1/data/gc"))
    assert a.status_code == b.status_code == 200, (a.text, b.text)
    assert a.json()["removed_blobs"] + b.json()["removed_blobs"] == 10
    assert a.json()["failed_blobs"] == b.json()["failed_blobs"] == 0
    assert _has(runtime, b"keep" * 50) and _has_tree(runtime, keep)


async def test_dry_run_is_a_pure_read_however_often_it_runs(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    junk = _tree(tmp_path / "junk", {"j": b"junk" * 100})
    _store(runtime, junk)
    for _ in range(3):
        got = (await http.post("/v1/data/gc", params={"dry_run": "true"})).json()
        assert got["dry_run"] is True and got["removed_blobs"] == 2
        assert _has(runtime, b"junk" * 100) and _has_tree(runtime, junk)
    assert (await http.post("/v1/data/gc")).json()["removed_blobs"] == 2


async def test_rm_of_unknown_or_already_removed_input_is_404_and_sweeps_nothing(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    junk = _tree(tmp_path / "junk", {"j": b"unregistered" * 50})
    ds = _tree(tmp_path / "ds", {"d": b"registered" * 50})
    _store(runtime, junk)
    _store(runtime, ds)
    await _push(http, "ds", ds)
    assert (await http.delete("/v1/data/nope/1")).status_code == 404
    assert (await http.delete("/v1/data/ds/9")).status_code == 404
    assert _has(runtime, b"unregistered" * 50)  # rm only ever sweeps its own tree
    assert (await http.delete("/v1/data/ds/1")).status_code == 200
    assert (await http.delete("/v1/data/ds/1")).status_code == 404
    assert _has(runtime, b"unregistered" * 50)


async def test_rm_never_touches_an_unrelated_unreferenced_blob(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """``among`` scopes the sweep: another user's abandoned upload is not ours to free."""
    other = _tree(tmp_path / "other", {"o": b"someone elses upload" * 30})
    ds = _tree(tmp_path / "ds", {"d": b"mine" * 100})
    _store(runtime, other)
    _store(runtime, ds)
    await _push(http, "ds", ds)
    body = (await http.delete("/v1/data/ds/1")).json()
    assert body["freed_blobs"] == 2
    assert _has(runtime, b"someone elses upload" * 30) and _has_tree(runtime, other)


async def test_a_finished_job_whose_manifest_is_gone_does_not_break_data_gc(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A row naming a tree the LRU pass already evicted must not 500 the sweep."""
    assert runtime.blobs is not None
    project = _tree(tmp_path / "proj", {"main.py": b"print('x')" * 30})
    _store(runtime, project)
    job_id = await _job(runtime, project)
    await _sql(
        runtime.settings.database,
        "UPDATE job SET state = 'succeeded' WHERE id = :j",
        j=job_id,
    )
    runtime.blobs.cas.path_for(project.tree.tree_id).unlink()
    junk = _tree(tmp_path / "junk", {"j": b"junk" * 60})
    _store(runtime, junk)
    got = await http.post("/v1/data/gc")
    assert got.status_code == 200, got.text
    assert got.json()["failed_blobs"] == 0
    assert not _has(runtime, b"junk" * 60)


async def test_a_pinned_and_a_live_job_reference_each_keep_a_shared_blob(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    shared = b"keep me" * 100
    ds = _tree(tmp_path / "ds", {"d": shared, "e": b"gone" * 100})
    pinned = _tree(tmp_path / "pinned", {"p": shared})
    live = _tree(tmp_path / "live", {"main.py": shared})
    for built in (ds, pinned, live):
        _store(runtime, built)
    await _push(http, "ds", ds)
    await _push(http, "pinned", pinned)
    assert (
        await http.patch("/v1/data/pinned/1", json={"pinned": True})
    ).status_code == 200
    first = (await http.delete("/v1/data/ds/1")).json()
    assert first["freed_blobs"] == 2 and _has(runtime, shared)
    await _job(runtime, live)  # queued = live
    await http.patch("/v1/data/pinned/1", json={"pinned": False})
    assert (await http.delete("/v1/data/pinned/1")).json()["freed_blobs"] == 1
    assert _has(runtime, shared)  # the live job's project still names it
    assert (await http.post("/v1/data/gc")).json()["removed_blobs"] == 0


async def test_a_have_answer_protects_an_old_blob_from_a_concurrent_sweep(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """The pusher's `have` said the blob is already here (so it will not upload
    it); a sweep a moment later must not unlink it, however old the blob is."""
    import time

    assert runtime.gc is not None
    old = _tree(tmp_path / "old", {"o": b"old but asked about" * 40})
    stale = _tree(tmp_path / "stale", {"s": b"old and ignored" * 40})
    _store(runtime, old)
    _store(runtime, stale)
    runtime.gc.sweep_min_age_s = 3600.0
    ahead = lambda: time.time() + 7200.0  # noqa: E731 - both clocks, 2 h later
    runtime.gc._wall = ahead
    runtime.gc.index._wall = ahead
    digest = Digest(hashlib.sha256(b"old but asked about" * 40).hexdigest())
    got = await http.post("/v1/blobs/have", json={"digests": [str(digest)]})
    assert got.status_code == 200 and got.json() == {"missing": []}
    swept = (await http.post("/v1/data/gc")).json()
    assert swept["removed_blobs"] == 3, (
        swept
    )  # both manifests + `stale`; not the asked-about file
    assert swept["kept_recent_blobs"] == 1
    assert _has(runtime, b"old but asked about" * 40)
    assert not _has(runtime, b"old and ignored" * 40)
