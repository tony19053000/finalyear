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

Timings are loopback numbers: they bound the software on one machine. The
fabric / REST / SQL helpers are the shared ``mf_split_cluster`` ones (V0.10
TD1); the ledger check is its ``check_exactly_once``.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric
from mf_split_cluster import (
    check_exactly_once,
    chunk_counts,
    fetch_output,
    live_leases,
    split_fabric,
    sql,
    submit,
    upload_folder,
    wait_job,
)

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

ITEMS = 120
CHUNK = 10
LEASE_S: Final = 10.0
"""This G4 group's chunk lease (the shared ``split_fabric`` default is 20 s)."""

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


def _submit(fabric: Fabric, tree: str, *, node_id: str | None = None) -> str:
    manifest = {
        "name": "lanes",
        "entrypoint": ["python", "main.py", "{items_file}", "{out}", "{chunk_index}"],
        "split": {"over": {"glob": "images/*.bin"}, "chunk": {"items": CHUNK}},
        "limits": {"timeout": 600},
        "retry": {"max_attempts": 3},
    }
    return submit(fabric, manifest, tree, node_id=node_id)


def _ledger(pg_settings: Any, job_id: str) -> None:
    """Exactly-once commit (the shared ledger check), in ITEMS // CHUNK chunks."""
    records = check_exactly_once(pg_settings, job_id, ITEMS)
    assert sorted({r.chunk_index for r in records}) == list(range(ITEMS // CHUNK))


def _parts(
    fabric: Fabric, pg_settings: Any, job_id: str
) -> tuple[str, bytes, list[str]]:
    """The parts index digest, the concatenated labels and every chunk's
    ``seen.txt`` line, read back through the REST blob API."""
    (row,) = sql(
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
        dest = fetch_output(
            fabric, part, fabric.workdir / "parts" / job_id / f"{first:05d}"
        )
        labels += (dest / "labels.csv").read_bytes()
        seen.append((dest / "seen.txt").read_text())
    return digest, labels, seen


def test_lanes_split_glob_items_identical_to_one_node_and_survive_kill9(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = split_fabric(make_fabric, lease_s=LEASE_S)
    files = _project(tmp_path / "project", delay_s=0.02)
    tree = upload_folder(fabric, tmp_path / "project")

    # --- 3 lanes, undisturbed
    job_id = _submit(fabric, tree)
    job = wait_job(fabric, job_id, 240)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    _ledger(pg_settings, job_id)
    digest, labels, seen = _parts(fabric, pg_settings, job_id)
    assert labels == _expected(files)
    assert seen == [
        f"chunk {k}: {CHUNK} files visible\n" for k in range(ITEMS // CHUNK)
    ]
    lanes = sql(
        pg_settings,
        "SELECT node_id, state FROM attempt WHERE job_id = :job AND role = 'lane'",
        job=job_id,
    )
    assert len({r.node_id for r in lanes}) == 3, lanes

    # --- the same job on one node: same chunks, byte-identical parts
    one = fabric.nodes[1].node_id
    single = _submit(fabric, tree, node_id=one)
    job = wait_job(fabric, single, 240)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    digest_one, labels_one, _ = _parts(fabric, pg_settings, single)
    assert digest_one == digest
    assert labels_one == labels


def test_kill9_of_a_lane_agent_mid_run_gives_the_same_output_once(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = split_fabric(make_fabric, lease_s=LEASE_S)
    files = _project(tmp_path / "project", delay_s=0.15)
    tree = upload_folder(fabric, tmp_path / "project")
    job_id = _submit(fabric, tree)
    victim = fabric.nodes[2]
    # wait until roughly a third of the chunks are committed, then kill -9
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        done, _total = chunk_counts(pg_settings, job_id)
        held = live_leases(pg_settings, job_id, victim.node_id)
        if done >= 4 and held >= 1:
            break
        time.sleep(0.05)
    else:
        fabric.fail("the victim's lane never held a chunk mid-run")
    victim.kill()
    job = wait_job(fabric, job_id, 300)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    lost = sql(
        pg_settings,
        "SELECT state, failure_type FROM attempt WHERE job_id = :job AND node_id = :node",
        job=job_id,
        node=victim.node_id,
    )
    assert [(r.state, r.failure_type) for r in lost] == [("lost", "node_lost")], lost
    released = sql(
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
    fabric = split_fabric(make_fabric, lease_s=LEASE_S)
    files = _project(tmp_path / "project", delay_s=0.1)
    tree = upload_folder(fabric, tmp_path / "project")
    job_id = _submit(fabric, tree)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        done, _total = chunk_counts(pg_settings, job_id)
        if done >= 3:
            break
        time.sleep(0.05)
    else:
        fabric.fail("no chunk was committed before the restart")
    fabric.restart_controller(graceful=False)
    job = wait_job(fabric, job_id, 300)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    _ledger(pg_settings, job_id)
    _digest, labels, _seen = _parts(fabric, pg_settings, job_id)
    assert labels == _expected(files)
