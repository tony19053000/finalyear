"""V0.10 G4 tester r1: lanes end-to-end failure injection on REAL agents.

hostile list items (data only), cancel during run, output over
max_chunk_output_bytes, a chunk failing on one node (retried elsewhere), a
network partition of one lane (lease expiry -> late result), exactly once.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric
from test_v10_g4_lanes_cluster import (  # type: ignore[import-not-found]
    EXEC,
    TIMING,
    _job,
    _sql,
    _upload,
    _wait,
)

from meshforge.common.bundle import unpack

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

MAIN = """\
import json, os, sys, time
items = json.load(open(sys.argv[1], encoding="utf-8"))
out, idx = sys.argv[2], sys.argv[3]
cfg = json.load(open("config.json"))
time.sleep(cfg.get("delay_s", 0))
marker = cfg.get("fail_once_marker")
if marker and idx == str(cfg.get("fail_chunk")) and not os.path.exists(marker):
    open(marker, "w").close()
    sys.exit(3)
if cfg.get("fail_always") is not None and idx == str(cfg["fail_always"]):
    sys.exit(4)
with open(os.path.join(out, "items.json"), "w", encoding="utf-8") as fh:
    json.dump(items, fh, ensure_ascii=False)
if cfg.get("big"):
    open(os.path.join(out, "big.bin"), "wb").write(os.urandom(cfg["big"]))
