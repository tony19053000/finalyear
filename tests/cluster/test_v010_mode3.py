"""V0.10 G9: Mode 3 success criteria 1, 4 and 5 at harness scale, on REAL
controller + agent processes (subprocess runtime; every chunk is a real
process of the benchmark library's ``split.image_classify`` kernel - the
integer CNN of criterion 1, D287 - in the lane's own ``numpy`` environment).

* **(a) criterion 1** - 300 images over 3 agents: the planner splits, every
  agent runs a lane, and the combined output is **byte-identical** (same job
  output digest) to the same job pinned to one agent ("don't split": one lane,
  one chunk - D269's same code path); the family's whole-output check
  (``verify_output``: every image once, in order, each label recomputed)
  passes; every chunk committed exactly once;
* **(b) criterion 4** - ``kill -9`` of one lane's agent when about half the
  chunks are committed (and it holds a lease): the job still SUCCEEDS with the
  one-agent digest; the dead lane is ``lost`` and its lease ``released``; the
  ledger - one committed chunk record per chunk, a record for every lease ever
  granted - holds;
* **(c) criterion 5** - a slow link (``[controller.planner]
  default_bw_bytes_s`` at its floor, probes off) and a submitter whose cache
  holds the images (the real ``compute run`` seeds it): the planner does
  **not** split - it runs Local - and ``compute explain`` says why (the best
  set's predicted time against the margin threshold, ``fail``);
* **(d)** the owner guard (``pause_benchmarks``) turned on on one node while
  its lane of a benchmark split job runs (a campaign's forced ``mode3`` run,
  D287): that lane ends ``owner_preempted`` and takes **no** chunk after it,
  its lease goes back to the queue (not a chunk failure), the job completes on
  the other lanes with a verified output and the run is ``done``.

Waits are bounded and generous (the suite runs under load); no assertion is
on a duration. The job's pinned numpy comes from a local wheelhouse (the
``numpy_wheelhouse`` fixture: cached, or downloaded once - V0.10 TD1), so the
agents install it offline; skipped only with neither a cached wheel nor a
network.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import yaml
from mf_cluster import Fabric
from mf_split_cluster import (
    build_split_job,
    campaign_runs,
    check_exactly_once,
    chunk_counts,
    compute,
    job,
    lanes,
    live_leases,
    output_files,
    split_fabric,
    sql,
    submit,
    upload_folder,
    wait_job,
)

from meshforge.agent.policy import write_overlay
from meshforge.bench.workloads import get_family

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(900)]

FAMILY: Final = "split.image_classify"
PARAMS: Final = {"images": 300}
SEED: Final = 91
ASSUMED_SLOW: Final = {"planner": {"default_compute_s": 600.0}}
"""No history in a fresh fabric: the planner assumes 600 s for the whole job,
so it splits (the decision under test in (a)/(b) is the split's correctness,
not the margin - (c) is) and auto-chunks into about 20 chunks."""


def _built(tmp_path: Path, name: str = "job") -> tuple[Path, dict[str, Any]]:
    built = build_split_job(FAMILY, PARAMS, SEED, tmp_path / name)
    manifest = yaml.safe_load((built.path / "compute.yaml").read_text())
    assert manifest["split"]["over"] == {"glob": "data/img/*.ppm"}, manifest
    return built.path, manifest


def _verified(fabric: Fabric, view: dict[str, Any], dest: Path) -> None:
    """The job's output passes the family's whole-output check."""
    files = output_files(fabric, str(view["output_digest"]), dest)
    verdict = get_family(FAMILY).verify_output(files, PARAMS, SEED)  # type: ignore[attr-defined]
    assert verdict.ok, verdict.problems


def _succeeded(view: dict[str, Any]) -> None:
    assert view["state"] == "succeeded", (
        view["failure_type"],
        view["failure_detail"],
        [(a.get("role"), a["state"], a.get("failure_type")) for a in view["attempts"]],
    )


def _one_node_digest(fabric: Fabric, manifest: dict[str, Any], tree: str) -> str:
    node = fabric.nodes[0].node_id
    view = wait_job(fabric, submit(fabric, manifest, tree, node_id=node), 300)
    _succeeded(view)
    (lane,) = lanes(view)
    assert lane["node_id"] == node
    return str(view["output_digest"])


# ------------------------------------------------------------------- (a)


def test_c1_split_over_three_agents_equals_one_agent_byte_for_byte(
    make_fabric: Callable[..., Fabric],
    tmp_path: Path,
    pg_settings: Any,
    numpy_wheelhouse: Path,
) -> None:
    fabric = split_fabric(
        make_fabric, controller_extra=ASSUMED_SLOW, wheelhouse=numpy_wheelhouse
    )
    folder, manifest = _built(tmp_path)
    tree = upload_folder(fabric, folder)

    split_id = submit(fabric, manifest, tree)
    split = wait_job(fabric, split_id, 420)
    _succeeded(split)
    nodes = {n.node_id for n in fabric.nodes}
    assert {a["node_id"] for a in lanes(split)} == nodes, lanes(split)
    # pull-based: a lane whose env is ready late may commit nothing - the
    # ledger, not an even spread, is the invariant
    check_exactly_once(pg_settings, split_id, PARAMS["images"])
    _verified(fabric, split, tmp_path / "out" / "split")

    single = _one_node_digest(fabric, manifest, tree)
    assert split["output_digest"] == single  # criterion 1: byte-identical


# ------------------------------------------------------------------- (b)


def test_c4_kill9_of_a_lane_agent_at_half_way_gives_the_same_output_once(
    make_fabric: Callable[..., Fabric],
    tmp_path: Path,
    pg_settings: Any,
    numpy_wheelhouse: Path,
) -> None:
    fabric = split_fabric(
        make_fabric, controller_extra=ASSUMED_SLOW, wheelhouse=numpy_wheelhouse
    )
    folder, manifest = _built(tmp_path)
    tree = upload_folder(fabric, folder)
    job_id = submit(fabric, manifest, tree)
    victim = fabric.nodes[2]

    # about half the chunks committed AND the victim holds a lease right now
    deadline = time.monotonic() + 420
    done = total = 0
    while time.monotonic() < deadline:
        done, total = chunk_counts(pg_settings, job_id)
        held = live_leases(pg_settings, job_id, victim.node_id)
        if total and done >= total // 2 and held:
            break
        if job(fabric, job_id)["state"] in ("succeeded", "failed", "cancelled"):
            fabric.fail(f"the job ended before the kill ({done}/{total} chunks)")
        time.sleep(0.05)
    else:
        fabric.fail(f"never half-way with a lease on the victim ({done}/{total})")
    assert total >= 12, f"{total} chunks: too few to kill a lane mid-run"
    victim.kill()

    view = wait_job(fabric, job_id, 420)
    _succeeded(view)
    dead = [a for a in lanes(view) if a["node_id"] == victim.node_id]
    assert [(a["state"], a["failure_type"]) for a in dead] == [("lost", "node_lost")]
    released = sql(
        pg_settings,
        "SELECT l.state FROM work_lease l JOIN attempt a ON a.id = l.holder "
        "WHERE a.job_id = :job AND a.node_id = :node",
        job=job_id,
        node=victim.node_id,
    )
    assert "released" in {r.state for r in released}, released
    records = check_exactly_once(pg_settings, job_id, PARAMS["images"])
    assert any(
        r.outcome == "released" and r.node_id == victim.node_id for r in records
    ), records
    _verified(fabric, view, tmp_path / "out" / "killed")
    # the same job on one surviving agent: the same bytes
    assert view["output_digest"] == _one_node_digest(fabric, manifest, tree)


# ------------------------------------------------------------------- (c)


def test_c5_slow_link_planner_does_not_split_and_explain_says_why(
    make_fabric: Callable[..., Fabric],
    tmp_path: Path,
    pg_settings: Any,
    numpy_wheelhouse: Path,
) -> None:
    fabric = split_fabric(
        make_fabric,
        controller_extra={
            # a slow link: nothing measured, the assumed bandwidth at its floor
            "planner": {"default_bw_bytes_s": 1000.0, "default_compute_s": 10.0},
            "network": {"enabled": False},
        },
        wheelhouse=numpy_wheelhouse,
    )
    folder, _manifest = _built(tmp_path)
    local = fabric.nodes[0]
    cwd = tmp_path / "cli"
    cwd.mkdir()
    ran = compute(fabric, local.data_dir, "run", str(folder), cwd=cwd)
    assert ran.returncode == 0, (ran.stdout[-3000:], ran.stderr[-3000:])
    job_id = ran.stdout.splitlines()[0].strip()
    assert "cache seeded" in ran.stderr, ran.stderr  # the Local node holds the items

    view = wait_job(fabric, job_id, 300)
    _succeeded(view)
    assert [a["node_id"] for a in lanes(view)] == [local.node_id]
    _verified(fabric, view, tmp_path / "out" / "local")

    decisions = fabric.call("GET", f"/v1/jobs/{job_id}/decisions").json()["decisions"]
    first = decisions[0]
    assert first["outcome"] == "dispatched" and first["chosen_node_id"] == local.node_id
    chosen = first["candidates"][0]
    assert (chosen["mode"], chosen["node_id"]) == ("local", local.node_id), chosen
    sets = [c for c in first["candidates"] if c["mode"] == "split"]
    assert sets, first["candidates"]
    best = min(sets, key=lambda c: c["margin_test"]["set_s"])
    test = best["margin_test"]
    assert test["passed"] is False and test["forced"] is None, test
    assert test["set_s"] > test["threshold_s"], test  # splitting predicted slower
    assert test["best_single_node_id"] == local.node_id, test

    explained = compute(fabric, local.data_dir, "explain", job_id, cwd=cwd)
    assert explained.returncode == 0, explained.stderr
    text = " ".join(explained.stdout.split())
    assert "not split: the best set" in text, explained.stdout
    assert f"(best single node {local.node_id})" in text, explained.stdout
    assert "fail:" in text, explained.stdout
    assert "ITEMS_FETCH" in text, explained.stdout  # the slow-link term is shown
    # ... and why it is so large: the bandwidth was assumed at its floor
    remote = next(c for c in first["candidates"] if c["mode"] == "offload")
    fetch = remote["estimate"]["terms"]["items_fetch"]
    assert fetch["source"] == "assumed", fetch
    assert fetch["seconds"] > chosen["estimate"]["total_s"], (fetch, chosen)


# ------------------------------------------------------------------- (d)

GUARD_IMAGES: Final = 6000
"""A benchmark-size point (the owner guard acts on benchmark jobs only, D246):
inside the 5 s - 3 min window; about 12 chunks of 500 images."""
GUARD_RUNS: Final = 3
"""The campaign minimum. A run whose job ends before the owner's pause reached
its lane proves nothing; the next run is tried (bounded)."""


def _campaign_spec(origin: str) -> str:
    return (
        "name: g9-owner-guard\n"
        f"seed: {SEED}\n"
        "families:\n"
        f"  - family: {FAMILY}\n"
        f"    params: {{images: {GUARD_IMAGES}}}\n"
        "strategies: [mode3]\n"
        f"origin: {origin}\n"
        f"repetitions: {GUARD_RUNS}\n"
        "budget: {max_runs: 5, max_hours: 1}\n"
    )


def _pause_mid_run(
    fabric: Fabric, pg_settings: Any, cid: str, seq: int, owner: Any
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run ``seq``'s job: once one chunk is committed and the owner's lane
    holds a lease, pause benchmarks on the owner's node; wait for the job;
    resume. Returns (the run, the ended job view)."""
    deadline = time.monotonic() + 420
    while True:
        run = campaign_runs(fabric, cid)[seq]
        job_id = run.get("job_id")
        if job_id:
            done, _total = chunk_counts(pg_settings, job_id)
            if done >= 1 and live_leases(pg_settings, job_id, owner.node_id):
                break
            view = job(fabric, job_id)
            if view["state"] in ("succeeded", "failed", "cancelled"):
                return run, view  # ended before the pause could reach it
        if time.monotonic() > deadline:
            fabric.fail(f"run {seq}: the owner's lane never held a chunk mid-run")
        time.sleep(0.05)
    overlay = owner.data_dir / "policy.toml"
    write_overlay(overlay, {"pause_benchmarks": True})
    try:
        view = wait_job(fabric, job_id, 420)
    finally:
        write_overlay(overlay, {"pause_benchmarks": False})
    return run, view


