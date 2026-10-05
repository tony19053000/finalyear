"""V0.5 G5 tester round 1 - end to end over real PostgreSQL + gRPC + REST + agent.

* GC (controller AND agent, quota 1 byte, min age 0) racing ten concurrent jobs:
  every accepted job must still run to success (the guard + leases hold);
* named inputs: unicode mounts, two mounts of one tree, hostile mounts refused
  at submit, the workdir (with read-only inputs) is removed after the job;
* scrub: a controller blob corrupted on disk fails the attempt typed
  (integrity_error), is scrubbed, a re-upload repairs it, the rerun succeeds;
* telemetry: a FAILED attempt still reports transfer rows, a repeat run reports
  cache hits only, project + input trees each have their own rows;
* node prepare: two concurrent requests and a repeated one all finish.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_exec_harness import ExecAgent, wait_job
from mf_jobs_harness import token_of
from pydantic import SecretStr

from meshforge.agent.envs.builder import default_uv_argv
from meshforge.agent.gc.collector import GcConfig
from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.hashing import sha256_bytes
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

CS = MIN_CHUNK_SIZE


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        blobs={"max_blob_bytes": 64 * 1024 * 1024},
        gc={"interval_s": 3600},
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=20,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


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


def _folder(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(parents=True)
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


async def _push(
    runtime: ControllerRuntime, http: httpx.AsyncClient, name: str, folder: Path
) -> dict[str, Any]:
    built = build_tree(folder, chunk_size=CS)
    await _upload(runtime, built)
    got = await http.post(
        "/v1/data",
        json={"name": name, "tree_id": str(built.tree.tree_id), "pinned": True},
    )
    assert got.status_code in (200, 201), got.text
    return dict(got.json())


async def _submit(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    project: Path,
    node_id: str,
    inputs: list[dict[str, str]] | None = None,
    entrypoint: list[str] | None = None,
) -> httpx.Response:
    built = build_tree(project, chunk_size=CS)
    await _upload(runtime, built)
    manifest: dict[str, Any] = {
        "name": "e2e",
        "entrypoint": entrypoint or ["python", "main.py"],
    }
    if inputs is not None:
        manifest["inputs"] = inputs
    return await http.post(
        "/v1/jobs",
        json={
            "manifest": manifest,
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": node_id},
        },
    )


async def _stdout(http: httpx.AsyncClient, job_id: str) -> str:
    got = await http.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"})
    assert got.status_code == 200, got.text
    return got.text


# ------------------------------------------------------------------ GC race


async def test_gc_on_both_sides_never_breaks_ten_concurrent_jobs(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    assert runtime.gc is not None and agent.gc is not None
    runtime.gc.quota_bytes, runtime.gc.min_age_s = (
        1,
        0.5,
    )  # harsh; min_age = upload->submit window
    agent.gc.collector.config = GcConfig(cas_bytes=1, envs_bytes=1, min_age_s=0)
    stop = asyncio.Event()

    async def hammer() -> int:
        n = 0
        while not stop.is_set():
            await runtime.gc.run_pass("race")  # type: ignore[union-attr]
            await agent.gc.run_now("race")  # type: ignore[union-attr]
            n += 1
            await asyncio.sleep(0.05)
        return n

    async def one(i: int) -> dict[str, Any]:
        project = _folder(
            tmp_path / f"p{i}",
            {
                "main.py": (
                    b"print(open('lib.txt').read().strip(), "
                    b"len(open('d.bin','rb').read()))\n"
                ),
                "lib.txt": f"lib{i}\n".encode(),
                "d.bin": os.urandom(2 * CS + i),
            },
        )
        # min_age_s = 0 lets a pass evict a blob between its upload and the
        # submit (documented: min_age is that window); the CLI just re-uploads.
        for _ in range(50):
            posted = await _submit(runtime, http, project, agent.node_id)
            if posted.status_code != 409:
                break
        assert posted.status_code == 201, posted.text
        job = await wait_job(http, str(posted.json()["job_id"]), timeout_s=120)
        job["_out"] = await _stdout(http, job["job_id"])
        return job

    runner = asyncio.create_task(hammer())
    try:
        jobs = await asyncio.gather(*(one(i) for i in range(10)))
    finally:
        stop.set()
        passes = await runner
    assert passes >= 1
    for i, job in enumerate(jobs):
        assert job["state"] == "succeeded", job
        assert f"lib{i} {2 * CS + i}" in job["_out"], job["_out"]


# ------------------------------------------------------------ named inputs


async def test_unicode_and_repeated_mounts_work_and_the_workdir_is_cleaned(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
    data_dir: Path,
) -> None:
    await _push(runtime, http, "ds", _folder(tmp_path / "ds", {"a.txt": b"alpha\n"}))
    project = _folder(
        tmp_path / "p",
        {
            "main.py": (
                "import pathlib\n"
                "for m in ('donn\u00e9es', 'x/y'):\n"
                "    print(m, pathlib.Path(m, 'a.txt').read_text().strip())\n"
            ).encode()
        },
    )
    posted = await _submit(
        runtime,
        http,
        project,
        agent.node_id,
        [
            {"ref": "ds@1", "mount": "donn\u00e9es"},
            {"ref": "ds@1", "mount": "x/y"},
        ],
    )
    assert posted.status_code == 201, posted.text
    job = await wait_job(http, str(posted.json()["job_id"]))
    assert job["state"] == "succeeded", job
    out = await _stdout(http, job["job_id"])
    assert "donn\u00e9es alpha" in out and "x/y alpha" in out
    # The attempt's workdir (read-only input folders inside) is gone.
    work = data_dir / "work"
    deadline = time.monotonic() + 10
    while work.exists() and any(work.iterdir()):
        assert time.monotonic() < deadline, (
            f"workdir left behind: {list(work.iterdir())}"
        )
        await asyncio.sleep(0.1)


@pytest.mark.parametrize(
    "mount",
    [
        "..",
        "../x",
        "/abs",
        "C:\\x",
        "C:/x",
        "a\\b",
        ".meshforge",
        ".meshforge/in",
        "a/../b",
        "",
        ".",
        "a//b",
        "a\x00b",
        "x" * 5000,
        "~",
        "CON",
    ],
)
async def test_hostile_mounts_are_refused_at_submit_never_accepted_or_500(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
    mount: str,
) -> None:
    await _push(runtime, http, "ds", _folder(tmp_path / "ds", {"a.txt": b"a"}))
    project = _folder(tmp_path / "p", {"main.py": b"print(1)\n"})
    got = await _submit(
        runtime, http, project, agent.node_id, [{"ref": "ds@1", "mount": mount}]
    )
    # "CON" and "~" are legal folder names on Linux: accepting is fine, but
    # a refusal must be a 4xx and nothing may be a 5xx.
    assert got.status_code < 500, (mount, got.status_code, got.text)
    if mount not in ("CON", "~"):
        assert got.status_code in (400, 422), (mount, got.status_code, got.text)


async def test_nested_and_duplicate_mounts_are_refused(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    await _push(runtime, http, "ds", _folder(tmp_path / "ds", {"a.txt": b"a"}))
    project = _folder(tmp_path / "p", {"main.py": b"print(1)\n"})
    for mounts in (
        ["a", "a/b"],
        ["a/b", "a"],
        ["a", "a"],
        [f"m{i}" for i in range(17)],
    ):
        got = await _submit(
            runtime,
            http,
            project,
            agent.node_id,
            [{"ref": "ds@1", "mount": m} for m in mounts],
        )
        assert got.status_code in (400, 422), (mounts, got.status_code, got.text)
    sixteen = await _submit(
        runtime,
        http,
        project,
        agent.node_id,
        [{"ref": "ds@1", "mount": f"m{i}"} for i in range(16)],
    )
    assert sixteen.status_code == 201, sixteen.text  # max accepted
    job = await wait_job(http, str(sixteen.json()["job_id"]), timeout_s=60)
    assert job["state"] == "succeeded", job


async def test_a_job_that_tries_to_rm_its_input_cannot_damage_the_cache(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    """Deviation 6: read-only inputs are a guard, not isolation. What must
    hold: a job that rewrites its mounted input (chmod + write, or unlink)
    never changes the next job's view of that input."""
    await _push(runtime, http, "ds", _folder(tmp_path / "ds", {"a.txt": b"original\n"}))
    vandal = _folder(
        tmp_path / "v",
        {
            "main.py": (
                b"import os, pathlib, stat\n"
                b"p = pathlib.Path('in/a.txt')\n"
                b"try:\n"
                b"    os.chmod(p, 0o644); os.chmod('in', 0o755)\n"
                b"    p.write_text('VANDALISED\\n')\n"
                b"except OSError as e:\n"
                b"    print('blocked', type(e).__name__)\n"
            )
        },
    )
    reader = _folder(
        tmp_path / "r",
        {"main.py": b"print(open('in/a.txt').read().strip())\n"},
    )
    ins = [{"ref": "ds@1", "mount": "in"}]
    for project in (vandal, reader):
        posted = await _submit(runtime, http, project, agent.node_id, ins)
        job = await wait_job(http, str(posted.json()["job_id"]))
        assert job["state"] == "succeeded", job
    out = await _stdout(http, job["job_id"])
    assert "original" in out and "VANDALISED" not in out, out
    # The cached blob itself is intact (inputs are copies, not links to the CAS).
    assert agent.executor.cas.store.verify(sha256_bytes(b"original\n"))


