"""V0.5 G5 tester round 1 - controller side over real PostgreSQL + HTTP:

* ``/v1/data`` (named inputs): auth on every verb, hostile names / ids / bodies,
  concurrent pushes (versions unique, same tree idempotent), pin/rm of unknown;
* ``POST /v1/nodes/{id}/gc`` and ``/prepare``: auth, malformed ids, bounds;
* controller GC: a pass racing job submissions never removes a blob of an
  accepted job; one broken live job must not stop GC for everyone; a corrupted
  blob can be replaced by a re-upload (scrub repair).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

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


async def _push(
    http: httpx.AsyncClient, name: str, built: BuiltTree, **extra: Any
) -> httpx.Response:
    return await http.post(
        "/v1/data", json={"name": name, "tree_id": str(built.tree.tree_id), **extra}
    )


# ------------------------------------------------------------------ auth


async def test_every_data_and_node_verb_needs_the_fabric_token(
    runtime: ControllerRuntime,
) -> None:
    node = str(new_node_id())
    calls: list[tuple[str, str, dict[str, Any]]] = [
        ("POST", "/v1/data", {"json": {"name": "a", "tree_id": "sha256:" + "0" * 64}}),
        ("GET", "/v1/data", {}),
        ("GET", "/v1/data/a", {}),
        ("PATCH", "/v1/data/a/1", {"json": {"pinned": True}}),
        ("DELETE", "/v1/data/a/1", {}),
        ("POST", f"/v1/nodes/{node}/gc", {}),
        ("POST", f"/v1/nodes/{node}/prepare", {"json": {}}),
        ("GET", f"/v1/nodes/{node}/prepare/abc", {}),
    ]
    for headers in (
        {},
        {"authorization": "Bearer wrong"},
        {"authorization": "Basic x"},
    ):
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{runtime.http_port}", headers=headers
        ) as client:
            for method, url, kw in calls:
                got = await client.request(method, url, **kw)
                assert got.status_code == 401, (method, url, headers, got.text)
                assert got.json()["error"]["code"] == "unauthorized"


async def test_node_gc_and_prepare_refuse_malformed_and_offline_nodes_typed(
    http: httpx.AsyncClient,
) -> None:
    for bad in ("nope", "node_", "node_" + "Z" * 26, "x" * 41, "..%2f..", "node_01"):
        got = await http.post(f"/v1/nodes/{bad}/gc")
        assert got.status_code in (400, 404, 422), (bad, got.status_code)
        got = await http.post(f"/v1/nodes/{bad}/prepare", json={})
        assert got.status_code in (400, 404, 422), (bad, got.status_code)
    unknown = str(new_node_id())  # canonical but never registered
    got = await http.post(f"/v1/nodes/{unknown}/gc")
    assert got.status_code == 409 and got.json()["error"]["code"] == "conflict"
    got = await http.get(f"/v1/nodes/{unknown}/prepare/whatever")
    assert got.status_code == 404


async def test_prepare_body_bounds_max_and_max_plus_one(
    http: httpx.AsyncClient,
) -> None:
    node = str(new_node_id())  # offline: valid bodies reach the 409 (not 4xx)
    assert (await http.post(f"/v1/nodes/{node}/prepare", json={})).status_code == 409
    eight = ["python-cpu", "cuda-pytorch"] * 4
    # duplicates collapse, so max is about distinct names: still accepted
    assert (
        await http.post(f"/v1/nodes/{node}/prepare", json={"images": eight})
    ).status_code == 409
    assert (
        await http.post(
            f"/v1/nodes/{node}/prepare", json={"images": [*eight, "python-cpu"]}
        )
    ).status_code == 400
    env = {"base": "python-cpu", "requirements": "x" * 65_536}
    assert (
        await http.post(f"/v1/nodes/{node}/prepare", json={"envs": [env]})
    ).status_code == 409
    env["requirements"] = "x" * 65_537
    assert (
        await http.post(f"/v1/nodes/{node}/prepare", json={"envs": [env]})
    ).status_code == 400
    bad_prepare: list[Any] = [
        {"envs": "nope"},
        {"envs": [1]},
        {"envs": [{"base": "python-cpu", "evil": 1}]},
        {"envs": [{"base": "python-cpu", "python": "3.12; rm -rf /"}]},
        {"envs": [{"base": "python-cpu", "runtime": "kubernetes"}]},
        {"images": "python-cpu"},
        {"images": [1]},
        {"images": None},
        {"extra": 1},
        [],
    ]
    for body in bad_prepare:
        got = await http.post(f"/v1/nodes/{node}/prepare", json=body)
        assert got.status_code == 400, (body, got.status_code, got.text)
    huge = await http.post(
        f"/v1/nodes/{node}/prepare", content=b"{" + b" " * 3_000_000 + b"}"
    )
    assert huge.status_code == 413
    garbage = await http.post(f"/v1/nodes/{node}/prepare", content=b"\xff\xfe{")
    assert garbage.status_code == 400


# ------------------------------------------------------------ /v1/data


async def test_named_input_names_ids_and_bodies_are_validated(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    built = _tree(tmp_path / "t", {"a.txt": b"a"})
    _store(runtime, built)
    tid = str(built.tree.tree_id)
    for name in (
        "",
        "A",
        "has space",
        "a/b",
        "..",
        "a" * 65,
        "naïve",
        "-x",
        "a@1",
        "a\n",
    ):
        got = await http.post("/v1/data", json={"name": name, "tree_id": tid})
        assert got.status_code == 400, (name, got.status_code, got.text)
    for tree_id in (
        tid.upper(),
        tid[:-1],
        "sha256:" + "g" * 64,
        "sha512:" + "0" * 64,
        "",
        tid + " ",
    ):
        got = await http.post("/v1/data", json={"name": "ok", "tree_id": tree_id})
        assert got.status_code == 400, (tree_id, got.status_code)
    bad_data: list[Any] = [
        {"name": "ok"},
        {"tree_id": tid},
        {"name": "ok", "tree_id": tid, "pinned": "yes"},
        {"name": "ok", "tree_id": tid, "extra": 1},
        {"name": 1, "tree_id": tid},
        [],
        "x",
    ]
    for body in bad_data:
        got = await http.post("/v1/data", json=body)
        assert got.status_code == 400, (body, got.status_code)
    big = await http.post("/v1/data", content=b'{"name":"' + b"a" * 5000 + b'"}')
    assert big.status_code == 413
    # Max name length is accepted (and a 64 char name resolves again).
    got = await http.post("/v1/data", json={"name": "a" * 64, "tree_id": tid})
    assert got.status_code == 201, got.text
    assert (await http.get(f"/v1/data/{'a' * 64}")).status_code == 200
    assert (await http.get(f"/v1/data/{'a' * 65}")).status_code in (400, 422)


async def test_a_tree_that_is_not_fully_stored_is_refused_409(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.blobs is not None
    built = _tree(tmp_path / "t", {"a.txt": b"aaa", "b.txt": b"bbb"})
    unknown = await _push(http, "ds", built)
    assert unknown.status_code == 409  # manifest not stored
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    partial = await _push(http, "ds", built)
    assert partial.status_code == 409  # members not stored
    assert (await http.get("/v1/data/ds")).status_code == 404  # nothing registered
    junk = runtime.blobs.cas.put_bytes(b"not a tree manifest")
    got = await http.post("/v1/data", json={"name": "ds", "tree_id": str(junk.digest)})
    assert got.status_code == 400  # a blob that is not a tree manifest


async def test_concurrent_pushes_never_duplicate_a_version_and_same_tree_is_idempotent(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    trees = []
    for i in range(12):
        built = _tree(tmp_path / f"t{i}", {"f.txt": f"version {i}".encode()})
        _store(runtime, built)
        trees.append(built)
    got = await asyncio.gather(*(_push(http, "ds", b) for b in trees))
    assert {g.status_code for g in got} <= {201, 409}, [g.text for g in got]
    versions = [g.json()["version"] for g in got if g.status_code == 201]
    assert len(versions) == len(set(versions))  # never two trees on one version
    listed = (await http.get("/v1/data/ds")).json()["versions"]
    assert sorted(v["version"] for v in listed) == sorted(versions)
    # Same tree, many racers: exactly one creation, the rest see its version.
    fresh = _tree(tmp_path / "fresh", {"f.txt": b"brand new"})
    _store(runtime, fresh)
    race = await asyncio.gather(*(_push(http, "race", fresh) for _ in range(10)))
    assert sorted(r.status_code for r in race) == [200] * 9 + [201]
    assert len((await http.get("/v1/data/race")).json()["versions"]) == 1
    # The same tree under another name is its own version 1.
    other = await _push(http, "other", trees[0])
    assert other.status_code == 201 and other.json()["version"] == 1


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT G5-T2 (minor): NamedInputStore.push retries a version "
    "collision only 5 times with no back-off, so 12 concurrent pushes of "
    "different trees under one name make some callers fail with 409 "
    "'concurrent pushes kept colliding' although a retry would succeed.",
)
async def test_twelve_concurrent_pushes_to_one_name_all_succeed(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    trees = []
    for i in range(12):
        built = _tree(tmp_path / f"t{i}", {"f.txt": f"version {i}".encode()})
        _store(runtime, built)
        trees.append(built)
    got = await asyncio.gather(*(_push(http, "ds", b) for b in trees))
    assert [g.status_code for g in got] == [201] * 12, [g.text for g in got]
    assert sorted(g.json()["version"] for g in got) == list(range(1, 13))


async def test_pin_and_rm_of_unknown_versions_and_bad_numbers(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    built = _tree(tmp_path / "t", {"a": b"a"})
    _store(runtime, built)
    assert (await _push(http, "ds", built)).status_code == 201
    assert (await http.patch("/v1/data/ds/9", json={"pinned": True})).status_code == 404
    assert (await http.delete("/v1/data/ds/9")).status_code == 404
    assert (await http.delete("/v1/data/nothere/1")).status_code == 404
    for version in ("0", "-1", "abc", "1.5", "1000000000", "99999999999999999999"):
        got = await http.delete(f"/v1/data/ds/{version}")
        assert 400 <= got.status_code < 500, (version, got.status_code)
    bad = await http.patch("/v1/data/ds/1", json={"pinned": "true"})
    assert bad.status_code == 400
    bad = await http.patch("/v1/data/ds/1", json={"pinned": True, "x": 1})
    assert bad.status_code == 400
    ok = await http.patch("/v1/data/ds/1", json={"pinned": True})
    assert ok.status_code == 200 and ok.json()["pinned"] is True
    # Pinned + referenced by nobody: rm works (rm is refused only while used).
    gone = await http.delete("/v1/data/ds/1")
    assert gone.status_code == 200
    assert (await http.get("/v1/data/ds")).status_code == 404
    # The names of removed versions are not reused as the SAME version number
    # for another tree silently: a new push starts again at 1 (documented
    # behaviour: max(version)+1 over remaining rows).
    again = await _push(http, "ds", built)
    assert again.status_code == 201


async def test_unpinned_tree_of_a_removed_input_is_collectable_pinned_is_not(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.gc is not None and runtime.blobs is not None
    pinned = _tree(tmp_path / "p", {"a": b"pinned data"})
    plain = _tree(tmp_path / "q", {"a": b"plain data!!"})
    for b in (pinned, plain):
        _store(runtime, b)
    assert (await _push(http, "p", pinned, pinned=True)).status_code == 201
    assert (await _push(http, "q", plain)).status_code == 201
    await runtime.gc.run_pass("t")
    assert runtime.blobs.cas.has(pinned.tree.tree_id)
    # registered but unpinned and unreferenced: GC may take it (documented).
    assert not runtime.blobs.cas.has(plain.tree.tree_id)
    # ... and a job can then not be submitted on it: typed 409, not a 500.
    got = await http.post(
        "/v1/jobs",
        json={
            "manifest": {
                "name": "x",
                "entrypoint": ["python", "main.py"],
                "inputs": [{"ref": "q@1", "mount": "data"}],
            },
            "tree_id": str(pinned.tree.tree_id),
            "placement": {"node_id": str(new_node_id())},
        },
    )
    assert got.status_code in (404, 409), got.text


# ------------------------------------------------------------------- GC


async def test_one_live_job_with_a_missing_manifest_does_not_wedge_gc(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    """A non-terminal job whose tree manifest is gone (operator deleted it, disk
    fault) must not make every later pass fail - the other blobs still need
    collecting and the rest of the live jobs still need protecting."""
    assert (
        runtime.gc is not None
        and runtime.blobs is not None
        and runtime.jobs is not None
    )
    broken = _tree(tmp_path / "broken", {"a": b"x" * 100})
    healthy = _tree(tmp_path / "healthy", {"a": b"y" * 100})
    loose = _tree(tmp_path / "loose", {"a": b"z" * 100})
    for b in (healthy, loose):
        _store(runtime, b)
    for b in (broken, healthy):
        await runtime.jobs.create_queued_job(
            job_id=str(new_job_id()),
            name="gc",
            manifest={"name": "gc", "entrypoint": ["python", "main.py"]},
            bundle_digest=None,
            placement={"mode": "offload", "node_id": str(new_node_id())},
            request_hash=os.urandom(32).hex(),
            idempotency_key=None,
            tree_id=str(b.tree.tree_id),
        )
    report = await runtime.gc.run_pass("t")  # must not raise
    assert report is not None
    assert runtime.blobs.cas.has(healthy.tree.tree_id)  # still protected
    assert not runtime.blobs.cas.has(loose.tree.tree_id)  # still collected


async def test_scrub_then_reupload_repairs_a_corrupted_blob(
    runtime: ControllerRuntime, http: httpx.AsyncClient
) -> None:
    assert runtime.scrubber is not None and runtime.blobs is not None
    data = b"precious bytes " * 100
    digest = sha256_bytes(data)
    put = await http.put(f"/v1/blobs/{digest.hex}", content=data)
    assert put.status_code in (200, 201), put.text
    path = runtime.blobs.path_for(digest)
    path.chmod(0o600)
    path.write_bytes(data[:-5] + b"XXXXX")  # same size, other bytes
    await runtime.scrubber.run_cycle()
    assert runtime.scrubber.corrupt == 1
    assert (await http.get(f"/v1/blobs/{digest.hex}")).status_code == 404
    have = await http.post("/v1/blobs/have", json={"digests": [str(digest)]})
    assert have.json() == {"missing": [str(digest)]}
    again = await http.put(f"/v1/blobs/{digest.hex}", content=data)  # the repair
    assert again.status_code in (200, 201), again.text
    got = await http.get(f"/v1/blobs/{digest.hex}")
    assert got.status_code == 200 and got.content == data
    await runtime.scrubber.run_cycle()
    assert runtime.scrubber.corrupt == 1  # nothing new


async def test_scrub_handles_truncated_empty_and_vanished_blobs(
    runtime: ControllerRuntime, http: httpx.AsyncClient
) -> None:
    assert runtime.scrubber is not None and runtime.blobs is not None
    blobs = {sha256_bytes(d): d for d in (b"a" * 500, b"b" * 500, b"c" * 500)}
    for digest, data in blobs.items():
        assert (
            await http.put(f"/v1/blobs/{digest.hex}", content=data)
        ).status_code in (
            200,
            201,
        )
    d_trunc, d_empty, d_gone = list(blobs)
    for digest, mode in ((d_trunc, "trunc"), (d_empty, "empty"), (d_gone, "gone")):
        path = runtime.blobs.path_for(digest)
        path.chmod(0o600)
        if mode == "trunc":
            path.write_bytes(blobs[digest][:100])
        elif mode == "empty":
            path.write_bytes(b"")
        else:
            path.unlink()
    await runtime.scrubber.run_cycle()  # must not raise on any of them
    assert runtime.scrubber.corrupt >= 2
    for digest in (d_trunc, d_empty, d_gone):
        assert (await http.get(f"/v1/blobs/{digest.hex}")).status_code == 404
