"""V0.10 G5 tester r1: failure injection against the REAL combine (real
controller + agent processes), written from D283 pins (4)(6)(7)(8) / brief 5e.

* a chunk that wrote an (in-tree) symlink -> ``combine_error`` ``unsafe_part``,
  one combine attempt, never retried, no job output;
* the combined output over ``max_output_bytes`` -> ``too_large``, not retried;
* the "don't split" path (one chunk) -> byte-identical output to many chunks;
* a combine script: placeholders inside larger argv items, env, cwd;
* cancel while a combine script runs -> CANCELLED, no retry, no live attempt;
* ``compute get`` on a split job returns the combined output.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    compute,
    fetch_output,
    split_fabric,
    submit,
    upload_folder,
    wait_job,
)
from mf_split_cluster import job as job_view

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

ITEMS = 30
CHUNK = 6

MAIN = """\
import hashlib, json, os, sys
items = json.load(open(sys.argv[1], encoding="utf-8"))
out = sys.argv[2]
cfg = json.load(open("config.json"))
with open(os.path.join(out, "labels.csv"), "w", encoding="utf-8", newline="") as fh:
    fh.write("item,digest\\n")
    for item in items:
        fh.write(f"{item},{hashlib.sha256(str(item).encode()).hexdigest()[:12]}\\n")
if cfg.get("symlink"):
    os.symlink("labels.csv", os.path.join(out, "alias.csv"))
if cfg.get("big"):
    with open(os.path.join(out, "blob.bin"), "wb") as fh:
        fh.write(b"x" * cfg["big"])
"""

SCRIPT = """\
import json, os, sys, time
cfg = json.load(open("config.json"))
time.sleep(cfg.get("combine_sleep_s", 0))
out = os.environ["MESHFORGE_COMBINE_OUT"]
parts = os.environ["MESHFORGE_COMBINE_PARTS"]
with open(os.path.join(out, "args.json"), "w") as fh:
    json.dump({"argv": sys.argv[1:], "parts": parts, "out": out,
               "n": len(os.listdir(parts)),
               "writable_parts": os.access(os.path.join(parts, "00000"), os.W_OK)}, fh)
