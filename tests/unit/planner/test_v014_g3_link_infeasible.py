"""V0.14 G3 (D294): ``link_infeasible`` - a candidate whose bytes would come
over a **fresh** measured link below ``min_useful_bw_bytes_s``, for more than
``min_useful_bytes``, is rejected before costing. Stale or unmeasured links,
and the controller's transfer history, never reject; a pinned job or forced
set is never refused for it; the floor 0 turns it off; it is live (the job
waits, never fails ``no_feasible_plan``)."""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from meshforge.controller.planner import plan
from meshforge.controller.planner.feasibility import link_rejection
from meshforge.controller.planner.types import (
    STATIC_CODES,
    ClusterSnapshot,
    ControllerLinkStat,
    LinkStat,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    RejectCode,
    SplitInfo,
    TreeNeed,
)

pytestmark = pytest.mark.unit

TREE = "sha256:" + "a" * 64
NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC)
MIB = 1024 * 1024


def _node(node_id: str, *, holds: bool = False) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=frozenset({"exec", "exec-subprocess", "exec-split"}),
        cpu_slots=2,
        cached=frozenset({TREE}) if holds else frozenset(),
    )


def _request(size: int, **kw: object) -> PlanRequest:
    return PlanRequest(
        job_id="job_" + "0" * 26,
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        trees=(TreeNeed(TREE, size),),
        **kw,  # type: ignore[arg-type]
    )


def _snapshot(
    bw: float,
    *,
    age_s: float = 30.0,
    floor: float = 1e6,
    min_bytes: int = 64 * MIB,
    controller_bw: float | None = None,
) -> ClusterSnapshot:
    links = {
        ("n_far", "n_holder"): LinkStat(
            bw_bytes_s=bw,
            rtt_ms=3.0,
            samples=4,
            measured_at=NOW - dt.timedelta(seconds=age_s),
        )
    }
    ctl = (
        {}
        if controller_bw is None
        else {"n_far": ControllerLinkStat(bw_bytes_s=controller_bw, samples=5)}
    )
    return ClusterSnapshot(
        nodes=(_node("n_far"), _node("n_holder", holds=True)),
        links=links,
        controller_links=ctl,
        config=PlannerConfig(min_useful_bw_bytes_s=floor, min_useful_bytes=min_bytes),
    )


def _far(snapshot: ClusterSnapshot) -> NodeSnapshot:
    node = snapshot.node("n_far")
    assert node is not None
    return node


def test_a_fresh_slow_link_for_many_bytes_rejects_before_costing() -> None:
    snap = _snapshot(200e3)  # 0.2 MB/s
    refused = link_rejection(
        _request(500 * MIB), _far(snap), snap, local=False, now=NOW
    )
    assert refused is not None and refused.code is RejectCode.LINK_INFEASIBLE
    assert "500 MiB" in refused.text and "n_holder" in refused.text
    assert "0.200 MB/s" in refused.text and not refused.static
    assert RejectCode.LINK_INFEASIBLE not in STATIC_CODES
    decision = plan(_request(500 * MIB), snap, "cost_model", NOW)
    far = next(c for c in decision.candidates if c.node_id == "n_far")
    assert not far.feasible and far.estimate is None  # never costed
    assert (
        far.rejection is not None and far.rejection.code is RejectCode.LINK_INFEASIBLE
    )
    assert decision.chosen is not None and decision.chosen.node_id == "n_holder"


def test_the_job_waits_when_only_a_hopeless_link_is_left() -> None:
    holder = dataclasses.replace(_node("n_holder", holds=True), state="draining")
    snap = dataclasses.replace(_snapshot(200e3), nodes=(_node("n_far"), holder))
    decision = plan(_request(500 * MIB), snap, "cost_model", NOW)
    assert decision.chosen is None and not decision.all_static_rejections


@pytest.mark.parametrize(
    ("label", "snap", "size"),
    [
        ("stale", _snapshot(200e3, age_s=7200.0), 500 * MIB),
        ("future-dated", _snapshot(200e3, age_s=-3600.0), 500 * MIB),
        ("fast enough", _snapshot(2e6), 500 * MIB),
        ("at the floor", _snapshot(1e6), 500 * MIB),
        ("few bytes", _snapshot(200e3), 64 * MIB),
        ("off", _snapshot(200e3, floor=0.0), 500 * MIB),
        ("controller faster", _snapshot(200e3, controller_bw=20e6), 500 * MIB),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_cases_that_never_reject(label: str, snap: ClusterSnapshot, size: int) -> None:
    assert (
        link_rejection(_request(size), _far(snap), snap, local=False, now=NOW) is None
    ), label


def test_an_unmeasured_link_or_no_clock_never_rejects() -> None:
    snap = dataclasses.replace(_snapshot(200e3), links={})
    assert (
        link_rejection(_request(500 * MIB), _far(snap), snap, local=False, now=NOW)
        is None
    )
    slow = _snapshot(200e3)
    # without ``now`` no link counts as fresh (the estimator's rule)
    assert (
        link_rejection(_request(500 * MIB), _far(slow), slow, local=False, now=None)
        is None
    )


def test_just_over_the_bytes_threshold_rejects() -> None:
    snap = _snapshot(200e3, min_bytes=64 * MIB)
    assert (
        link_rejection(_request(64 * MIB + 1), _far(snap), snap, local=False, now=NOW)
        is not None
    )


def test_a_pinned_job_or_forced_set_is_never_refused_for_its_link() -> None:
    snap = _snapshot(200e3)
    for kw in ({"pinned_node_id": "n_far"}, {"node_set": ("n_far", "n_holder")}):
        assert (
            link_rejection(
                _request(500 * MIB, **kw), _far(snap), snap, local=False, now=NOW
            )
            is None
        )


def test_a_split_job_counts_its_tree_without_the_items() -> None:
    snap = _snapshot(200e3)
    split = SplitInfo(items=100, item_bytes=480 * MIB, items_tree=TREE)
    # 500 MiB tree, 480 MiB of it items fetched per chunk: 20 MiB staged
    request = _request(500 * MIB, split=split)
    assert link_rejection(request, _far(snap), snap, local=False, now=NOW) is None
    heavy = _request(
        500 * MIB, split=SplitInfo(items=100, item_bytes=100 * MIB, items_tree=TREE)
    )
    assert link_rejection(heavy, _far(snap), snap, local=False, now=NOW) is not None
