"""V0.10 G4 tester r3: a chunk leaves a session-detached background process
(``setsid``, escapes the process-group kill) that keeps creating, deleting and
link-swapping inside ``{out}`` while the agent packs. Expected: never a leak of
the private folder; the lane and the agent survive (the chunk fails cleanly or
the job succeeds). Also: an honest WHOLE job still uploads its logs.
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
from mf_split_cluster import split_fabric, sql, upload_folder, wait_job

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

MAIN = """\
import json, os, subprocess, sys
cfg = json.load(open("config.json"))
out = sys.argv[2]
code = '''
import os, sys, time
out, secret = sys.argv[1], sys.argv[2]
end = time.time() + 40
i = 0
while time.time() < end:
    i += 1
    try:
        d = os.path.join(out, "sub")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "f%d" % (i % 30)), "w").write("x" * 4000)
        os.rename(d, d + ".h")
        os.symlink(secret, d)
        os.unlink(d)
        os.rename(d + ".h", d)
        os.symlink(os.path.join(secret, "key"), os.path.join(out, "l%d" % (i % 5)))
        os.unlink(os.path.join(out, "l%d" % (i % 5)))
    except OSError:
        pass
'''
subprocess.Popen([sys.executable, "-c", code, out, cfg["secret"]],
                 start_new_session=True, stdin=subprocess.DEVNULL,
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(os.path.join(out, "ok.txt"), "w").write("x")
"""


def test_background_churn_in_out_never_leaks_and_the_lanes_survive(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: object
) -> None:
    fabric = split_fabric(make_fabric, lease_s=10.0)
    secret = tmp_path / "secret"
    secret.mkdir()
    data = b"agent-private-churn" * 40
    (secret / "key").write_bytes(data)
    root = tmp_path / "proj"
    (root / "images").mkdir(parents=True)
    (root / "main.py").write_text(MAIN)
    (root / "config.json").write_text(json.dumps({"secret": str(secret)}))
    for i in range(30):
        (root / "images" / f"{i:04d}.bin").write_bytes(f"x{i}".encode())
    tree = upload_folder(fabric, root)
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": {
                "name": "churn",
                "entrypoint": ["python", "main.py", "{items_file}", "{out}"],
                "split": {"over": {"glob": "images/*.bin"}, "chunk": {"items": 5}},
                "limits": {"timeout": 120},
                "retry": {"max_attempts": 3},
            },
            "tree_id": tree,
            "placement": {},
        },
    )
    assert resp.status_code == 201, resp.text
    job_id = str(resp.json()["job_id"])
    job = wait_job(fabric, job_id, 240)
    time.sleep(2)
    # whatever the verdict, the private file never reached the controller
    for blob in (data,):
        got = fabric.call("GET", f"/v1/blobs/{hashlib.sha256(blob).hexdigest()}")
        assert got.status_code == 404, "LEAK of an agent-private file"
    assert (secret / "key").read_bytes() == data
    # the agents are alive (an unhandled error in a lane must not kill them)
    lanes = sql(
        pg_settings,
        "SELECT state, failure_type, detail FROM attempt WHERE job_id=:j "
        "AND role='lane'",
        j=job_id,
    )
    internal = [r for r in lanes if r.failure_type == "internal_error"]
    assert not internal, f"a lane died of an internal error: {internal}"
    assert job["state"] in ("succeeded", "failed"), job
    # D4 (minor, observed): with the churn daemon still writing into the old
    # chunk's out/, shutil.rmtree(ignore_errors) leaves the folder behind and
    # the lane's NEXT chunk fails "transfer_error: [Errno 17] File exists
    # ...<attempt>.chunk" (create() does not retry/clean again). Allowed here
    # only because it is the hostile job hurting itself; see report.
    # Lead fix (D4): remove() now moves a still-busy folder aside, so the next
    # chunk always gets a fresh folder - no "File exists" staging failure.
    exists = [r for r in lanes if r.detail and "File exists" in r.detail]
    assert not exists, f"D4 regression: {exists}"
