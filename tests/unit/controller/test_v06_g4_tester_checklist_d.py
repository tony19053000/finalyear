"""V0.6 G4 tester round 1 (T-14), converted in fix round 1: does part D of
``lab/checklists/V0.6-scheduling.md`` produce the criterion-4 result it promises?

The original D1 told the human to run the tiny 300 MB project pinned on L2
**and** on the strongest other laptop, then D2 expected ``local``. A pinned run
stages the project on that laptop (its agent reports the complete tree to the
cache index, ARCHITECTURE §14, D100), so the planner sees transfer 0 s there and
a faster CPU - Local cannot win. D132 rewrote part D: history on L2 only, and a
warning that a laptop which ran it holds the project and then - correctly - wins.
Pure planner model of both fabrics: the procedure as written now, and the
warning's case (which must keep choosing the strong laptop: picking L2 there
would be a wrong planner, not a passing lab step)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

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


def test_a_laptop_that_already_ran_it_holds_the_tree_and_correctly_wins() -> None:
    """Part D1's warning (the old D1 procedure): the strong laptop ran it too, so
    it holds the tree - nothing to transfer, a faster CPU. The planner must pick
    it (remote), which is why the checklist builds history on L2 only."""
    picked = chosen(
        True,
        History(
            by_node_s={"L2": 1.0, "STRONG": 0.4}, samples_by_node={"L2": 1, "STRONG": 1}
        ),
    )
    assert picked is not None and picked.node_id == "STRONG", (
        f"a holder with a faster CPU must win; the planner picked {picked and picked.node_id}"
    )
    assert picked.mode == "offload"


def test_the_checklist_builds_history_on_l2_only_and_warns_about_other_laptops() -> (
    None
):
    """The lab text matches the procedure modelled above (D132)."""
    root = Path(__file__).resolve().parents[3]
    text = (root / "lab" / "checklists" / "V0.6-scheduling.md").read_text(
        encoding="utf-8"
    )
    part_d = text[text.index("## D. Criterion 4") : text.index("## E. ")]
    assert "History first, on L2 only" in part_d
    assert "Do not run `c4-tiny` on any other laptop before step 2." in part_d
    assert "mode `local`" in part_d
