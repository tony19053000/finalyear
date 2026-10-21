"""V0.14 G1 (D290/D291): the one candidate generator.

Whole jobs get Local + Mode 1 per node; split jobs get the same one-lane
candidates plus the Mode 3 prefix sets over their feasible lanes; an
accelerator request (Mode 2/4, deferred by ADR-0012) gets nothing and a typed
refusal. Stored modes are unchanged (``mode1`` -> ``offload``, ``mode3`` ->
``split``). The planner package stays pure (AST import test, including the
new modules).
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest
from mf_planner_fabrics import NOW, synthetic_fabric

from meshforge.controller import planner as planner_pkg
from meshforge.controller.planner import (
    AcceleratorRequest,
    RejectCode,
    RequestKind,
    candidates,
    plan,
    request_kind,
)
from meshforge.controller.planner.candidates import FORCED_SET, STORED_MODE
from meshforge.controller.planner.split import lane, prefix_sets
from meshforge.controller.planner.types import STATIC_CODES, SplitInfo

pytestmark = pytest.mark.unit


def _lanes(request, snapshot):  # type: ignore[no-untyped-def]
    return [
        lane(
            request,
            n,
            "cpu",
            snapshot,
            local=n.node_id == request.submitter_node_id,
            now=NOW,
        )
        for n in sorted(snapshot.nodes, key=lambda n: n.node_id)
    ]


def test_whole_job_local_then_mode1_in_node_id_order() -> None:
    request, snapshot, _ = synthetic_fabric(4, split=False)
    generated = candidates(request, snapshot)
    assert generated.kind is RequestKind.JOB and generated.refused is None
    ids = sorted(n.node_id for n in snapshot.nodes)
    assert [c.node_set for c in generated.candidates] == [(i,) for i in ids]
    assert [c.mode for c in generated.candidates] == [
        "local",
        "mode1",
        "mode1",
        "mode1",
    ]
    assert [c.stored_mode for c in generated.candidates] == [
        "local",
        "offload",
        "offload",
        "offload",
    ]
    assert generated.sets == () and generated.forced is None


def test_pinned_job_has_its_one_node_or_none() -> None:
    request, snapshot, _ = synthetic_fabric(3, split=False)
    pinned = dataclasses.replace(request, pinned_node_id="node_002")
    assert [c.node_set for c in candidates(pinned, snapshot).candidates] == [
        ("node_002",)
    ]
    gone = dataclasses.replace(request, pinned_node_id="node_999")
    assert candidates(gone, snapshot).candidates == ()


def test_forced_node_set_limits_the_node_candidates() -> None:
    request, snapshot, _ = synthetic_fabric(4, split=True)
    forced = dataclasses.replace(request, node_set=("node_001", "node_003"))
    nodes = candidates(forced, snapshot).candidates
    assert [c.node_id for c in nodes] == ["node_001", "node_003"]


def test_split_job_sets_are_the_prefix_sets_of_its_lanes() -> None:
    request, snapshot, _ = synthetic_fabric(5, split=True)
    lanes = _lanes(request, snapshot)
    generated = candidates(request, snapshot, lanes=lanes)
    assert generated.kind is RequestKind.SPLIT and generated.forced is None
    assert [c.node_id for c in generated.singles] == [x.node_id for x in lanes]
    assert generated.singles[0].mode == "local"
    expected = [
        tuple(x.node_id for x in group)
        for group in prefix_sets(lanes, items=2000, min_nodes=2, max_nodes=None)
    ]
    assert [c.node_set for c in generated.sets] == expected
    assert all(c.mode == "mode3" and c.stored_mode == "split" for c in generated.sets)
    assert len(generated.sets) <= 2 * len(lanes)  # O(N), never 2^N


@pytest.mark.parametrize(
    ("change", "forced", "code"),
    [
        ({"pinned_node_id": "node_000"}, "pinned", None),
        ({"split": SplitInfo(items=3)}, "split_too_small", RejectCode.SPLIT_TOO_SMALL),
        (
            {"split": SplitInfo(items=50, min_nodes=4)},
            "split_too_few_nodes",
            RejectCode.SPLIT_TOO_FEW_NODES,
        ),
    ],
)
def test_split_job_that_cannot_split_says_why(
    change: dict[str, object], forced: str, code: RejectCode | None
) -> None:
    request, snapshot, _ = synthetic_fabric(3, split=True)
    request = dataclasses.replace(request, **change)  # type: ignore[arg-type]
    generated = candidates(request, snapshot, lanes=_lanes(request, snapshot))
    assert generated.forced == forced and generated.sets == ()
    refused = generated.refused_sets
    if code is None:
        assert refused == ()
    else:
        (row,) = refused
        assert row.rejection is not None and row.rejection.code is code


def test_forced_set_is_one_candidate_or_a_refused_one() -> None:
    request, snapshot, _ = synthetic_fabric(4, split=True)
    forced = dataclasses.replace(request, node_set=("node_001", "node_002"))
    lanes = [x for x in _lanes(forced, snapshot) if x.node_id in forced.node_set]
    generated = candidates(forced, snapshot, lanes=lanes)
    (only,) = generated.sets
    assert set(only.node_set) == {"node_001", "node_002"}
    assert only.plan_params == {"forced": FORCED_SET} and generated.forced == FORCED_SET
    short = candidates(forced, snapshot, lanes=lanes[:1])
    (refused,) = short.refused_sets
    assert refused.rejection is not None
    assert refused.rejection.code is RejectCode.SPLIT_TOO_FEW_NODES
    assert "1 of 2" in refused.rejection.text


def test_accelerator_request_has_no_candidate_and_a_typed_refusal() -> None:
    _, snapshot, _ = synthetic_fabric(3, split=False)
    request = AcceleratorRequest(request_id="acc_1")
    assert request_kind(request) is RequestKind.ACCELERATOR
    generated = candidates(request, snapshot)
    assert generated.candidates == ()
    assert generated.refused is not None
    assert generated.refused.code is RejectCode.NOT_SUPPORTED_IN_BUILD
    assert generated.refused.code.value == "not_supported_in_build"
    assert RejectCode.NOT_SUPPORTED_IN_BUILD not in STATIC_CODES  # rejects no node


def test_lanes_for_a_whole_job_are_a_caller_bug() -> None:
    request, snapshot, _ = synthetic_fabric(2, split=False)
    with pytest.raises(ValueError, match="split job"):
        candidates(request, snapshot, lanes=[])


def test_stored_modes_never_change() -> None:
    assert dict(STORED_MODE) == {"local": "local", "mode1": "offload", "mode3": "split"}
    request, snapshot, _ = synthetic_fabric(4, split=True)
    decision = plan(request, snapshot, "cost_model", NOW)
    assert {c.mode for c in decision.candidates} <= {"local", "offload", "split"}


def test_planner_package_stays_pure() -> None:
    """The V0.6 rule for every module, the new ``candidates`` and ``replay``
    included: stdlib, ``meshforge.common`` and the planner itself only."""
    root = Path(planner_pkg.__file__).parent
    allowed = {
        "__future__",
        "collections",
        "dataclasses",
        "datetime",
        "enum",
        "hashlib",
        "json",
        "math",
        "random",
        "typing",
    }
    names = {p.name for p in root.glob("*.py")}
    assert {"candidates.py", "replay.py"} <= names
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for item in ast.walk(tree):
            modules: list[str] = []
            if isinstance(item, ast.Import):
                modules = [a.name for a in item.names]
            elif isinstance(item, ast.ImportFrom) and item.module:
                modules = [item.module]
            for name in modules:
                top = name.split(".")[0]
                if top == "meshforge":
                    assert name.startswith(
                        ("meshforge.common", "meshforge.controller.planner")
                    ), f"{path.name} imports {name}"
                else:
                    assert top in allowed, f"{path.name} imports {name}"
