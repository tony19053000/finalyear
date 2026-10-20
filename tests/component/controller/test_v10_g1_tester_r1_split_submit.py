"""V0.10 G1 tester r1: ``POST /v1/jobs`` with ``split:`` under attack.

Real HTTPS + PostgreSQL + the controller's CAS (+ gRPC session where a node is
needed). Covers: submit atomicity (a 400 leaves no row and no new blob),
concurrency on one Idempotency-Key, auth (anonymous, bad token, normal user,
other user), GC (a live job keeps its item list; a finished one does not keep
it forever), dispatch refusal never runs a split job whole (auto and pinned),
caps and huge inputs refused before anything is built.
"""

from __future__ import annotations

import asyncio
import json
import time
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

from mf_users import create_user, login
from sqlalchemy import text

from meshforge.common.hashing import Digest
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

# V0.10 G4: the fake agent advertises no ``exec-split``, so the planner gives
# it no lane (``no_split_support``, static) - the job still never runs whole.
SPLIT_NOT_SUPPORTED_YET = (
    "no node of the fabric can run this job (see compute explain)",
    "node_rejected_no_split_support",
)

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

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


def _blobs(runtime: ControllerRuntime) -> dict[str, int]:
    assert runtime.blobs is not None
    return runtime.blobs.cas.listing()


async def _rows(runtime: ControllerRuntime) -> list[dict[str, Any]]:
    assert runtime._engine is not None
    async with runtime._engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT id, state, owner_user_id, split, split_items_digest, "
                "split_item_count, split_item_bytes FROM job ORDER BY created_at"
            )
        )
        return [dict(r._mapping) for r in rows]


async def _wait_terminal(runtime: ControllerRuntime, job_id: str) -> Any:
    assert runtime.jobs is not None
    for _ in range(300):
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.FAILED, JobState.SUCCEEDED, JobState.CANCELLED):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError(f"job {job_id} did not end")


async def _stop_dispatcher(runtime: ControllerRuntime) -> None:
    task = runtime._dispatcher_task
    assert task is not None
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ----------------------------------------------------------------- atomicity


@pytest.mark.parametrize(
    ("files", "split", "fragment"),
    [
        ({}, {"over": "none/*.x"}, "matched no files"),
        ({"ids.txt": b"a\nb\na\n"}, {"over": {"list_file": "ids.txt"}}, "repeats"),
        ({"ids.txt": b"\xff\xfe\x00"}, {"over": {"list_file": "ids.txt"}}, "UTF-8"),
        ({"ids.txt": b"a\x00b\n"}, {"over": {"list_file": "ids.txt"}}, "NUL"),
        ({"ids.txt": b""}, {"over": {"list_file": "ids.txt"}}, "no items"),
        (
            {"ids.txt": b"".join(b"i%d\n" % i for i in range(51))},
            {"over": {"list_file": "ids.txt"}},
            "more than 50",
        ),
        ({"r.csv": b"h\n"}, {"over": {"lines": "r.csv", "header": True}}, "no records"),
        ({}, {"over": {"range": {"stop": 2**53 - 1}}}, "more than 50"),
        ({}, {"over": {"range": {"start": 3, "stop": 3}}}, "empty"),
        ({}, {"over": {"list": ["a", "a"]}}, "repeats"),
        ({}, {"over": "../../etc/passwd"}, ".."),
        ({}, {"over": {"list_file": "C:\\\\x.txt"}}, "split.over.list_file"),
        ({}, {"over": {"lines": "/etc/passwd"}}, "relative"),
        ({}, {"over": {"list_file": "../x"}}, ".."),
        ({}, {"over": "*", "chunk": {"items": 0}}, "split.chunk"),
        ({}, {"over": "*", "combine": {"script": ["x", "{items_file}"]}}, "combine"),
    ],
)
async def test_a_refused_submit_leaves_no_row_and_no_new_blob(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
    files: dict[str, bytes],
    split: Any,
    fragment: str,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b"x", **files})
    _store(runtime, built)
    before = _blobs(runtime)
    got = await http.post("/v1/jobs", json=_body(built, split))
    assert got.status_code == 400, got.text
    error = got.json()["error"]
    assert error["code"] == "invalid_input"
    assert fragment in error["message"], error["message"]
    assert await _rows(runtime) == []
    assert _blobs(runtime) == before  # no item-list blob stored for a refused job


async def test_a_refused_submit_then_a_fixed_one_works(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b"", "a.txt": b"a"})
    _store(runtime, built)
    bad = await http.post("/v1/jobs", json=_body(built, {"over": "*.csv"}))
    assert bad.status_code == 400
    good = await http.post("/v1/jobs", json=_body(built, {"over": "*.txt"}))
    assert good.status_code == 201, good.text
    assert len(await _rows(runtime)) == 1


