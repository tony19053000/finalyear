"""V0.10 G5 (0.10.7; D268, D269, D283) on the cluster harness: REAL controller
+ agent processes (subprocess runtime), the REAL combine - parts fetched with
the combine's own token, unpacked read-only in index order, the built-in run as
the agent interpreter's own process, or the user's script in the job runtime.

* ``merge-csv`` over 3 lanes and over ONE pinned lane (the "don't split" path)
  -> the same job output digest (criterion 1 at harness scale), and that output
  is the header once + every row in item order;
* a combine script: success (``{parts}``/``{out}``, ``MESHFORGE_COMBINE_*``)
  and failure (``combine_error`` ``script_failed``, never retried);
* the real built-in refusing its parts (``merge-json`` duplicate key ->
  ``combine_error``);
* ``kill -9`` of the node running the combine -> LOST, retried on another node,
  the job still SUCCEEDED with the same output.
"""

from __future__ import annotations

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
    name="combine",
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
ITEMS = 60
CHUNK = 6
TERMINAL = {"succeeded", "failed", "cancelled"}

MAIN = """\
import hashlib, json, os, sys
items = json.load(open(sys.argv[1], encoding="utf-8"))
out = sys.argv[2]
mode = json.load(open("config.json"))["mode"]
if mode == "json-dup":
    with open(os.path.join(out, "r.json"), "w") as fh:
        json.dump({"same": 1}, fh)
    sys.exit(0)
with open(os.path.join(out, "labels.csv"), "w", encoding="utf-8", newline="") as fh:
    fh.write("item,digest\\r\\n")
    for item in items:
        digest = hashlib.sha256(str(item).encode()).hexdigest()[:12]
        fh.write(f"{item},{digest}\\r\\n")
with open(os.path.join(out, "count.txt"), "w") as fh:
    fh.write(f"{len(items)}\\n")
"""

COMBINE_SCRIPT = """\
import os, sys, time
parts, out = sys.argv[1], sys.argv[2]
assert parts == os.environ["MESHFORGE_COMBINE_PARTS"]
assert out == os.environ["MESHFORGE_COMBINE_OUT"]
cfg = __import__("json").load(open("config.json"))
time.sleep(cfg.get("combine_sleep_s", 0))
if cfg.get("combine_fail"):
    print("refusing on purpose", file=sys.stderr)
    sys.exit(3)
names = sorted(os.listdir(parts))
total = 0
with open(os.path.join(out, "summary.txt"), "w") as fh:
    for name in names:
        n = int(open(os.path.join(parts, name, "count.txt")).read())
        total += n
        fh.write(f"{name} {n}\\n")
    fh.write(f"total {total}\\n")
"""


def _fabric(make_fabric: Callable[..., Fabric], agents: int = 3) -> Fabric:
    return make_fabric(
        agents=agents,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        controller_extra={"split": {"visibility_timeout_s": 10.0, "wait_retry_s": 0.3}},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )


def _project(root: Path, **config: Any) -> None:
    root.mkdir(parents=True)
    (root / "main.py").write_text(MAIN)
    (root / "combine.py").write_text(COMBINE_SCRIPT)
    (root / "config.json").write_text(json.dumps({"mode": "csv", **config}))


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


def _submit(
    fabric: Fabric,
    tree: str,
    *,
    combine: Any = "merge-csv",
    node_id: str | None = None,
    submitter: str | None = None,
) -> str:
    body: dict[str, Any] = {
        "manifest": {
            "name": "combine",
            "entrypoint": ["python", "main.py", "{items_file}", "{out}"],
            "split": {
                "over": {"range": {"stop": ITEMS}},
                "chunk": {"items": CHUNK},
                "combine": combine,
            },
            "limits": {"timeout": 600},
            "retry": {"max_attempts": 3, "backoff_s": 0},
        },
        "tree_id": tree,
        "placement": {"node_id": node_id} if node_id else {},
    }
    if submitter is not None:
        body["submitter_node_id"] = submitter
    resp = fabric.call("POST", "/v1/jobs", json=body)
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


def _output(fabric: Fabric, job: dict[str, Any], dest: Path) -> Path:
    digest = str(job["output_digest"])
    data = fabric.call("GET", f"/v1/blobs/{digest.removeprefix('sha256:')}").content
    assert hashlib.sha256(data).hexdigest() == digest.removeprefix("sha256:")
    archive = dest.with_suffix(".bundle")
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(data)
    unpack(archive, dest)
    return dest


