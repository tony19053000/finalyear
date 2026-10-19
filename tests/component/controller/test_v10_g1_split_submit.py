"""V0.10 G1 (0.10.1, D260): ``POST /v1/jobs`` with ``split:`` over real HTTP +
PostgreSQL + the controller's CAS + a real gRPC session (``FakeAgent``).

* the items are resolved at submit against the **stored** trees (project and
  named input), the canonical item list is a CAS blob, and the job row carries
  ``split`` / ``split_items_digest`` / ``split_item_count`` / ``split_item_bytes``;
* 0 items, a missing ``list_file``, too many items or chunks
  (``[controller.split]``), and a bundle (not a tree) job are ``400
  invalid_input`` naming the YAML path - and write no row;
* an Idempotency-Key replay returns the same job;
* the GC keeps the item list of a live job;
* with a capable node online the dispatcher refuses the job typed
  (``no_feasible_plan``, ``not_supported_yet``) - no attempt, never run whole.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.jobs.planning import SPLIT_NOT_SUPPORTED_YET
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

GIB = 1024**3
ENTRY = "python main.py {items_file} {out}"


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        split={"max_items": 50, "max_chunks": 5},
        jobs={"dispatch_interval_s": 0.2},
        gc={"min_age_s": 0, "interval_s": 3600},
    )
    async with running_controller(settings) as rt:
        yield rt


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(runtime, token=token_of(runtime)) as client:
        yield client


@pytest_asyncio.fixture
async def agent(runtime: ControllerRuntime) -> AsyncIterator[FakeAgent]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        fake = FakeAgent(channel)
        hardware = agent_pb2.HardwareSummary(
            cpu_model="test",
            cpu_cores=4,
            cpu_threads=8,
            ram_bytes=16 * GIB,
            capabilities=["exec", "exec-subprocess"],
        )
        await fake.connect(hardware=hardware)
        try:
            yield fake
        finally:
            await fake.close()


def _tree(root: Path, files: dict[str, bytes]) -> BuiltTree:
    root.mkdir(parents=True)
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return build_tree(root)


def _store(runtime: ControllerRuntime, built: BuiltTree) -> None:
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)


def _body(built: BuiltTree, split: Any, **manifest: Any) -> dict[str, Any]:
    return {
        "manifest": {"name": "s", "entrypoint": ENTRY, "split": split, **manifest},
        "tree_id": str(built.tree.tree_id),
        "placement": {},
    }


async def _rows(runtime: ControllerRuntime) -> list[dict[str, Any]]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, split, split_items_digest, split_item_count, "
                "split_item_bytes FROM job ORDER BY created_at"
            )
        )
        return [dict(r._mapping) for r in rows]


async def _wait_terminal(runtime: ControllerRuntime, job_id: str) -> Any:
    assert runtime.jobs is not None
    for _ in range(200):
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not end")


# ------------------------------------------------------------------ accepted


async def test_glob_split_is_resolved_stored_and_refused_at_dispatch(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    images = {f"images/{n}.jpg": os.urandom(10 + i) for i, n in enumerate("cAbé")}
    built = _tree(tmp_path / "p", {"main.py": b"print(1)\n", **images})
    _store(runtime, built)
    got = await http.post("/v1/jobs", json=_body(built, {"over": "images/*.jpg"}))
    assert got.status_code == 201, got.text
    job_id = got.json()["job_id"]

    (row,) = await _rows(runtime)
    assert row["id"] == job_id
    assert row["split"]["over"] == {"glob": "images/*.jpg"}
    assert row["split"]["chunk"] == "auto" and row["split"]["combine"] == "concat"
    assert row["split_item_count"] == 4
    assert row["split_item_bytes"] == sum(len(v) for v in images.values())

    # the item list is a CAS blob whose bytes hash to the stored digest
    assert runtime.blobs is not None
    digest = Digest.parse(row["split_items_digest"])
    blob = runtime.blobs.open_path(digest).read_bytes()
    assert sha256_bytes(blob) == digest
    doc = json.loads(blob)
    assert doc["items"] == sorted(images, key=lambda p: p.encode("utf-8"))
    assert doc["items"][0] == "images/A.jpg" and doc["items"][-1] == "images/é.jpg"
    assert [f[0] for f in doc["files"]] == [
        sha256_bytes(images[p]).hex for p in doc["items"]
    ]

    # the record carries it too
    assert runtime.jobs is not None
    record = await runtime.jobs.get_job(job_id)
    assert record is not None and record.split is not None
    assert record.split.items_digest == str(digest)
    assert record.split.item_count == 4

    # never run whole: refused typed, no attempt
    ended = await _wait_terminal(runtime, job_id)
    assert ended.state is JobState.FAILED
    assert ended.failure_type is not None
    assert ended.failure_type.value == "no_feasible_plan"
    assert ended.failure_detail == SPLIT_NOT_SUPPORTED_YET
    assert await runtime.jobs.list_attempts(job_id) == []


async def test_gc_keeps_the_item_list_of_a_live_job(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b"", "a.txt": b"a", "b.txt": b"b"})
    _store(runtime, built)
    got = await http.post("/v1/jobs", json=_body(built, {"over": {"list": ["x", "y"]}}))
    assert got.status_code == 201, got.text
    (row,) = await _rows(runtime)
    assert runtime.gc is not None
    protected = await runtime.gc.protected()
    assert Digest.parse(row["split_items_digest"]).hex in protected.blobs


async def test_list_file_range_and_lines_sources(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    built = _tree(
        tmp_path / "p",
        {"main.py": b"", "ids.txt": b"b\na\n", "t.csv": b"h\n1\n2\n3\n"},
    )
    _store(runtime, built)
    for split, count, item_bytes in [
        ({"over": {"list_file": "ids.txt"}}, 2, None),
        ({"over": {"range": {"start": 0, "stop": 50}}}, 50, None),
        ({"over": {"lines": "t.csv", "header": True}, "combine": "merge-csv"}, 3, 6),
    ]:
        got = await http.post("/v1/jobs", json=_body(built, split))
        assert got.status_code == 201, (split, got.text)
        row = (await _rows(runtime))[-1]
        assert (row["split_item_count"], row["split_item_bytes"]) == (count, item_bytes)


async def test_glob_over_a_named_input_mount(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    data = _tree(tmp_path / "d", {"x/1.png": b"1", "x/2.png": b"22", "x/n.txt": b"n"})
    _store(runtime, data)
    pushed = await http.post(
        "/v1/data", json={"name": "imgs", "tree_id": str(data.tree.tree_id)}
    )
    assert pushed.status_code == 201, pushed.text
    built = _tree(tmp_path / "p", {"main.py": b""})
    _store(runtime, built)
    body = _body(
        built,
        {"over": "in/**/*.png"},
        inputs=[{"ref": "imgs@1", "mount": "in"}],
    )
    got = await http.post("/v1/jobs", json=body)
    assert got.status_code == 201, got.text
    row = (await _rows(runtime))[-1]
    assert row["split_item_count"] == 2 and row["split_item_bytes"] == 3
    assert runtime.blobs is not None
    doc = json.loads(
        runtime.blobs.open_path(Digest.parse(row["split_items_digest"])).read_bytes()
    )
    assert doc["items"] == ["in/x/1.png", "in/x/2.png"]


async def test_idempotent_replay_returns_the_same_job(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b"", "a.jpg": b"a"})
    _store(runtime, built)
    body = _body(built, {"over": "*.jpg"})
    first = await http.post("/v1/jobs", json=body, headers={"idempotency-key": "s-1"})
    again = await http.post("/v1/jobs", json=body, headers={"idempotency-key": "s-1"})
    assert first.status_code == 201 and again.status_code == 200, again.text
    assert first.json()["job_id"] == again.json()["job_id"]
    other = _body(built, {"over": "*.jpg", "chunk": {"items": 1}})
    clash = await http.post("/v1/jobs", json=other, headers={"idempotency-key": "s-1"})
    assert clash.status_code == 409
    assert len(await _rows(runtime)) == 1


# ------------------------------------------------------------------ refused


@pytest.mark.parametrize(
    ("split", "fragment"),
    [
        ({"over": "*.jpg"}, "split.over.glob: matched no files"),
        ({"over": {"list_file": "nope.txt"}}, "split.over.list_file: no such file"),
        ({"over": {"lines": "empty.csv"}}, "split.over.lines: the file has no records"),
        ({"over": {"range": {"stop": 51}}}, "split.over.range: more than 50 items"),
        (
            {"over": {"range": {"stop": 50}}, "chunk": {"items": 8}},
            "split.chunk.items: 50 items in chunks of 8 make 7 chunks, more than 5",
        ),
        ({"over": {}}, "split.over"),
    ],
)
async def test_bad_items_are_400_invalid_input_and_write_nothing(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
    split: Any,
    fragment: str,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b"", "empty.csv": b"\n"})
    _store(runtime, built)
    got = await http.post("/v1/jobs", json=_body(built, split))
    assert got.status_code == 400, got.text
    error = got.json()["error"]
    assert error["code"] == "invalid_input"
    assert fragment in error["message"]
    assert await _rows(runtime) == []


async def test_a_bundle_job_cannot_split(
    runtime: ControllerRuntime, http: httpx.AsyncClient, agent: FakeAgent
) -> None:
    bundle = os.urandom(300)
    digest = sha256_bytes(bundle)
    put = await http.put(f"/v1/blobs/{digest.hex}", content=bundle)
    assert put.status_code == 201
    got = await http.post(
        "/v1/jobs",
        json={
            "manifest": {"name": "s", "entrypoint": ENTRY, "split": {"over": "*"}},
            "bundle_digest": str(digest),
            "placement": {},
        },
    )
    assert got.status_code == 400, got.text
    assert "needs a project tree" in got.json()["error"]["message"]
    assert await _rows(runtime) == []


async def test_placeholders_without_split_are_refused(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b""})
    _store(runtime, built)
    body = _body(built, None)
    del body["manifest"]["split"]
    got = await http.post("/v1/jobs", json=body)
    assert got.status_code == 400
    assert "entrypoint" in got.json()["error"]["message"]


async def test_a_plain_job_keeps_null_split_columns(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b""})
    _store(runtime, built)
    got = await http.post(
        "/v1/jobs",
        json={
            "manifest": {"name": "p", "entrypoint": "python main.py"},
            "tree_id": str(built.tree.tree_id),
            "placement": {},
        },
    )
    assert got.status_code == 201, got.text
    (row,) = await _rows(runtime)
    assert (
        row["split"],
        row["split_items_digest"],
        row["split_item_count"],
        row["split_item_bytes"],
    ) == (None, None, None, None)


async def test_the_database_refuses_a_half_split_row(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b""})
    _store(runtime, built)
    got = await http.post(
        "/v1/jobs",
        json={
            "manifest": {"name": "p", "entrypoint": "python main.py"},
            "tree_id": str(built.tree.tree_id),
            "placement": {},
        },
    )
    assert got.status_code == 201
    assert runtime._engine is not None
    from sqlalchemy.exc import IntegrityError

    for statement in (
        "UPDATE job SET split_item_count = 3",
        "UPDATE job SET split = '{}'::jsonb",
        "UPDATE job SET split = '{}'::jsonb, split_items_digest = 'sha256:"
        + "0" * 64
        + "', split_item_count = 0",
        "UPDATE job SET split = '{}'::jsonb, split_items_digest = 'bad', "
        "split_item_count = 1",
    ):
        with pytest.raises(IntegrityError):
            async with runtime._engine.begin() as conn:
                await conn.execute(text(statement))
