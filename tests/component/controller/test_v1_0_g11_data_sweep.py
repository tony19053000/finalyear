"""V1.0 G11 (D454) over real PostgreSQL + HTTPS: ``compute data rm`` frees the
controller's blob store at once, ``POST /v1/data/gc`` sweeps it on demand.

* rm unlinks the removed tree's blobs that nothing else references and says
  how much it freed; a blob shared with another named input (pinned or not)
  or with a job (any state) stays;
* a finished job's named-input history does not keep a removed input alive;
* blobs used within ``sweep_min_age_s`` are kept (an upload in flight);
* ``/v1/data/gc`` with ``dry_run`` counts and unlinks nothing; without it
  sweeps every unreferenced blob (e.g. an abandoned upload) and nothing else;
* GC off -> rm still works (``swept: false``), ``/v1/data/gc`` is a typed
  ``config_error``.
"""

from __future__ import annotations

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
    # no quota: the LRU pass never evicts here, only the sweep does
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
    import hashlib

    assert runtime.blobs is not None
    return runtime.blobs.cas.has(Digest(hashlib.sha256(data).hexdigest()))


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
        name="g11",
        manifest={"name": "g11", "entrypoint": ["python", "main.py"]},
        bundle_digest=None,
        placement={"mode": "offload", "node_id": str(new_node_id())},
        request_hash=os.urandom(32).hex(),
        idempotency_key=None,
        tree_id=str(built.tree.tree_id),
    )
    return job_id


async def _sql(pg: Any, statement: str, **params: Any) -> None:
    from sqlalchemy import text  # the controller extra (import guard)

    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