"""


def _fabric(make_fabric: Callable[..., Fabric], **jobs: Any) -> Fabric:
    """Two agents; ``jobs`` keys go to ``[controller.jobs]`` (also used by r2)."""
    return split_fabric(make_fabric, agents=2, lease_s=10.0, controller_jobs=jobs)


def _project(root: Path, **config: Any) -> str:
    root.mkdir(parents=True)
    (root / "main.py").write_text(MAIN)
    (root / "combine.py").write_text(SCRIPT)
    (root / "config.json").write_text(json.dumps(config))
    return str(root)


def _submit(
    fabric: Fabric, tree: str, *, combine: Any = "merge-csv", chunk: int = CHUNK
) -> str:
    manifest = {
        "name": "t",
        "entrypoint": ["python", "main.py", "{items_file}", "{out}"],
        "split": {
            "over": {"range": {"stop": ITEMS}},
            "chunk": {"items": chunk},
            "combine": combine,
        },
        "limits": {"timeout": 600},
        "retry": {"max_attempts": 3, "backoff_s": 0},
    }
    return submit(fabric, manifest, tree)


def _expected_csv() -> bytes:
    rows = "".join(
        f"{i},{hashlib.sha256(str(i).encode()).hexdigest()[:12]}\n"
        for i in range(ITEMS)
    )
    return ("item,digest\n" + rows).encode()


def test_symlink_in_a_chunk_result_never_reaches_the_combine(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    """Layered defence: the chunk's own pack refuses a symlink in ``{out}``
    (``output_error``), so the job fails there and no combine is ever placed.
    (The built-in's own ``unsafe_part`` refusal is covered at unit level.)"""
    fabric = _fabric(make_fabric)
    _project(tmp_path / "p", symlink=True)
    job = wait_job(fabric, _submit(fabric, upload_folder(fabric, tmp_path / "p")), 240)
    assert job["state"] == "failed", job
    assert job["failure_type"] in ("output_error", "combine_error"), job
    if job["failure_type"] == "combine_error":
        assert str(job["failure_detail"]).startswith("unsafe_part")
    else:
        assert combines(job) == []
    assert job["output_digest"] is None


def test_combined_output_over_the_cap_is_too_large_and_not_retried(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    # 5 chunks x 400 KB of chunk output (chunks are capped separately and by
    # default at 256 MiB) -> a 2 MB combine against a 1 MiB attempt cap
    fabric = _fabric(make_fabric, max_output_bytes=1024 * 1024)
    _project(tmp_path / "p", big=400_000)
    job = wait_job(
        fabric,
        _submit(fabric, upload_folder(fabric, tmp_path / "p"), combine="concat"),
        240,
    )
    assert job["state"] == "failed", job
    assert job["failure_type"] == "combine_error", (
        job["failure_type"],
        job["failure_detail"],
    )
    assert "too_large" in str(job["failure_detail"])
    time.sleep(1.5)
    assert len(combines(job_view(fabric, job["job_id"]))) == 1


def test_one_chunk_path_goes_through_the_same_combine_and_matches_golden(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    """Pin 3: "don't split" (one chunk) uses the one combine path. Alone on
    its fabric (T2, fixed in fix r1: see the test below); the golden bytes are the many-chunk
    output other tests assert (merge-csv over 0..ITEMS)."""
    fabric = _fabric(make_fabric)
    _project(tmp_path / "p")
    job = wait_job(
        fabric, _submit(fabric, upload_folder(fabric, tmp_path / "p"), chunk=ITEMS), 240
    )
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    assert len(combines(job)) == 1
    assert len([a for a in job["attempts"] if a.get("role") == "lane"]) == 1
    assert job["output_digest"] == combines(job)[0]["output_digest"]
    got = fetch_output(fabric, job["output_digest"], tmp_path / "o")
    assert (got / "labels.csv").read_bytes() == _expected_csv()


def test_same_tree_same_items_other_chunk_size_runs(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    _project(tmp_path / "p")
    tree = upload_folder(fabric, tmp_path / "p")
    first = wait_job(fabric, _submit(fabric, tree, chunk=CHUNK), 240)
    second = wait_job(fabric, _submit(fabric, tree, chunk=ITEMS), 240)
    assert first["state"] == "succeeded"
    assert second["state"] == "succeeded", second["failure_detail"]


def test_script_combine_placeholders_inside_larger_arguments(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    _project(tmp_path / "p")
    script = {
        "script": [
            "python",
            "combine.py",
            "--parts={parts}",
            "--out={out}",
            "{{parts}}",  # not a placeholder of ours: left untouched
            "x{parts}y{parts}",  # twice in one item: both replaced
        ]
    }
    job = wait_job(
        fabric,
        _submit(fabric, upload_folder(fabric, tmp_path / "p"), combine=script),
        240,
    )
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    doc = json.loads(
        (
            fetch_output(fabric, job["output_digest"], tmp_path / "o") / "args.json"
        ).read_text()
    )
    parts, out = doc["parts"], doc["out"]
    assert os.path.isabs(parts) and os.path.isabs(out) and parts != out
    argv = doc["argv"]
    assert argv[0] == f"--parts={parts}" and argv[1] == f"--out={out}"
    assert argv[2] in ("{{parts}}", "{" + parts + "}")  # report which one it was
    assert argv[3] == f"x{parts}y{parts}"
    assert doc["n"] == ITEMS // CHUNK
    assert doc["writable_parts"] is False  # parts are read-only


def test_cancel_while_a_combine_script_runs(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    _project(tmp_path / "p", combine_sleep_s=120)
    script = {"script": ["python", "combine.py", "{parts}", "{out}"]}
    job_id = _submit(fabric, upload_folder(fabric, tmp_path / "p"), combine=script)
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        tries = combines(job_view(fabric, job_id))
        if tries and tries[-1]["state"] == "running":
            break
        time.sleep(0.1)
    else:
        fabric.fail("the combine never ran")
    time.sleep(1.0)
    assert fabric.call("POST", f"/v1/jobs/{job_id}/cancel").status_code == 202
    job = wait_job(fabric, job_id, 90)
    assert job["state"] == "cancelled", job
    time.sleep(2.0)
    again = job_view(fabric, job_id)
    assert again["state"] == "cancelled"
    assert len(combines(again)) == 1
    assert all(a["state"] not in ("running", "uploading") for a in again["attempts"])
    assert again["output_digest"] is None


def test_compute_get_returns_the_combined_output(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    _project(tmp_path / "p")
    job = wait_job(fabric, _submit(fabric, upload_folder(fabric, tmp_path / "p")), 240)
    assert job["state"] == "succeeded", job["failure_detail"]
    done = compute(
        fabric,
        tmp_path / "data",
        "get",
        job["job_id"],
        "--dest",
        str(tmp_path / "got"),
        cwd=tmp_path,
        timeout_s=240,
    )
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert (tmp_path / "got" / "labels.csv").read_bytes() == _expected_csv()
    # a lane's chunk result is never the job's output
    assert not list((tmp_path / "got").glob("0000*"))
