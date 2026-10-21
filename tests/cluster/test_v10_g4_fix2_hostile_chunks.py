"""V0.10 G4 fix round 2, B1: a hostile chunk process cannot get the agent's
own files out (or delete them) by swapping folders for links. Real agents
(subprocess runtime, the agent's own user - the worst case: the job can reach
everything the agent's user can).

* the chunk replaces ``{out}`` with a link to a private folder -> the agent
  refuses to pack it: the chunk fails ``output_error`` (not retried), the job
  fails, nothing of the folder reaches the controller, nothing in it is
  deleted;
* the same with the whole per-chunk control folder (the parent of ``{out}``);
* the job swaps its workdir's ``.meshforge`` (the lane's log files) for a link
  -> the final log upload never reads through it.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric
from mf_split_cluster import job as job_view
from mf_split_cluster import split_fabric, upload_folder, wait_job

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

HOSTILE = """\
import json, os, shutil, sys
cfg = json.load(open("config.json"))
out = sys.argv[2]
attack, secret = cfg["attack"], cfg["secret"]
if attack == "out_link":
    os.rmdir(out)
    os.symlink(secret, out)
elif attack == "folder_link":
    root = os.path.dirname(out)
    shutil.rmtree(root)
    os.symlink(secret, root)
elif attack == "meshforge_link" and not os.path.islink(".meshforge"):
    shutil.rmtree(".meshforge", ignore_errors=True)
    os.symlink(secret, ".meshforge")
if os.path.isdir(out) and not os.path.islink(out):
    with open(os.path.join(out, "r.txt"), "w") as fh:
        fh.write("x")
"""


def _hostile(fabric: Fabric, tmp_path: Path, attack: str) -> tuple[str, Path, bytes]:
    secret = tmp_path / f"secret-{attack}"
    secret.mkdir()
    data = b"agent-private-" + attack.encode() * 50
    (secret / "key").write_bytes(data)
    (secret / "stdout.log").write_bytes(data + b"-log")
    root = tmp_path / f"hostile-{attack}"
    (root / "images").mkdir(parents=True)
    (root / "main.py").write_text(HOSTILE)
    (root / "config.json").write_text(
        json.dumps({"attack": attack, "secret": str(secret)})
    )
    for i in range(20):
        (root / "images" / f"{i:04d}.bin").write_bytes(f"{attack}-{i}".encode())
    tree = upload_folder(fabric, root)
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": {
                "name": f"hostile-{attack.replace('_', '-')}",
                "entrypoint": ["python", "main.py", "{items_file}", "{out}"],
                "split": {"over": {"glob": "images/*.bin"}, "chunk": {"items": 5}},
                "limits": {"timeout": 120},
            },
            "tree_id": tree,
            "placement": {},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"]), secret, data


def _not_uploaded(fabric: Fabric, data: bytes) -> None:
    for blob in (data, data + b"-log"):
        hex_ = hashlib.sha256(blob).hexdigest()
        got = fabric.call("GET", f"/v1/blobs/{hex_}")
        assert got.status_code == 404, (
            f"an agent-private file reached the controller ({got.status_code})"
        )


@pytest.mark.parametrize("attack", ["out_link", "folder_link"])
def test_a_chunk_swapping_its_control_folder_for_a_link_gets_nothing_out(
    make_fabric: Callable[..., Fabric], tmp_path: Path, attack: str
) -> None:
    fabric = split_fabric(make_fabric, lease_s=10.0)
    job_id, secret, data = _hostile(fabric, tmp_path, attack)
    job = wait_job(fabric, job_id, 180)
    assert job["state"] == "failed", job
    assert job["failure_type"] == "output_error", job
    assert "chunk_output_swapped" in (job["failure_detail"] or ""), job
    time.sleep(2)  # lanes end and clean up after the job
    _not_uploaded(fabric, data)
    assert (secret / "key").read_bytes() == data  # never deleted, never changed
    assert (secret / "stdout.log").read_bytes() == data + b"-log"


def test_a_chunk_swapping_the_workdir_log_folder_gets_no_log_out(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = split_fabric(make_fabric, lease_s=10.0)
    job_id, secret, data = _hostile(fabric, tmp_path, "meshforge_link")
    job = wait_job(fabric, job_id, 180)
    assert job["state"] == "succeeded", job
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:  # lanes end (final logs) after the job
        attempts = job_view(fabric, job_id)["attempts"]
        if all(a["state"] in ("done", "failed", "lost") for a in attempts):
            break
        time.sleep(0.2)
    time.sleep(2)  # a lane's release (workdir removal) follows its ack
    _not_uploaded(fabric, data)
    assert (secret / "key").read_bytes() == data
    assert (secret / "stdout.log").read_bytes() == data + b"-log"