def _expected_csv() -> bytes:
    rows = "".join(
        f"{i},{hashlib.sha256(str(i).encode()).hexdigest()[:12]}\r\n"
        for i in range(ITEMS)
    )
    return ("item,digest\r\n" + rows).encode()


def _combines(job: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        (a for a in job["attempts"] if a.get("role") == "combine"),
        key=lambda a: int(a["fencing_token"]),
    )


def test_merge_csv_split_and_unsplit_give_the_same_output(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    _project(tmp_path / "project")
    tree = _upload(fabric, tmp_path / "project")

    split = _wait(fabric, _submit(fabric, tree), 240)
    assert split["state"] == "succeeded", (
        split["failure_type"],
        split["failure_detail"],
    )
    lanes = [a for a in split["attempts"] if a.get("role") == "lane"]
    assert len({a["node_id"] for a in lanes}) == 3
    (combine,) = _combines(split)
    assert combine["state"] == "done"
    assert split["output_digest"] == combine["output_digest"]
    got = _output(fabric, split, tmp_path / "out" / "split")
    assert (got / "labels.csv").read_bytes() == _expected_csv()
    counts = (got / "count.txt").read_text()  # not a CSV: concatenated in order
    assert counts == f"{CHUNK}\n" * (ITEMS // CHUNK)

    one = fabric.nodes[1].node_id
    single = _wait(fabric, _submit(fabric, tree, node_id=one), 240)
    assert single["state"] == "succeeded", single["failure_detail"]
    assert {a["node_id"] for a in single["attempts"]} == {one}
    assert single["output_digest"] == split["output_digest"]  # criterion 1


def test_combine_script_success_and_failure(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric, agents=2)
    script = {"script": ["python", "combine.py", "{parts}", "{out}"]}
    _project(tmp_path / "ok")
    ok = _wait(
        fabric, _submit(fabric, _upload(fabric, tmp_path / "ok"), combine=script), 240
    )
    assert ok["state"] == "succeeded", (ok["failure_type"], ok["failure_detail"])
    summary = (_output(fabric, ok, tmp_path / "out" / "ok") / "summary.txt").read_text()
    chunks = ITEMS // CHUNK
    assert summary == "".join(f"{i:05d} {CHUNK}\n" for i in range(chunks)) + (
        f"total {ITEMS}\n"
    )

    _project(tmp_path / "bad", combine_fail=True)
    bad = _wait(
        fabric, _submit(fabric, _upload(fabric, tmp_path / "bad"), combine=script), 240
    )
    assert bad["state"] == "failed"
    assert bad["failure_type"] == "combine_error"
    assert str(bad["failure_detail"]).startswith("script_failed")
    time.sleep(1.0)
    assert len(_combines(_job(fabric, bad["job_id"]))) == 1  # never retried
    assert bad["output_digest"] is None


def test_the_real_builtin_refuses_duplicate_json_keys(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric, agents=2)
    _project(tmp_path / "p", mode="json-dup")
    job = _wait(
        fabric,
        _submit(fabric, _upload(fabric, tmp_path / "p"), combine="merge-json"),
        240,
    )
    assert job["state"] == "failed"
    assert job["failure_type"] == "combine_error"
    assert "duplicate_key" in str(job["failure_detail"])
    assert len(_combines(job)) == 1


def test_kill9_of_the_combine_node_retries_it_elsewhere(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric, agents=3)
    script = {"script": ["python", "combine.py", "{parts}", "{out}"]}
    _project(tmp_path / "p", combine_sleep_s=6)
    tree = _upload(fabric, tmp_path / "p")
    victim = fabric.nodes[2]
    job_id = _submit(fabric, tree, combine=script, submitter=victim.node_id)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        combines = _combines(_job(fabric, job_id))
        if combines and combines[-1]["state"] == "running":
            break
        time.sleep(0.1)
    else:
        fabric.fail("the combine never ran")
    assert combines[-1]["node_id"] == victim.node_id  # the submitter first
    victim.kill()
    job = _wait(fabric, job_id, 300)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    combines = _combines(job)
    assert [(c["node_id"] == victim.node_id, c["state"]) for c in combines] == [
        (True, "lost"),
        (False, "done"),
    ]
    summary = (_output(fabric, job, tmp_path / "out") / "summary.txt").read_text()
    assert summary.endswith(f"total {ITEMS}\n")
    assert io.StringIO(summary).read().count("\n") == ITEMS // CHUNK + 1
