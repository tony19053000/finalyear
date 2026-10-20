"""V0.10 G4 (0.10.4 / 0.10.6) on the cluster harness: a split job over REAL
controller + agent processes (subprocess runtime - no Docker needed; every
chunk is a real Python process in the lane's workdir).

* 3 agents, ``glob`` over 120 files, chunks of 10 -> SUCCEEDED; each chunk's
  process sees ONLY its own 10 item files (D264: the lanes staged the project
  without them; every chunk fetched and placed its own); the combined output
  (parts in chunk order, unpacked and concatenated) is exactly what the
  workload computes locally; exactly one committed result per chunk;
* the same job pinned to ONE node (one lane, "don't split" path, same chunk
  size) gives a byte-identical parts index (criterion 1 at harness scale);
* ``kill -9`` of one agent while its lane runs -> the lane is LOST once the node
  is OFFLINE, its chunks go back to the queue, the other lanes finish: the
  SAME parts index, every chunk committed exactly once (0.10.6 / criterion 4).

Timings are loopback numbers: they bound the software on one machine.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing
from pydantic import SecretStr

from meshforge.cli.client import ControllerClient
from meshforge.cli.transfer import upload_tree
from meshforge.common.bundle import unpack
from meshforge.common.tree import build_tree

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

TIMING = Timing(
    name="lanes",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.2,
    lease_multiplier=4,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=2.0,
)
EXEC = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}
ITEMS = 120
CHUNK = 10
TERMINAL = {"succeeded", "failed", "cancelled"}

# The workload: one CSV line per item file (its path and a digest of its
# bytes), plus how many item files this chunk's process could see - which
# must be exactly its own (D264).
MAIN = """\
import hashlib, json, os, sys, time
items = json.load(open(sys.argv[1], encoding="utf-8"))
out = sys.argv[2]
delay = json.load(open("config.json"))["delay_s"]
visible = len(os.listdir("images"))
with open(os.path.join(out, "labels.csv"), "w", encoding="utf-8") as fh:
    for path in items:
        data = open(path, "rb").read()
        fh.write(path + "," + hashlib.sha256(data).hexdigest()[:16] + "\\n")
        time.sleep(delay)
with open(os.path.join(out, "seen.txt"), "w", encoding="utf-8") as fh:
    fh.write(f"chunk {sys.argv[3]}: {visible} files visible\\n")
