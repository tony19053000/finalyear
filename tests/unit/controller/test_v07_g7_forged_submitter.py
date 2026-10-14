"""V0.7 G7 (THREAT_MODEL RE-T2, D145c): a forged ``submitter_node_id`` grants
nothing.

The CLI names "its" node (``submitter_node_id``, and ``seeded`` = the project
tree is already in that node's cache) but holds no node certificate, so the
claim is unproven. It is a placement *hint*: it makes that node the **Local**
candidate and lets the estimator drop the tree transfer - it must never make
an infeasible node feasible. Here every static rejection (incl. every owner
policy code) and every live rejection is planned again with the refusing node
named as the submitter, seeded: the decision is the same refusal, with the
same code.

One rule depends on the hint by design: a *seeded* Local candidate needs no
disk for the tree (``disk_short``), because the tree is claimed to be cached
there. A forged claim only moves the check to the node: the agent stages what
is missing under its own free-disk floor (``disk_full``, D116/D143) - cost and
time, never authority. Pinned here so a change to it is a decision, not an
accident.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_v06_g2_planner import LIVE_CASES, STATIC_CASES, node, request, snap

from meshforge.common.manifest import POLICIES
from meshforge.common.owner_policy import OwnerPolicy
from meshforge.controller.planner.feasibility import RejectCode
from meshforge.controller.planner.plan import plan

pytestmark = [pytest.mark.unit, pytest.mark.security]

FORGED = {"submitter_node_id": "n1", "seeded": True}


def _rejection_of(decision: Any, node_id: str) -> RejectCode | None:
    (candidate,) = [c for c in decision.candidates if c.node_id == node_id]
    return None if candidate.rejection is None else candidate.rejection.code


@pytest.mark.parametrize(("code", "node_kw", "req_kw"), STATIC_CASES)
@pytest.mark.parametrize("policy", POLICIES)
def test_a_forged_submitter_cannot_lift_a_static_or_policy_rejection(
    code: RejectCode, node_kw: dict[str, Any], req_kw: dict[str, Any], policy: str
) -> None:
    honest = plan(request(**req_kw), snap(node("n1", **node_kw)), policy)
    forged = plan(request(**req_kw, **FORGED), snap(node("n1", **node_kw)), policy)
    for decision in (honest, forged):
        assert decision.chosen is None
        assert _rejection_of(decision, "n1") is code
        assert decision.all_static_rejections
    (local,) = forged.candidates
    assert local.mode == "local"  # the hint did take effect - and changed nothing


@pytest.mark.parametrize(
    ("code", "node_kw", "req_kw"),
    [case for case in LIVE_CASES if case[0] is not RejectCode.DISK_SHORT],
)
def test_a_forged_submitter_cannot_lift_a_live_rejection(
    code: RejectCode, node_kw: dict[str, Any], req_kw: dict[str, Any]
) -> None:
    forged = plan(
        request(**req_kw, **FORGED), snap(node("n1", **node_kw)), "cost_model"
    )
    assert forged.chosen is None
    assert _rejection_of(forged, "n1") is code


def test_a_forged_submitter_never_beats_a_feasible_node_onto_an_infeasible_one() -> (
    None
):
    """Two nodes: the named one refuses (owner policy), the other is fine -
    the job goes to the other one, whatever the hint says."""
    refusing = node("n1", policy=OwnerPolicy())  # accept_jobs = false
    decision = plan(request(**FORGED), snap(refusing, node("n2")), "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "n2"
    assert _rejection_of(decision, "n1") is RejectCode.POLICY_NOT_ACCEPTING


def test_the_one_rule_the_hint_changes_is_the_seeded_tree_disk_check() -> None:
    (case,) = [c for c in LIVE_CASES if c[0] is RejectCode.DISK_SHORT]
    _, node_kw, req_kw = case
    honest = plan(request(**req_kw), snap(node("n1", **node_kw)), "cost_model")
    assert _rejection_of(honest, "n1") is RejectCode.DISK_SHORT
    forged = plan(
        request(**req_kw, **FORGED), snap(node("n1", **node_kw)), "cost_model"
    )
    # Documented (module docstring): the claimed-cached tree needs no disk here;
    # the agent's own staging floor decides on the laptop.
    assert forged.chosen is not None and forged.chosen.node_id == "n1"
