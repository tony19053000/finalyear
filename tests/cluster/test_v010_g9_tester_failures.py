"""V0.10 G9 tester: extra failure injection on Mode 3 (criterion 4 beyond the
implementer's single kill -9).  Real controller + 3 real agents, the real
``split.image_classify`` job.  Invariant after every fault: the job either
SUCCEEDS with the exactly-once ledger and the correct, verified output (the same
digest as an unsplit run), or FAILS cleanly (failure type set, no output)."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import yaml
from mf_cluster import Fabric, Node
from mf_split_cluster import (
    TERMINAL,
    build_split_job,
    check_exactly_once,
    chunk_counts,
    index_reachable,
    job,
    lanes,
    output_files,
    split_fabric,
    sql,
    submit,
    upload_folder,
    wait_job,
)

from meshforge.bench.workloads import get_family

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(900)]

FAMILY: Final = "split.image_classify"
PARAMS: Final = {"images": 300}
SEED: Final = 77
SLOW: Final = {"planner": {"default_compute_s": 600.0}}


@pytest.fixture(autouse=True)
def _needs_index() -> None:
    if not index_reachable():
        pytest.skip("the job's pinned numpy must be installed from the package index")


def _prepare(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> tuple[Fabric, dict[str, Any], str]:
    fabric = split_fabric(make_fabric, controller_extra=SLOW)
    built = build_split_job(FAMILY, PARAMS, SEED, tmp_path / "job")
    manifest = yaml.safe_load((built.path / "compute.yaml").read_text())
    return fabric, manifest, upload_folder(fabric, built.path)


def _holds_lease(pg: Any, job_id: str, node: Node) -> bool:
    rows = sql(
        pg,
        "SELECT count(*) AS n FROM work_lease l JOIN attempt a ON a.id = l.holder "
        "WHERE a.job_id = :job AND a.node_id = :node AND l.state = 'live'",
        job=job_id,
        node=node.node_id,
    )
    return bool(rows[0].n)


def _wait_progress(
    fabric: Fabric, pg: Any, job_id: str, fraction: float, victims: list[Node]
) -> tuple[int, int]:
    deadline = time.monotonic() + 420
    done = total = 0
    while time.monotonic() < deadline:
        done, total = chunk_counts(pg, job_id)
        if (
            total
            and done >= int(total * fraction)
            and all(_holds_lease(pg, job_id, v) for v in victims)
        ):
            return done, total
        if job(fabric, job_id)["state"] in TERMINAL:
            fabric.fail(f"job ended before the fault ({done}/{total})")
        time.sleep(0.05)
    fabric.fail(f"never reached {fraction} with leases on victims ({done}/{total})")


def _verified(fabric: Fabric, view: dict[str, Any], dest: Path) -> None:
    files = output_files(fabric, str(view["output_digest"]), dest)
    verdict = get_family(FAMILY).verify_output(files, PARAMS, SEED)  # type: ignore[attr-defined]
    assert verdict.ok, verdict.problems


def _unsplit_digest(
    fabric: Fabric, manifest: dict[str, Any], tree: str, node: Node
) -> str:
    view = wait_job(fabric, submit(fabric, manifest, tree, node_id=node.node_id), 300)
    assert view["state"] == "succeeded", view["failure_detail"]
    return str(view["output_digest"])


def test_kill9_two_of_three_agents_survivor_finishes_exactly_once(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric, manifest, tree = _prepare(make_fabric, tmp_path)
    job_id = submit(fabric, manifest, tree)
    victims = [fabric.nodes[1], fabric.nodes[2]]
    _wait_progress(fabric, pg_settings, job_id, 0.25, victims)
    for v in victims:
        v.kill()
    view = wait_job(fabric, job_id, 420)
    assert view["state"] == "succeeded", (view["failure_type"], view["failure_detail"])
    dead = {a["node_id"]: (a["state"], a["failure_type"]) for a in lanes(view)}
    for v in victims:
        assert dead[v.node_id] == ("lost", "node_lost"), dead
    records = check_exactly_once(pg_settings, job_id, PARAMS["images"])
    assert {r.node_id for r in records if r.outcome == "committed"} >= {
        fabric.nodes[0].node_id
    }
    _verified(fabric, view, tmp_path / "out")
    fabric.nodes[1].restart(
        graceful=False
    )  # a killed agent can come back for the control run
    fabric.wait_all_connected(within=60, nodes=[fabric.nodes[0], fabric.nodes[1]])
    assert view["output_digest"] == _unsplit_digest(
        fabric, manifest, tree, fabric.nodes[0]
    )


def test_kill9_during_combine_job_is_correct_or_fails_cleanly(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric, manifest, tree = _prepare(make_fabric, tmp_path)
    job_id = submit(fabric, manifest, tree)
    by_id = {n.node_id: n for n in fabric.nodes}
    killed: str | None = None
    deadline = time.monotonic() + 420
    while time.monotonic() < deadline and killed is None:
        rows = sql(
            pg_settings,
            "SELECT node_id, state FROM attempt WHERE job_id = :job AND role = 'combine'",
            job=job_id,
        )
        if rows:
            killed = rows[0].node_id
            by_id[killed].kill()
            break
        if job(fabric, job_id)["state"] in TERMINAL:
            break
        time.sleep(0.01)
    view = wait_job(fabric, job_id, 420)
    combine = [a for a in view["attempts"] if a.get("role") == "combine"]
    print(  # noqa: T201 - tester evidence: did the kill land on a combine?
        "COMBINE-KILL",
        killed,
        view["state"],
        [(a["node_id"], a["state"], a.get("failure_type")) for a in combine],
    )
    assert killed is not None, "the combine attempt was never observed: test is vacuous"
    if view["state"] == "succeeded":
        check_exactly_once(pg_settings, job_id, PARAMS["images"])
        _verified(fabric, view, tmp_path / "out")
        survivor = next(
            n for n in fabric.nodes if n.node_id != killed and n.process.running
        )
        assert view["output_digest"] == _unsplit_digest(
            fabric, manifest, tree, survivor
        )
    else:
        assert view["state"] == "failed", view["state"]
        assert view["failure_type"], view
        assert not view.get("output_digest"), "a failed job must not publish an output"
    # the queue must not be left with live leases or running chunks
    live = sql(
        pg_settings,
        "SELECT count(*) AS n FROM work_lease l JOIN work_item i ON i.id = l.item_id "
        "JOIN work_queue q ON q.id = i.queue_id WHERE q.owner_id = :job AND l.state = 'live'",
        job=job_id,
    )[0].n
    assert live == 0, f"{live} live lease(s) left on a terminal job (killed={killed})"


def test_agent_restart_mid_run_rejoins_and_job_stays_exactly_once(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric, manifest, tree = _prepare(make_fabric, tmp_path)
    job_id = submit(fabric, manifest, tree)
    victim = fabric.nodes[2]
    _wait_progress(fabric, pg_settings, job_id, 0.3, [victim])
    victim.restart(graceful=False)
    fabric.wait_all_connected(within=90, nodes=[victim])
    view = wait_job(fabric, job_id, 420)
    assert view["state"] == "succeeded", (view["failure_type"], view["failure_detail"])
    check_exactly_once(pg_settings, job_id, PARAMS["images"])
    _verified(fabric, view, tmp_path / "out")
    assert view["output_digest"] == _unsplit_digest(fabric, manifest, tree, victim)
