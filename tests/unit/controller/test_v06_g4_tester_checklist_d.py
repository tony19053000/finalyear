"""V0.6 G4 tester round 1: does part D of ``lab/checklists/V0.6-scheduling.md``
produce the criterion-4 result it promises?

D1 tells the human to run the tiny 300 MB project once pinned on L2 **and once
pinned on the strongest other laptop**, then D2 expects the automatic run to be
``local`` with the other laptops' transfer "~300 MB / their link". But a pinned
run stages the project on the strongest laptop: its agent then reports the
complete tree to the cache index (ARCHITECTURE §14, D100), so the planner sees
that laptop holding the tree, transfer 0 s, and a faster CPU. Pure planner
model of exactly that fabric."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.types import ControllerLinkStat

MB = 1_000_000
NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
TREE = "sha256:" + "a" * 64
EXEC = frozenset({"exec", "exec-subprocess"})


def node(node_id: str, score: float, cached: bool) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=EXEC,
        cpu_slots=5,
        cpu_score=score,
        cached=frozenset({TREE}) if cached else frozenset(),
    )


def request(history: History) -> PlanRequest:
    return PlanRequest(
        job_id="j",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="k",
        trees=(TreeNeed(TREE, 300 * MB),),
        submitter_node_id="L2",
        seeded=True,
        history=history,
    )


def chosen(strong_cached: bool, history: History) -> Any:
    snap = ClusterSnapshot(
        nodes=(node("L2", 100.0, True), node("STRONG", 400.0, strong_cached)),
        controller_links={
            "STRONG": ControllerLinkStat(5 * MB, 4),
            "L2": ControllerLinkStat(5 * MB, 4),
        },
    )
    return plan(request(history), snap, "cost_model", NOW).chosen


def test_when_only_l2_ran_it_the_tiny_job_stays_local() -> None:
    """The procedure that works: history on L2 only; the strong laptop does not
    hold the tree, so it would first pull 300 MB at 5 MB/s."""
    picked = chosen(False, History(by_node_s={"L2": 1.0}, samples_by_node={"L2": 1}))
    assert picked is not None and picked.node_id == "L2" and picked.mode == "local"


def test_d1_as_written_pins_a_run_on_the_strong_laptop_so_local_is_not_chosen() -> None:
    """D1 as written: the strong laptop ran it too, so it holds the tree."""
    picked = chosen(
        True,
        History(
            by_node_s={"L2": 1.0, "STRONG": 0.4}, samples_by_node={"L2": 1, "STRONG": 1}
        ),
    )
    assert picked is not None and picked.node_id == "L2", (
        "the checklist's D2 expectation (Local, the others pay ~300 MB) cannot "
        f"hold after D1; the planner picked {picked and picked.node_id}"
    )


test_d1_as_written_pins_a_run_on_the_strong_laptop_so_local_is_not_chosen = (
    pytest.mark.xfail(
        strict=True,
        reason="T-14: lab checklist D1 warms the strong laptop's cache with the "
        "300 MB tree, so D2's expected result (Local; others transfer ~300 MB) "
        "cannot occur",
    )(test_d1_as_written_pins_a_run_on_the_strong_laptop_so_local_is_not_chosen)
)