# ------------------------------------------------------------------- scrub


@pytest.mark.parametrize(
    "where",
    ["project", "input"],
)
async def test_a_corrupted_controller_blob_fails_typed_is_scrubbed_and_repairable(
    where: str,
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    tmp_path: Path,
    grpc_target: str,
    data_dir: Path,
) -> None:
    assert runtime.blobs is not None and runtime.scrubber is not None
    # A plain agent (no GC): the only thing that can go wrong is the blob.
    plain = await ExecAgent.start(grpc_target, data_dir / "plain")
    try:
        data = b"dataset bytes\n" * 50
        if where == "input":
            folder = _folder(tmp_path / "ds", {"a.txt": data})
            project = _folder(
                tmp_path / "p",
                {"main.py": b"print(len(open('in/a.txt','rb').read()))\n"},
            )
            built = build_tree(folder, chunk_size=CS)
            await _upload(runtime, built)
            reg = await http.post(
                "/v1/data",
                json={"name": "ds", "tree_id": str(built.tree.tree_id), "pinned": True},
            )
            assert reg.status_code == 201, reg.text
            ins: list[dict[str, str]] | None = [{"ref": "ds@1", "mount": "in"}]
        else:
            project = _folder(
                tmp_path / "p",
                {
                    "main.py": b"print(len(open('in.txt','rb').read()))\n",
                    "in.txt": data,
                },
            )
            ins = None
            await _upload(runtime, build_tree(project, chunk_size=CS))
        path = runtime.blobs.path_for(sha256_bytes(data))
        path.chmod(0o600)
        path.write_bytes(data[:-3] + b"BAD")  # same size, other bytes
        posted = await _submit(runtime, http, project, plain.node_id, ins)
        assert posted.status_code == 201, posted.text
        job = await wait_job(http, str(posted.json()["job_id"]), timeout_s=60)
        assert job["state"] == "failed", job
        # The first attempt fails typed. integrity_error is retryable (V0.6
        # D110); by the retry the scrubber has removed the blob, so the job
        # ends with its last attempt's not_found (or integrity_error if the
        # retries outran the scrubber).
        attempts = job["attempts"]
        assert attempts[0]["failure_type"] == "integrity_error", attempts
        assert job["failure_type"] == attempts[-1]["failure_type"], job
        assert job["failure_type"] in {"integrity_error", "not_found"}, job
        # The controller notices (dispatcher -> scrubber) and removes the blob.
        deadline = time.monotonic() + 20
        while runtime.blobs.cas.has(sha256_bytes(data)):
            assert time.monotonic() < deadline, "corrupt blob never scrubbed"
            await asyncio.sleep(0.1)
        assert runtime.scrubber.corrupt >= 1
        # Repair = upload again; the next run succeeds.
        if where == "input":
            await _upload(runtime, built)
        posted = await _submit(runtime, http, project, plain.node_id, ins)
        job = await wait_job(http, str(posted.json()["job_id"]), timeout_s=60)
        assert job["state"] == "succeeded", job
        assert str(len(data)) in await _stdout(http, job["job_id"])
    finally:
        await plain.stop()


