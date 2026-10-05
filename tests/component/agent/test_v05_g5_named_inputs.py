"""V0.5 G5 D92 end to end: named inputs over real PostgreSQL + gRPC + REST + agent.

``compute data push`` is done the way the CLI does it (upload_tree + POST
/v1/data); jobs name the input in their manifest and the agent copies it
read-only under the workdir. Covers resolve by version and by tree id, unknown
refs, mounts that overlap the project, ``rm`` refused while a job uses the
version, and pins reaching the agent's GC.
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

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

CS = MIN_CHUNK_SIZE
READER = b"""\
import pathlib, sys
data = pathlib.Path("data/ds")
print(sorted(p.name for p in data.iterdir()))
print((data / "labels.csv").read_text().strip())
try:
    (data / "labels.csv").write_text("changed")
except OSError as exc:
    print("write refused:", type(exc).__name__)
else:
    sys.exit("the input was writable")
try:
    (data / "new.txt").write_text("x")
except OSError as exc:
    print("create refused:", type(exc).__name__)
else:
    sys.exit("the input folder was writable")
"""


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        blobs={"max_blob_bytes": 64 * 1024 * 1024},
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
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
    root.mkdir()
    for name, data in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    return root


async def _push(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    name: str,
    folder: Path,
    *,
    pinned: bool = False,
) -> dict[str, Any]:
    built = build_tree(folder, chunk_size=CS)
    await _upload(runtime, built)
    got = await http.post(
        "/v1/data",
        json={"name": name, "tree_id": str(built.tree.tree_id), "pinned": pinned},
    )
    assert got.status_code in (200, 201), got.text
    return dict(got.json())


async def _submit(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    project: Path,
    node_id: str,
    inputs: list[dict[str, str]],
) -> httpx.Response:
    built = build_tree(project, chunk_size=CS)
    await _upload(runtime, built)
    return await http.post(
        "/v1/jobs",
        json={
            "manifest": {
                "name": "inputs",
                "entrypoint": ["python", "main.py"],
                "inputs": inputs,
            },
            "tree_id": str(built.tree.tree_id),
            "placement": {"node_id": node_id},
        },
    )


async def _logs(http: httpx.AsyncClient, job_id: str) -> str:
    got = await http.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"})
    assert got.status_code == 200, got.text
    return got.text


async def test_a_job_reads_its_named_input_read_only(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    dataset = _folder(
        tmp_path / "ds",
        {"labels.csv": b"cat,dog\n", "images/0.bin": os.urandom(2 * CS + 3)},
    )
    first = await _push(runtime, http, "pets", dataset)
    assert first["version"] == 1 and first["created"] is True
    again = await _push(runtime, http, "pets", dataset)  # idempotent
    assert again["version"] == 1 and again["created"] is False
    project = _folder(tmp_path / "p", {"main.py": READER})
    posted = await _submit(
        runtime, http, project, agent.node_id, [{"ref": "pets@1", "mount": "data/ds"}]
    )
    assert posted.status_code == 201, posted.text
    job = await wait_job(http, str(posted.json()["job_id"]))
    assert job["state"] == "succeeded", job
    out = await _logs(http, job["job_id"])
    assert "['images', 'labels.csv']" in out
    assert "cat,dog" in out
    assert "write refused" in out and "create refused" in out
    # By tree id, too; a second version is a new version.
    (dataset / "labels.csv").write_bytes(b"cat,dog,cow\n")
    second = await _push(runtime, http, "pets", dataset)
    assert second["version"] == 2
    by_tree = await _submit(
        runtime,
        http,
        project,
        agent.node_id,
        [{"ref": f"pets@{first['tree_id']}", "mount": "data/ds"}],
    )
    job = await wait_job(http, str(by_tree.json()["job_id"]))
    assert job["state"] == "succeeded", job
    assert "cat,dog\n" in await _logs(http, job["job_id"])  # version 1's bytes


async def test_unknown_refs_and_overlapping_mounts_are_refused_at_submit(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    await _push(runtime, http, "ds", _folder(tmp_path / "ds", {"a.txt": b"a"}))
    project = _folder(
        tmp_path / "p", {"main.py": b"print(1)\n", "data/own.txt": b"mine\n"}
    )
    unknown = await _submit(
        runtime, http, project, agent.node_id, [{"ref": "ds@9", "mount": "x"}]
    )
    assert unknown.status_code == 404, unknown.text
    overlap = await _submit(
        runtime, http, project, agent.node_id, [{"ref": "ds@1", "mount": "data"}]
    )
    assert overlap.status_code == 400 and "overlaps" in overlap.text
    inside = await _submit(
        runtime,
        http,
        project,
        agent.node_id,
        [{"ref": "ds@1", "mount": "data/own.txt/x"}],
    )
    assert inside.status_code == 400, inside.text
    fine = await _submit(
        runtime, http, project, agent.node_id, [{"ref": "ds@1", "mount": "data/in"}]
    )
    assert fine.status_code == 201, fine.text
    job = await wait_job(http, str(fine.json()["job_id"]))
    assert job["state"] == "succeeded", job


async def test_rm_is_refused_while_a_job_uses_the_version(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    await _push(runtime, http, "big", _folder(tmp_path / "ds", {"a.txt": b"a"}))
    flag = tmp_path / "go"
    project = _folder(
        tmp_path / "p",
        {
            "main.py": (
                "import pathlib, time\n"
                f"flag = pathlib.Path({str(flag)!r})\n"
                "end = time.monotonic() + 30\n"
                "while not flag.exists() and time.monotonic() < end:\n"
                "    time.sleep(0.05)\n"
            ).encode()
        },
    )
    posted = await _submit(
        runtime, http, project, agent.node_id, [{"ref": "big@1", "mount": "in"}]
    )
    assert posted.status_code == 201, posted.text
    refused = await http.delete("/v1/data/big/1")
    assert refused.status_code == 409, refused.text
    assert posted.json()["job_id"] in refused.text
    flag.write_text("go")
    job = await wait_job(http, str(posted.json()["job_id"]))
    assert job["state"] == "succeeded", job
    removed = await http.delete("/v1/data/big/1")
    assert removed.status_code == 200, removed.text
    assert (await http.get("/v1/data/big")).status_code == 404
    assert (await http.delete("/v1/data/big/1")).status_code == 404


async def test_pins_reach_the_agents_gc(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    assert agent.gc is not None
    pinned = await _push(
        runtime, http, "keep", _folder(tmp_path / "ds", {"a.txt": b"a"}), pinned=True
    )
    tree_hex = pinned["tree_id"].removeprefix("sha256:")
    deadline = time.monotonic() + 10
    while tree_hex not in agent.gc.index.pins("tree"):
        assert time.monotonic() < deadline, "pins never reached the agent"
        await asyncio.sleep(0.05)
    unpinned = await http.patch("/v1/data/keep/1", json={"pinned": False})
    assert unpinned.status_code == 200 and unpinned.json()["pinned"] is False
    deadline = time.monotonic() + 10
    while agent.gc.index.pins("tree"):
        assert time.monotonic() < deadline, "unpin never reached the agent"
        await asyncio.sleep(0.05)
    listed = (await http.get("/v1/data")).json()
    assert [i["ref"] for i in listed["inputs"]] == ["keep@1"]
    bad = await http.post("/v1/data", json={"name": "Bad Name", "tree_id": "x"})
    assert bad.status_code == 400