"""


def _fabric(make_fabric: Callable[..., Fabric]) -> Fabric:
    return make_fabric(
        agents=3,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        controller_extra={"split": {"visibility_timeout_s": 10.0, "wait_retry_s": 0.3}},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )


def _project(root: Path, delay_s: float) -> dict[str, bytes]:
    (root / "images").mkdir(parents=True)
    (root / "main.py").write_text(MAIN)
    (root / "config.json").write_text(json.dumps({"delay_s": delay_s}))
    files = {}
    for i in range(ITEMS):
        data = hashlib.sha256(f"image-{i}".encode()).digest() * (1 + i % 7)
        name = f"images/{i:04d}.bin"
        (root / name).write_bytes(data)
        files[name] = data
    return files


def _expected(files: dict[str, bytes]) -> bytes:
    lines = [
        f"{name},{hashlib.sha256(data).hexdigest()[:16]}\n"
        for name, data in sorted(files.items(), key=lambda kv: kv[0].encode())
    ]
    return "".join(lines).encode()


def _upload(fabric: Fabric, root: Path) -> str:
    built = build_tree(root, chunk_size=256 * 1024)
    client = ControllerClient(
        fabric.http_base, timeout_s=30, token=SecretStr(fabric.api_token())
    )
    try:
        upload_tree(client, built)
    finally:
        client.close()
    return str(built.tree.tree_id)


def _submit(fabric: Fabric, tree: str, *, node_id: str | None = None) -> str:
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": {
                "name": "lanes",
                "entrypoint": [
                    "python",
                    "main.py",
                    "{items_file}",
                    "{out}",
                    "{chunk_index}",
                ],
                "split": {"over": {"glob": "images/*.bin"}, "chunk": {"items": CHUNK}},
                "limits": {"timeout": 600},
                "retry": {"max_attempts": 3},
            },
            "tree_id": tree,
            "placement": {"node_id": node_id} if node_id else {},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"])


def _job(fabric: Fabric, job_id: str) -> dict[str, Any]:
    resp = fabric.call("GET", f"/v1/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _wait(fabric: Fabric, job_id: str, within: float) -> dict[str, Any]:
    deadline = time.monotonic() + within
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        job = _job(fabric, job_id)
        if job["state"] in TERMINAL:
            return job
        time.sleep(0.1)
    fabric.fail(f"job {job_id} did not end within {within} s: {job}")


async def _rows(pg_settings: Any, query: str, **params: Any) -> list[Any]:
    from sqlalchemy import text

    from meshforge.controller.store.engine import create_engine

    engine = create_engine(pg_settings)
    try:
        async with engine.connect() as conn:
            return list((await conn.execute(text(query), params)).all())
    finally:
        await engine.dispose()


def _sql(pg_settings: Any, query: str, **params: Any) -> list[Any]:
    return asyncio.run(_rows(pg_settings, query, **params))


def _ledger(pg_settings: Any, job_id: str) -> None:
    """Exactly-once commit: every chunk done with exactly one completed lease."""
    rows = _sql(
        pg_settings,
        "SELECT i.idx, i.state, "
        " (SELECT count(*) FROM work_lease l WHERE l.item_id = i.id "
        "  AND l.state = 'completed') AS completed "
        "FROM work_item i JOIN work_queue q ON q.id = i.queue_id "
        "WHERE q.owner_id = :job ORDER BY i.idx",
        job=job_id,
    )
    assert [r.idx for r in rows] == list(range(ITEMS // CHUNK))
    assert all(r.state == "done" and r.completed == 1 for r in rows), rows


def _parts(
    fabric: Fabric, pg_settings: Any, job_id: str
) -> tuple[str, bytes, list[str]]:
    """The parts index digest, the concatenated labels and every chunk's
    ``seen.txt`` line, read back through the REST blob API."""
    (row,) = _sql(
        pg_settings, "SELECT split_parts_digest FROM job WHERE id = :job", job=job_id
    )
    digest = str(row.split_parts_digest)
    index = json.loads(
        fabric.call("GET", f"/v1/blobs/{digest.removeprefix('sha256:')}").content
    )
    labels = b""
    seen: list[str] = []
    for first, count, part in index["chunks"]:
        assert count == CHUNK and first % CHUNK == 0
        data = fabric.call("GET", f"/v1/blobs/{part.removeprefix('sha256:')}").content
        assert hashlib.sha256(data).hexdigest() == part.removeprefix("sha256:")
        dest = fabric.workdir / "parts" / job_id / f"{first:05d}"
        archive = dest.with_suffix(".bundle")
        dest.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(data)
        unpack(archive, dest)
        labels += (dest / "labels.csv").read_bytes()
        seen.append((dest / "seen.txt").read_text())
    return digest, labels, seen


def test_lanes_split_glob_items_identical_to_one_node_and_survive_kill9(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = _fabric(make_fabric)
    files = _project(tmp_path / "project", delay_s=0.02)
    tree = _upload(fabric, tmp_path / "project")

    # --- 3 lanes, undisturbed
    job_id = _submit(fabric, tree)
    job = _wait(fabric, job_id, 240)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    _ledger(pg_settings, job_id)
    digest, labels, seen = _parts(fabric, pg_settings, job_id)
    assert labels == _expected(files)
    assert seen == [
        f"chunk {k}: {CHUNK} files visible\n" for k in range(ITEMS // CHUNK)
    ]
    lanes = _sql(
        pg_settings,
        "SELECT node_id, state FROM attempt WHERE job_id = :job AND role = 'lane'",
        job=job_id,
    )
    assert len({r.node_id for r in lanes}) == 3, lanes

    # --- the same job on one node: same chunks, byte-identical parts
    one = fabric.nodes[1].node_id
    single = _submit(fabric, tree, node_id=one)
    job = _wait(fabric, single, 240)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    digest_one, labels_one, _ = _parts(fabric, pg_settings, single)
    assert digest_one == digest
    assert labels_one == labels


def test_kill9_of_a_lane_agent_mid_run_gives_the_same_output_once(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = _fabric(make_fabric)
    files = _project(tmp_path / "project", delay_s=0.15)
    tree = _upload(fabric, tmp_path / "project")
    job_id = _submit(fabric, tree)
    victim = fabric.nodes[2]
    # wait until roughly a third of the chunks are committed, then kill -9
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        done = _sql(
            pg_settings,
            "SELECT count(*) AS n FROM work_item i JOIN work_queue q "
            "ON q.id = i.queue_id WHERE q.owner_id = :job AND i.state = 'done'",
            job=job_id,
        )[0].n
        held = _sql(
            pg_settings,
            "SELECT count(*) AS n FROM work_lease l JOIN attempt a ON a.id = l.holder "
            "WHERE a.job_id = :job AND a.node_id = :node AND l.state = 'live'",
            job=job_id,
            node=victim.node_id,
        )[0].n
        if done >= 4 and held >= 1:
            break
        time.sleep(0.05)
    else:
        fabric.fail("the victim's lane never held a chunk mid-run")
    victim.kill()
    job = _wait(fabric, job_id, 300)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    lost = _sql(
        pg_settings,
        "SELECT state, failure_type FROM attempt WHERE job_id = :job AND node_id = :node",
        job=job_id,
        node=victim.node_id,
    )
    assert [(r.state, r.failure_type) for r in lost] == [("lost", "node_lost")], lost
    released = _sql(
        pg_settings,
        "SELECT l.state FROM work_lease l JOIN attempt a ON a.id = l.holder "
        "WHERE a.job_id = :job AND a.node_id = :node",
        job=job_id,
        node=victim.node_id,
    )
    assert "released" in {r.state for r in released}, released
    _ledger(pg_settings, job_id)
    _digest, labels, _seen = _parts(fabric, pg_settings, job_id)
    assert labels == _expected(files)
    assert io.BytesIO(labels).getvalue().count(b"\n") == ITEMS


def test_controller_restart_mid_run_resumes_from_postgres(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    """The controller is killed and restarted while the lanes run: the queue,
    leases and committed chunks are in PostgreSQL; the agents reconnect, their
    buffered results are replayed (a result whose upload the new process cannot
    verify is run again - never committed unverified), their lanes ask again,
    and the job ends with the same output, every chunk committed once."""
    fabric = _fabric(make_fabric)
    files = _project(tmp_path / "project", delay_s=0.1)
    tree = _upload(fabric, tmp_path / "project")
    job_id = _submit(fabric, tree)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        done = _sql(
            pg_settings,
            "SELECT count(*) AS n FROM work_item i JOIN work_queue q "
            "ON q.id = i.queue_id WHERE q.owner_id = :job AND i.state = 'done'",
            job=job_id,
        )[0].n
        if done >= 3:
            break
        time.sleep(0.05)
    else:
        fabric.fail("no chunk was committed before the restart")
    fabric.restart_controller(graceful=False)
    job = _wait(fabric, job_id, 300)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    _ledger(pg_settings, job_id)
    _digest, labels, _seen = _parts(fabric, pg_settings, job_id)
    assert labels == _expected(files)