def test_owner_guard_on_one_node_mid_run_stops_its_lane_and_the_job_completes(
    make_fabric: Callable[..., Fabric],
    tmp_path: Path,
    pg_settings: Any,
    numpy_wheelhouse: Path,
) -> None:
    fabric = split_fabric(
        make_fabric,
        controller_extra={"bench": {"tick_s": 0.5}},
        agent_extra={"policy": {"reload_interval_s": 0.2}},
        wheelhouse=numpy_wheelhouse,
    )
    origin, owner = fabric.nodes[0], fabric.nodes[2]
    created = fabric.call(
        "POST", "/v1/bench/campaigns", json={"spec": _campaign_spec(origin.node_id)}
    )
    assert created.status_code == 201, created.text
    cid = created.json()["campaign_id"]
    started = fabric.call(
        "POST",
        f"/v1/bench/campaigns/{cid}/start",
        json={"max_concurrent_per_node": 1, "cooldown_s": 0.5},
    )
    assert started.status_code == 200, started.text

    mine: list[dict[str, Any]] = []
    for seq in range(GUARD_RUNS):
        run, view = _pause_mid_run(fabric, pg_settings, cid, seq, owner)
        _succeeded(view)
        mine = [a for a in lanes(view) if a["node_id"] == owner.node_id]
        if [a["failure_type"] for a in mine] == ["owner_preempted"]:
            break
    else:
        fabric.fail(f"no run's lane on {owner.name} was pre-empted mid-run")
    job_id = str(view["job_id"])
    assert [a["state"] for a in mine] == ["failed"], mine  # one lane, never re-placed
    others = {a["node_id"] for a in lanes(view)} - {owner.node_id}
    assert others == {origin.node_id, fabric.nodes[1].node_id}, lanes(view)

    # the pre-empted lane took no chunk after it stopped; its lease went back
    # to the queue (released - never a chunk failure)
    (stopped,) = sql(
        pg_settings,
        "SELECT ended_at FROM attempt WHERE id = :a",
        a=mine[0]["attempt_id"],
    )
    assert stopped.ended_at is not None, mine
    held = sql(
        pg_settings,
        "SELECT state, leased_at FROM work_lease WHERE holder = :a",
        a=mine[0]["attempt_id"],
    )
    assert all(r.leased_at <= stopped.ended_at for r in held), (held, stopped)
    assert "released" in {r.state for r in held}, held
    assert "failed" not in {r.state for r in held}, held
    records = check_exactly_once(pg_settings, job_id, GUARD_IMAGES)
    assert not any(r.outcome == "failed" for r in records), records

    files = output_files(fabric, str(view["output_digest"]), tmp_path / "out")
    family = get_family(FAMILY)
    seed = int(run["seed"])  # the run's own seed (derived from the spec's)
    verdict = family.verify_output(files, {"images": GUARD_IMAGES}, seed)  # type: ignore[attr-defined]
    assert verdict.ok, verdict.problems

    # the campaign counts the run as done with this job (not requeued)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        run = campaign_runs(fabric, cid)[seq]
        if run["state"] in ("done", "failed", "skipped"):
            break
        time.sleep(0.5)
    assert (run["state"], run["job_id"]) == ("done", job_id), run
    cancelled = fabric.call("POST", f"/v1/bench/campaigns/{cid}/cancel")
    assert cancelled.status_code == 200, cancelled.text  # the other runs: not needed
