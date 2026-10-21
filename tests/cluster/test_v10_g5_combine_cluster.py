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

from mf_cluster import Fabric
from mf_split_cluster import (
    combines,
    fetch_output,
    split_fabric,
    submit,
    upload_folder,
    wait_job,
)
from mf_split_cluster import job as job_view

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

ITEMS = 60
CHUNK = 6
LEASE_S = 10.0
"""This G5 group's chunk lease (the shared ``split_fabric`` default is 20 s)."""

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


def _project(root: Path, **config: Any) -> None:
    root.mkdir(parents=True)
    (root / "main.py").write_text(MAIN)
    (root / "combine.py").write_text(COMBINE_SCRIPT)
    (root / "config.json").write_text(json.dumps({"mode": "csv", **config}))


def _submit(
    fabric: Fabric,
    tree: str,
    *,
    combine: Any = "merge-csv",
    node_id: str | None = None,
    submitter: str | None = None,
) -> str:
    manifest = {
        "name": "combine",
        "entrypoint": ["python", "main.py", "{items_file}", "{out}"],
        "split": {
            "over": {"range": {"stop": ITEMS}},
            "chunk": {"items": CHUNK},
            "combine": combine,
        },
        "limits": {"timeout": 600},
        "retry": {"max_attempts": 3, "backoff_s": 0},
    }
    return submit(fabric, manifest, tree, node_id=node_id, submitter=submitter)


def _expected_csv() -> bytes:
    rows = "".join(
        f"{i},{hashlib.sha256(str(i).encode()).hexdigest()[:12]}\r\n"
        for i in range(ITEMS)
    )
    return ("item,digest\r\n" + rows).encode()


def test_merge_csv_split_and_unsplit_give_the_same_output(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = split_fabric(make_fabric, lease_s=LEASE_S)
    _project(tmp_path / "project")
    tree = upload_folder(fabric, tmp_path / "project")

    split = wait_job(fabric, _submit(fabric, tree), 240)
    assert split["state"] == "succeeded", (
        split["failure_type"],
        split["failure_detail"],
    )
    lanes = [a for a in split["attempts"] if a.get("role") == "lane"]
    assert len({a["node_id"] for a in lanes}) == 3
    (combine,) = combines(split)
    assert combine["state"] == "done"
    assert split["output_digest"] == combine["output_digest"]
    got = fetch_output(fabric, split["output_digest"], tmp_path / "out" / "split")
    assert (got / "labels.csv").read_bytes() == _expected_csv()
    counts = (got / "count.txt").read_text()  # not a CSV: concatenated in order
    assert counts == f"{CHUNK}\n" * (ITEMS // CHUNK)

    one = fabric.nodes[1].node_id
    single = wait_job(fabric, _submit(fabric, tree, node_id=one), 240)
    assert single["state"] == "succeeded", single["failure_detail"]
    assert {a["node_id"] for a in single["attempts"]} == {one}
    assert single["output_digest"] == split["output_digest"]  # criterion 1


def test_combine_script_success_and_failure(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = split_fabric(make_fabric, agents=2, lease_s=LEASE_S)
    script = {"script": ["python", "combine.py", "{parts}", "{out}"]}
    _project(tmp_path / "ok")
    ok = wait_job(
        fabric,
        _submit(fabric, upload_folder(fabric, tmp_path / "ok"), combine=script),
        240,
    )
    assert ok["state"] == "succeeded", (ok["failure_type"], ok["failure_detail"])
    summary = (
        fetch_output(fabric, ok["output_digest"], tmp_path / "out" / "ok")
        / "summary.txt"
    ).read_text()
    chunks = ITEMS // CHUNK
    assert summary == "".join(f"{i:05d} {CHUNK}\n" for i in range(chunks)) + (
        f"total {ITEMS}\n"
    )

    _project(tmp_path / "bad", combine_fail=True)
    bad = wait_job(
        fabric,
        _submit(fabric, upload_folder(fabric, tmp_path / "bad"), combine=script),
        240,
    )
    assert bad["state"] == "failed"
    assert bad["failure_type"] == "combine_error"
    assert str(bad["failure_detail"]).startswith("script_failed")
    time.sleep(1.0)
    assert len(combines(job_view(fabric, bad["job_id"]))) == 1  # never retried
    assert bad["output_digest"] is None


def test_the_real_builtin_refuses_duplicate_json_keys(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = split_fabric(make_fabric, agents=2, lease_s=LEASE_S)
    _project(tmp_path / "p", mode="json-dup")
    job = wait_job(
        fabric,
        _submit(fabric, upload_folder(fabric, tmp_path / "p"), combine="merge-json"),
        240,
    )
    assert job["state"] == "failed"
    assert job["failure_type"] == "combine_error"
    assert "duplicate_key" in str(job["failure_detail"])
    assert len(combines(job)) == 1


def test_kill9_of_the_combine_node_retries_it_elsewhere(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = split_fabric(make_fabric, agents=3, lease_s=LEASE_S)
    script = {"script": ["python", "combine.py", "{parts}", "{out}"]}
    _project(tmp_path / "p", combine_sleep_s=6)
    tree = upload_folder(fabric, tmp_path / "p")
    victim = fabric.nodes[2]
    job_id = _submit(fabric, tree, combine=script, submitter=victim.node_id)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        tries = combines(job_view(fabric, job_id))
        if tries and tries[-1]["state"] == "running":
            break
        time.sleep(0.1)
    else:
        fabric.fail("the combine never ran")
    assert tries[-1]["node_id"] == victim.node_id  # the submitter first
    victim.kill()
    job = wait_job(fabric, job_id, 300)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    tries = combines(job)
    assert [(c["node_id"] == victim.node_id, c["state"]) for c in tries] == [
        (True, "lost"),
        (False, "done"),
    ]
    summary = (
        fetch_output(fabric, job["output_digest"], tmp_path / "out") / "summary.txt"
    ).read_text()
    assert summary.endswith(f"total {ITEMS}\n")
    assert io.StringIO(summary).read().count("\n") == ITEMS // CHUNK + 1