"""


def _fab(make_fabric: Callable[..., Fabric], visibility: float = 10.0) -> Fabric:
    return make_fabric(
        agents=3,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        controller_extra={
            "split": {"visibility_timeout_s": visibility, "wait_retry_s": 0.3}
        },
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )


def _tree(root: Path, **cfg: Any) -> None:
    root.mkdir(parents=True)
    (root / "main.py").write_text(MAIN)
    (root / "config.json").write_text(json.dumps(cfg))


def _submit(
    fabric: Fabric, tree: str, over: dict[str, Any], *, chunk: int = 1, **extra: Any
) -> str:
    split = {"over": over, "chunk": {"items": chunk}, **extra.pop("split", {})}
    manifest = {
        "name": "t",
        "entrypoint": ["python", "main.py", "{items_file}", "{out}", "{chunk_index}"],
        "split": split,
        "limits": {"timeout": 300},
        "retry": {"max_attempts": 3},
        **extra,
    }
    r = fabric.call(
        "POST",
        "/v1/jobs",
        json={"manifest": manifest, "tree_id": tree, "placement": {}},
    )
    assert r.status_code == 201, r.text
    return str(r.json()["job_id"])


def _read_items(fabric: Fabric, pg: Any, job_id: str) -> list[Any]:
    (row,) = _sql(pg, "SELECT split_parts_digest FROM job WHERE id=:j", j=job_id)
    idx = json.loads(
        fabric.call(
            "GET", f"/v1/blobs/{str(row.split_parts_digest).removeprefix('sha256:')}"
        ).content
    )
    got: list[Any] = []
    for first, _count, part in idx["chunks"]:
        data = fabric.call("GET", f"/v1/blobs/{part.removeprefix('sha256:')}").content
        base = fabric.workdir / "parts" / job_id
        base.mkdir(parents=True, exist_ok=True)
        archive = base / f"{first:05d}.bundle"
        archive.write_bytes(data)
        dest = base / f"{first:05d}"
        unpack(archive, dest)
        got += json.loads((dest / "items.json").read_text(encoding="utf-8"))
    return got


def _ledger(pg: Any, job_id: str, chunks: int) -> None:
    rows = _sql(
        pg,
        "SELECT i.idx, i.state, (SELECT count(*) FROM work_lease l WHERE "
        "l.item_id=i.id AND l.state='completed') AS c FROM work_item i JOIN "
        "work_queue q ON q.id=i.queue_id WHERE q.owner_id=:j ORDER BY i.idx",
        j=job_id,
    )
    assert [r.idx for r in rows] == list(range(chunks))
    assert all(r.state == "done" and r.c == 1 for r in rows), rows


HOSTILE = [
    "a\nb",
    "../../etc/passwd",
    "$(touch pwned)",
    "`touch pwned`",
    "x; touch pwned",
    "a && touch pwned",
    "--help",
    "ünï😀 ключ 文字",
    'it\'s "quoted" {out} {items_file}',
    "C:\\Windows\\System32 | calc",
    "   spaces   ",
]


def test_hostile_list_items_reach_the_entrypoint_only_as_data(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = _fab(make_fabric)
    _tree(tmp_path / "p")
    tree = _upload(fabric, tmp_path / "p")
    job_id = _submit(fabric, tree, {"list": HOSTILE})
    job = _wait(fabric, job_id, 240)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    _ledger(pg_settings, job_id, len(HOSTILE))
    assert _read_items(fabric, pg_settings, job_id) == HOSTILE
    assert not list(fabric.workdir.rglob("pwned")), "a hostile item was executed"
    assert not (Path.cwd() / "pwned").exists()


def test_cancel_during_run_stops_every_lane_and_leaves_no_output(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = _fab(make_fabric)
    _tree(tmp_path / "p", delay_s=1.0)
    tree = _upload(fabric, tmp_path / "p")
    job_id = _submit(fabric, tree, {"range": {"stop": 60}}, chunk=2)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        n = _sql(
            pg_settings,
            "SELECT count(*) AS n FROM work_item i JOIN work_queue q ON "
            "q.id=i.queue_id WHERE q.owner_id=:j AND i.state='done'",
            j=job_id,
        )[0].n
        if n >= 3:
            break
        time.sleep(0.1)
    else:
        fabric.fail("no chunk committed before the cancel")
    assert fabric.call("POST", f"/v1/jobs/{job_id}/cancel").status_code == 202
    job = _wait(fabric, job_id, 120)
    assert job["state"] == "cancelled", job
    (row,) = _sql(
        pg_settings, "SELECT split_parts_digest FROM job WHERE id=:j", j=job_id
    )
    assert row.split_parts_digest is None
    time.sleep(3)
    live = _sql(
        pg_settings,
        "SELECT state FROM attempt WHERE job_id=:j AND state NOT IN "
        "('done','failed','lost','fenced')",
        j=job_id,
    )
    assert live == [], live
    done_a = _sql(
        pg_settings,
        "SELECT count(*) AS n FROM work_item i JOIN work_queue q ON q.id=i.queue_id "
        "WHERE q.owner_id=:j AND i.state='done'",
        j=job_id,
    )[0].n
    time.sleep(4)  # nothing keeps committing after CANCELLED
    done_b = _sql(
        pg_settings,
        "SELECT count(*) AS n FROM work_item i JOIN work_queue q ON q.id=i.queue_id "
        "WHERE q.owner_id=:j AND i.state='done'",
        j=job_id,
    )[0].n
    assert done_a == done_b < 30
    assert _job(fabric, job_id)["state"] == "cancelled"


def test_chunk_output_over_the_cap_fails_the_job_without_retrying(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = _fab(make_fabric)
    _tree(tmp_path / "p", big=200_000)
    tree = _upload(fabric, tmp_path / "p")
    job_id = _submit(
        fabric,
        tree,
        {"range": {"stop": 6}},
        chunk=2,
        split={"max_chunk_output_bytes": 4096},
    )
    job = _wait(fabric, job_id, 240)
    assert job["state"] == "failed", job
    assert job["failure_type"] == "output_error", job
    (row,) = _sql(
        pg_settings, "SELECT split_parts_digest FROM job WHERE id=:j", j=job_id
    )
    assert row.split_parts_digest is None
    leases = _sql(
        pg_settings,
        "SELECT l.state FROM work_lease l JOIN work_item i ON i.id=l.item_id "
        "JOIN work_queue q ON q.id=i.queue_id WHERE q.owner_id=:j AND l.state='failed'",
        j=job_id,
    )
    assert len(leases) <= 3, "an over-cap chunk must not be retried per chunk"


def test_chunk_failing_once_on_one_node_is_retried_and_the_job_succeeds(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = _fab(make_fabric)
    marker = tmp_path / "failed-once.marker"
    _tree(tmp_path / "p", fail_once_marker=str(marker), fail_chunk=2)
    tree = _upload(fabric, tmp_path / "p")
    job_id = _submit(fabric, tree, {"range": {"stop": 12}}, chunk=2)
    job = _wait(fabric, job_id, 240)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    assert marker.exists()
    _ledger(pg_settings, job_id, 6)
    assert _read_items(fabric, pg_settings, job_id) == list(range(12))


def test_chunk_failing_on_every_node_fails_the_job_naming_the_chunk(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = _fab(make_fabric)
    _tree(tmp_path / "p", fail_always=1)
    tree = _upload(fabric, tmp_path / "p")
    job_id = _submit(fabric, tree, {"range": {"stop": 8}}, chunk=2)
    job = _wait(fabric, job_id, 240)
    assert job["state"] == "failed", job
    assert "chunk 1" in (job["failure_detail"] or ""), job
    (row,) = _sql(
        pg_settings, "SELECT split_parts_digest FROM job WHERE id=:j", j=job_id
    )
    assert row.split_parts_digest is None


def test_partitioned_lane_expires_then_heals_exactly_once_same_output(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = _fab(make_fabric, visibility=5.0)
    _tree(tmp_path / "p", delay_s=0.4)
    tree = _upload(fabric, tmp_path / "p")
    job_id = _submit(fabric, tree, {"range": {"stop": 60}}, chunk=2)
    victim = fabric.nodes[1]
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        held = _sql(
            pg_settings,
            "SELECT count(*) AS n FROM work_lease l JOIN attempt a ON a.id=l.holder "
            "WHERE a.job_id=:j AND a.node_id=:n AND l.state='live'",
            j=job_id,
            n=victim.node_id,
        )[0].n
        if held:
            break
        time.sleep(0.05)
    else:
        fabric.fail("victim never held a chunk")
    victim.link.blackhole()
    time.sleep(10)  # > visibility timeout + liveness
    victim.link.blackhole(False)
    job = _wait(fabric, job_id, 300)
    assert job["state"] == "succeeded", (job["failure_type"], job["failure_detail"])
    _ledger(pg_settings, job_id, 30)
    assert _read_items(fabric, pg_settings, job_id) == list(range(60))