# --------------------------------------------------------------- telemetry


async def test_a_failed_attempt_reports_transfers_and_a_repeat_run_only_cache_hits(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    grpc_target: str,
    data_dir: Path,
    tmp_path: Path,
) -> None:
    agent = await ExecAgent.start(grpc_target, data_dir / "tel")  # default GC quotas
    try:
        await _telemetry_body(runtime, http, agent, tmp_path)
    finally:
        await agent.stop()


async def _telemetry_body(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    await _push(
        runtime, http, "ds", _folder(tmp_path / "ds", {"a.bin": os.urandom(CS + 5)})
    )
    project = _folder(
        tmp_path / "p",
        {"main.py": b"import sys\nprint('x')\nsys.exit(3)\n"},
    )
    ins = [{"ref": "ds@1", "mount": "in"}]
    rows: list[list[dict[str, Any]]] = []
    for _ in range(2):
        posted = await _submit(runtime, http, project, agent.node_id, ins)
        job = await wait_job(http, str(posted.json()["job_id"]))
        assert job["state"] == "failed" and job["failure_type"] == "user_error", job
        detail = (await http.get(f"/v1/jobs/{job['job_id']}")).json()
        (attempt,) = detail["attempts"]
        rows.append(attempt["transfers"])
    first, second = rows
    assert len({r["tree_id"] for r in first}) == 2  # project tree + input tree
    assert sum(r["bytes"] for r in first if r["source"] == "controller") > 0
    assert second, "a failed attempt must still report its transfers"
    # Repeat run: nothing travels (zero-usage rows of the controller may appear).
    assert all(
        r["bytes"] == 0 and r["units"] == 0 for r in second if r["source"] != "cache"
    ), second
    assert all(r["units"] >= 1 for r in second if r["source"] == "cache"), second
    assert sum(r["bytes"] for r in second) > 0  # hits are accounted as bytes


# ----------------------------------------------------------------- prepare


@pytest.mark.skipif(
    not default_uv_argv(), reason="the uv wheel (agent extra) is absent"
)
async def test_concurrent_and_repeated_prepare_requests_all_finish(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    grpc_target: str,
    data_dir: Path,
) -> None:
    plain = await ExecAgent.start(grpc_target, data_dir / "prep")
    try:
        body = {
            "images": [],
            "envs": [
                {"base": "python-cpu", "requirements": "", "runtime": "subprocess"}
            ],
        }
        posts = await asyncio.gather(
            *(
                http.post(f"/v1/nodes/{plain.node_id}/prepare", json=body)
                for _ in range(3)
            )
        )
        assert [p.status_code for p in posts] == [202] * 3
        ids = [p.json()["request_id"] for p in posts]
        assert len(set(ids)) == 3
        results = []
        deadline = time.monotonic() + 200
        for rid in ids:
            while True:
                got = (
                    await http.get(f"/v1/nodes/{plain.node_id}/prepare/{rid}")
                ).json()
                if got["done"]:
                    results.append(got)
                    break
                assert time.monotonic() < deadline, got
                await asyncio.sleep(0.1)
        keys = set()
        for res in results:
            states = {i["state"] for i in res["items"]}
            # one request builds, the rest hit (or are refused busy, typed)
            assert states <= {"built", "hit", "failed"}, res
            for item in res["items"]:
                if item["state"] == "failed":
                    assert item["reason"], item  # typed, never empty
                elif item["kind"] == "env":
                    keys.add(item["key"])
        assert len(keys) <= 1  # same spec -> same env hash
        assert any(r["ok"] for r in results), results
        # A status for another node's id never leaks the request.
        other = await http.get(
            f"/v1/nodes/node_01HZZZZZZZZZZZZZZZZZZZZZZZ/prepare/{ids[0]}"
        )
        assert other.status_code in (400, 404)
    finally:
        await plain.stop()