async def test_rm_frees_only_blobs_nothing_else_references(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    gone, shared_ds, shared_job = b"only in ds@1" * 50, b"also in other" * 50, b"j" * 99
    ds = _tree(tmp_path / "ds", {"a": gone, "b": shared_ds, "c": shared_job})  # removed
    other = _tree(tmp_path / "other", {"x": shared_ds})  # unpinned, stays
    project = _tree(tmp_path / "proj", {"main.py": shared_job})  # a finished job's
    for built in (ds, other, project):
        _store(runtime, built)
    await _push(http, "ds", ds)
    await _push(http, "other", other)
    job_id = await _job(runtime, project)
    await _sql(
        runtime.settings.database,
        "UPDATE job SET state = 'succeeded' WHERE id = :j",
        j=job_id,
    )

    got = await http.delete("/v1/data/ds/1")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["removed"] is True and body["swept"] is True
    # the manifest of ds@1 and its one unshared file
    assert body["freed_blobs"] == 2
    assert body["freed_bytes"] == len(gone) + len(ds.manifest)
    assert body["kept_recent_blobs"] == 0
    assert not _has(runtime, gone)
    assert not runtime.blobs.cas.has(ds.tree.tree_id)  # type: ignore[union-attr]
    assert _has(runtime, shared_ds)  # still named by `other`
    assert _has(runtime, shared_job)  # still named by a finished job's project
    assert runtime.blobs.cas.has(other.tree.tree_id)  # type: ignore[union-attr]
    assert runtime.blobs.cas.has(project.tree.tree_id)  # type: ignore[union-attr]


async def test_a_finished_jobs_input_history_does_not_keep_a_removed_input(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    data = b"dataset bytes" * 100
    ds = _tree(tmp_path / "ds", {"d": data})
    project = _tree(tmp_path / "proj", {"main.py": b"print(1)"})
    for built in (ds, project):
        _store(runtime, built)
    await _push(http, "ds", ds)
    job_id = await _job(runtime, project)
    # what a submission with `inputs: [{ref: ds@1, mount: data}]` records
    await _sql(
        runtime.settings.database,
        "INSERT INTO job_input (job_id, mount, ref, name, version, tree_id) "
        "VALUES (:j, 'data', 'ds@1', 'ds', 1, :t)",
        j=job_id,
        t=str(ds.tree.tree_id),
    )
    # unfinished: rm refused, nothing freed
    assert (await http.delete("/v1/data/ds/1")).status_code == 409
    assert _has(runtime, data)
    await _sql(
        runtime.settings.database,
        "UPDATE job SET state = 'failed', failure_type = 'node_lost' WHERE id = :j",
        j=job_id,
    )
    body = (await http.delete("/v1/data/ds/1")).json()
    assert body["swept"] is True and body["freed_blobs"] == 2, body
    assert not _has(runtime, data)
    assert _has(runtime, b"print(1)")  # the job's own project stays


async def test_recently_used_blobs_survive_and_gc_frees_them_later(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.gc is not None
    data = b"fresh" * 300
    ds = _tree(tmp_path / "ds", {"d": data})
    _store(runtime, ds)
    await _push(http, "ds", ds)
    runtime.gc.sweep_min_age_s = 3600.0  # everything here was used just now
    body = (await http.delete("/v1/data/ds/1")).json()
    assert body["swept"] is True
    assert body["freed_blobs"] == 0 and body["kept_recent_blobs"] == 2
    assert _has(runtime, data)
    got = await http.post("/v1/data/gc")
    assert got.json()["removed_blobs"] == 0
    assert got.json()["kept_recent_blobs"] >= 2
    runtime.gc.sweep_min_age_s = 0.0  # ... later
    got = await http.post("/v1/data/gc")
    assert got.status_code == 200, got.text
    assert got.json()["removed_blobs"] >= 2
    assert not _has(runtime, data)


async def test_data_gc_dry_run_counts_and_the_real_sweep_frees_only_garbage(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    abandoned = _tree(tmp_path / "abandoned", {"a": b"never registered" * 40})
    kept = _tree(tmp_path / "kept", {"k": b"registered" * 40})
    live = _tree(tmp_path / "live", {"main.py": b"live job" * 40})
    for built in (abandoned, kept, live):
        _store(runtime, built)
    await _push(http, "kept", kept)
    await _job(runtime, live)  # queued = live
    expected_bytes = len(b"never registered" * 40) + len(abandoned.manifest)

    dry = await http.post("/v1/data/gc", params={"dry_run": "true"})
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True
    assert dry.json()["removed_blobs"] == 2
    assert dry.json()["removed_bytes"] == expected_bytes
    assert runtime.blobs.cas.has(abandoned.tree.tree_id)  # type: ignore[union-attr]

    real = (await http.post("/v1/data/gc")).json()
    assert real["dry_run"] is False
    assert real["removed_blobs"] == 2 and real["removed_bytes"] == expected_bytes
    assert real["failed_blobs"] == 0
    assert not runtime.blobs.cas.has(abandoned.tree.tree_id)  # type: ignore[union-attr]
    assert _has(runtime, b"registered" * 40)
    assert _has(runtime, b"live job" * 40)
    again = (await http.post("/v1/data/gc")).json()
    assert again["removed_blobs"] == 0


async def test_data_gc_needs_a_token(runtime: ControllerRuntime) -> None:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}"
    ) as client:
        got = await client.post("/v1/data/gc")
    assert got.status_code == 401


async def test_with_gc_off_rm_still_works_and_data_gc_is_a_config_error(
    pg_settings: object, tmp_path: Path
) -> None:
    settings = g2_settings(
        pg_settings,  # type: ignore[arg-type]
        gc={"enabled": False},
    )
    async with (
        running_controller(settings) as rt,
        api_client(rt, token=token_of(rt)) as http,
    ):
        ds = _tree(tmp_path / "ds", {"d": b"data" * 10})
        _store(rt, ds)
        await _push(http, "ds", ds)
        body = (await http.delete("/v1/data/ds/1")).json()
        assert body["removed"] is True and body["swept"] is False
        assert body["freed_blobs"] == 0
        got = await http.post("/v1/data/gc")
        assert got.status_code >= 400
        assert got.json()["error"]["code"] == "config_error"


async def test_a_push_racing_the_rm_sweep_never_registers_a_broken_tree(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    """The sweep holds the GC guard exclusively and a push holds it shared
    from its tree check to its row: a push sharing the removed input's blobs
    either lands first (its tree keeps them) or after (the blob is gone and
    the push is a typed 409) - never a registered tree with a missing blob.
    ``sweep_min_age_s`` is 0 here, so recency does not hide the race."""
    import asyncio

    assert runtime.blobs is not None
    for i in range(8):
        shared = f"shared {i} ".encode() * 64
        old = _tree(tmp_path / f"old{i}", {"s": shared, "o": b"old" * (i + 1)})
        new = _tree(tmp_path / f"new{i}", {"s": shared})
        _store(runtime, old)
        await _push(http, f"old{i}", old)
        runtime.blobs.cas.put_bytes(new.manifest, new.tree.tree_id)
        rm, push = await asyncio.gather(
            http.delete(f"/v1/data/old{i}/1"),
            http.post(
                "/v1/data", json={"name": f"new{i}", "tree_id": str(new.tree.tree_id)}
            ),
        )
        assert rm.status_code == 200, rm.text
        if push.status_code == 201:
            assert _has(runtime, shared), i  # its tree kept the shared blob
        else:
            assert push.status_code == 409, push.text
            assert not _has(runtime, shared)