async def test_unknown_tree_id_is_refused_before_any_item_work(
    runtime: ControllerRuntime, http: httpx.AsyncClient, agent: FakeAgent
) -> None:
    before = _blobs(runtime)
    body = {
        "manifest": {"name": "s", "entrypoint": ENTRY, "split": {"over": "*"}},
        "tree_id": "sha256:" + "ab" * 32,
        "placement": {},
    }
    got = await http.post("/v1/jobs", json=body)
    assert got.status_code >= 400 and got.status_code < 500, got.text
    assert await _rows(runtime) == []
    assert _blobs(runtime) == before


# --------------------------------------------------- idempotency, concurrency


async def test_twenty_concurrent_submits_with_one_key_make_one_job_one_item_blob(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    await _stop_dispatcher(runtime)
    built = _tree(
        tmp_path / "p", {"main.py": b"", **{f"d/{i}.t": b"%d" % i for i in range(9)}}
    )
    _store(runtime, built)
    before = set(_blobs(runtime))
    body = _body(built, {"over": "d/*.t"})
    results = await asyncio.gather(
        *[
            http.post("/v1/jobs", json=body, headers={"idempotency-key": "race-1"})
            for _ in range(20)
        ]
    )
    codes = sorted(r.status_code for r in results)
    assert set(codes) <= {200, 201}, [r.text for r in results if r.status_code >= 400]
    assert codes.count(201) == 1
    assert len({r.json()["job_id"] for r in results}) == 1
    rows = await _rows(runtime)
    assert len(rows) == 1
    assert len(set(_blobs(runtime)) - before) == 1  # exactly one new blob: the list


async def test_distinct_jobs_with_the_same_items_share_one_list_blob_and_gc_keeps_it(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    await _stop_dispatcher(runtime)
    built = _tree(tmp_path / "p", {"main.py": b"", "a.t": b"a", "b.t": b"b"})
    _store(runtime, built)
    before = set(_blobs(runtime))
    ids = []
    for _ in range(5):
        got = await http.post("/v1/jobs", json=_body(built, {"over": "*.t"}))
        assert got.status_code == 201, got.text
        ids.append(got.json()["job_id"])
    rows = await _rows(runtime)
    assert len({r["split_items_digest"] for r in rows}) == 1
    digest = Digest.parse(rows[0]["split_items_digest"])
    assert len(set(_blobs(runtime)) - before) == 1
    # GC with min_age 0 while all five are QUEUED: the list stays
    assert runtime.gc is not None
    await runtime.gc.run_pass("test")
    assert digest.hex in _blobs(runtime)
    # cancel four: the fifth still needs it
    for job_id in ids[:4]:
        assert (await http.post(f"/v1/jobs/{job_id}/cancel")).status_code in (200, 202)
    await runtime.gc.run_pass("test")
    assert digest.hex in _blobs(runtime)
    assert runtime.blobs is not None
    assert json.loads(runtime.blobs.open_path(digest).read_bytes())["items"] == [
        "a.t",
        "b.t",
    ]


async def test_gc_collects_the_item_list_once_no_live_job_needs_it(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    await _stop_dispatcher(runtime)
    assert runtime.gc is not None and runtime.blobs is not None
    built = _tree(tmp_path / "p", {"main.py": b"", "a.t": b"a"})
    _store(runtime, built)
    got = await http.post("/v1/jobs", json=_body(built, {"over": "*.t"}))
    assert got.status_code == 201
    (row,) = await _rows(runtime)
    digest = Digest.parse(row["split_items_digest"])
    assert (await http.post(f"/v1/jobs/{got.json()['job_id']}/cancel")).status_code in (
        200,
        202,
    )
    ended = await _wait_terminal(runtime, got.json()["job_id"])
    assert ended.state is JobState.CANCELLED
    await runtime.gc.run_pass("test")
    # "no orphan kept beyond GC rules": GC rules for unpinned blobs of min_age 0 = evictable;
    # whatever the policy, the list must not be pinned by a terminal job.
    protected = await runtime.gc.protected()
    assert digest.hex not in protected.blobs


# ----------------------------------------------------------------------- auth


async def test_anonymous_and_bad_token_submit_is_401_and_stores_nothing(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b"", "a.t": b"a"})
    _store(runtime, built)
    before = _blobs(runtime)
    body = _body(built, {"over": "*.t"})
    async with api_client(runtime, token=None) as anon:
        assert (await anon.post("/v1/jobs", json=body)).status_code == 401
    async with api_client(runtime, token="not-a-token-" + "x" * 30) as bad:
        assert (await bad.post("/v1/jobs", json=body)).status_code == 401
    assert await _rows(runtime) == []
    assert _blobs(runtime) == before


async def test_normal_user_can_submit_a_split_job_and_others_cannot_touch_it(
    runtime: ControllerRuntime, agent: FakeAgent, tmp_path: Path
) -> None:
    await _stop_dispatcher(runtime)
    assert runtime._engine is not None
    alice_id = await create_user(runtime._engine, "alice", "alice-password-123")
    await create_user(runtime._engine, "mallory", "mallory-password-1")
    built = _tree(tmp_path / "p", {"main.py": b"", "a.t": b"a", "b.t": b"b"})
    _store(runtime, built)
    async with api_client(runtime) as anon:
        alice_token = await login(anon, "alice", "alice-password-123")
        mallory_token = await login(anon, "mallory", "mallory-password-1")
    async with api_client(runtime, token=alice_token) as alice:
        got = await alice.post("/v1/jobs", json=_body(built, {"over": "*.t"}))
        assert got.status_code == 201, got.text
        job_id = got.json()["job_id"]
        assert (await alice.get(f"/v1/jobs/{job_id}")).status_code == 200
    (row,) = await _rows(runtime)
    assert row["owner_user_id"] == alice_id and row["split_item_count"] == 2
    async with api_client(runtime, token=mallory_token) as mallory:
        seen = await mallory.get(f"/v1/jobs/{job_id}")
        assert seen.status_code in (403, 404), seen.text
        cancelled = await mallory.post(f"/v1/jobs/{job_id}/cancel")
        assert cancelled.status_code in (403, 404), cancelled.text
        listed = await mallory.get("/v1/jobs")
        assert job_id not in json.dumps(listed.json())
    (still,) = await _rows(runtime)
    assert still["state"] == "queued"
    # a normal user hits the same refusals: no special path for non-admins
    async with api_client(runtime, token=alice_token) as alice:
        bad = await alice.post("/v1/jobs", json=_body(built, {"over": "*.csv"}))
        assert bad.status_code == 400
    assert len(await _rows(runtime)) == 1


async def test_normal_user_cannot_split_over_another_users_private_data(
    runtime: ControllerRuntime, agent: FakeAgent, tmp_path: Path
) -> None:
    """Items may only be read from the submitter's own tree and named inputs
    the submitter can resolve (the controller never reads anything else)."""
    assert runtime._engine is not None
    await create_user(runtime._engine, "mallory", "mallory-password-1")
    built = _tree(tmp_path / "p", {"main.py": b"", "a.t": b"a"})
    _store(runtime, built)
    async with api_client(runtime) as anon:
        token = await login(anon, "mallory", "mallory-password-1")
    async with api_client(runtime, token=token) as mallory:
        body = _body(
            built,
            {"over": "secrets/*"},
            inputs=[{"ref": "nobody-owns-this@1", "mount": "secrets"}],
        )
        got = await mallory.post("/v1/jobs", json=body)
        assert got.status_code in (400, 403, 404, 409), got.text
    assert await _rows(runtime) == []


# --------------------------------------------------------- dispatch refusal


@pytest.mark.parametrize("pinned", [False, True])
async def test_a_split_job_is_never_run_whole_auto_or_pinned(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
    pinned: bool,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b"print(1)", "a.t": b"a", "b.t": b"b"})
    _store(runtime, built)
    body = _body(built, {"over": "*.t"})
    if pinned:
        body["placement"] = {"node_id": agent.node_id}
    got = await http.post("/v1/jobs", json=body)
    assert got.status_code == 201, got.text
    ended = await _wait_terminal(runtime, got.json()["job_id"])
    assert ended.state is JobState.FAILED
    assert (
        ended.failure_type is not None
        and ended.failure_type.value == "no_feasible_plan"
    )
    assert ended.failure_detail in SPLIT_NOT_SUPPORTED_YET
    assert runtime.jobs is not None
    assert await runtime.jobs.list_attempts(got.json()["job_id"]) == []
    await asyncio.sleep(0.6)  # more dispatch ticks: still nothing sent to the node
    kinds = [m.WhichOneof("payload") for m in agent.received]
    assert "start_attempt" not in kinds and "prepare_env" not in kinds, kinds
    assert await runtime.jobs.list_attempts(got.json()["job_id"]) == []


async def test_a_plain_job_next_to_a_split_job_is_not_affected_by_the_refusal(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    built = _tree(tmp_path / "p", {"main.py": b"print(1)", "a.t": b"a"})
    _store(runtime, built)
    split = await http.post("/v1/jobs", json=_body(built, {"over": "*.t"}))
    plain = await http.post(
        "/v1/jobs",
        json={
            "manifest": {"name": "plain", "entrypoint": "python main.py"},
            "tree_id": str(built.tree.tree_id),
            "placement": {},
        },
    )
    assert split.status_code == 201 and plain.status_code == 201, plain.text
    ended = await _wait_terminal(runtime, split.json()["job_id"])
    assert ended.failure_detail in SPLIT_NOT_SUPPORTED_YET
    plain_row = next(
        r for r in await _rows(runtime) if r["id"] == plain.json()["job_id"]
    )
    assert plain_row["split"] is None and plain_row["split_items_digest"] is None
    assert (
        plain_row["split_item_count"] is None and plain_row["split_item_bytes"] is None
    )
    assert plain_row["state"] != "failed"  # the refusal is the split job's alone


# -------------------------------------------------------------- caps / huge


async def test_huge_range_and_boundaries_over_http(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    await _stop_dispatcher(runtime)
    built = _tree(tmp_path / "p", {"main.py": b""})
    _store(runtime, built)
    t0 = time.monotonic()
    for stop in (2**53 - 1, 10**9, 51):
        got = await http.post(
            "/v1/jobs", json=_body(built, {"over": {"range": {"stop": stop}}})
        )
        assert (
            got.status_code == 400 and "more than 50" in got.json()["error"]["message"]
        )
    got = await http.post(
        "/v1/jobs", json=_body(built, {"over": {"range": {"stop": 2**53}}})
    )
    assert (
        got.status_code == 400
        and "split.over.range.stop" in got.json()["error"]["message"]
    )
    assert time.monotonic() - t0 < 10
    ok = await http.post(
        "/v1/jobs", json=_body(built, {"over": {"range": {"stop": 50}}})
    )
    assert ok.status_code == 201, ok.text
    neg = await http.post(
        "/v1/jobs",
        json=_body(built, {"over": {"range": {"start": 100, "stop": 0, "step": -2}}}),
    )
    assert neg.status_code == 201, neg.text  # exactly 50 items
    assert [r["split_item_count"] for r in await _rows(runtime)] == [50, 50]


async def test_oversized_list_file_is_refused_by_size_before_reading(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    root = tmp_path / "p"
    root.mkdir()
    (root / "main.py").write_bytes(b"")
    with (root / "big.txt").open("wb") as fh:  # 64 MiB + 1: one over the default cap
        fh.truncate(64 * 1024 * 1024 + 1)
    built = build_tree(root)
    _store(runtime, built)
    before = _blobs(runtime)
    t0 = time.monotonic()
    got = await http.post(
        "/v1/jobs", json=_body(built, {"over": {"list_file": "big.txt"}})
    )
    assert got.status_code == 400, got.text
    assert "larger than" in got.json()["error"]["message"]
    assert time.monotonic() - t0 < 20
    assert _blobs(runtime) == before and await _rows(runtime) == []


async def test_many_files_glob_cap_refuses_without_storing(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    built = _tree(
        tmp_path / "p", {"main.py": b"", **{f"f/{i:04d}.t": b"x" for i in range(300)}}
    )
    _store(runtime, built)
    before = _blobs(runtime)
    got = await http.post("/v1/jobs", json=_body(built, {"over": "f/*.t"}))
    assert got.status_code == 400 and "more than 50" in got.json()["error"]["message"]
    assert _blobs(runtime) == before and await _rows(runtime) == []


async def test_unicode_and_odd_names_round_trip_through_the_stored_list(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    await _stop_dispatcher(runtime)
    names = ["日本/ß.t", "é.t", "Z.t", "a b.t", "😀.t", "q'uo\"te.t"]
    built = _tree(tmp_path / "p", {"main.py": b"", **{n: n.encode() for n in names}})
    _store(runtime, built)
    got = await http.post("/v1/jobs", json=_body(built, {"over": "**/*.t"}))
    assert got.status_code == 201, got.text
    (row,) = await _rows(runtime)
    assert runtime.blobs is not None
    doc = json.loads(
        runtime.blobs.open_path(Digest.parse(row["split_items_digest"])).read_bytes()
    )
    assert doc["items"] == sorted(names, key=lambda n: n.encode("utf-8"))


async def test_list_file_inside_a_named_input_resolved_by_the_controller(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: FakeAgent,
    tmp_path: Path,
) -> None:
    await _stop_dispatcher(runtime)
    data = _tree(tmp_path / "d", {"ids.txt": b"x\r\ny\r\n\r\nz"})
    _store(runtime, data)
    pushed = await http.post(
        "/v1/data", json={"name": "ids", "tree_id": str(data.tree.tree_id)}
    )
    assert pushed.status_code == 201, pushed.text
    built = _tree(tmp_path / "p", {"main.py": b""})
    _store(runtime, built)
    got = await http.post(
        "/v1/jobs",
        json=_body(
            built,
            {"over": {"list_file": "in/ids.txt"}},
            inputs=[{"ref": "ids@1", "mount": "in"}],
        ),
    )
    assert got.status_code == 201, got.text
    (row,) = await _rows(runtime)
    assert row["split_item_count"] == 3
