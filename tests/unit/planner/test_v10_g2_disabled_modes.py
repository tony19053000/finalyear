"""V1.0 G2 (D418 A5/A6): ``[controller.planner] disabled_modes`` - the live
ablation switches. The generator leaves the disabled mode out for an
automatically placed job; pinned jobs and forced node sets are untouched; the
replay carries the switch only when it is set, so every other decision's
replay and digest are byte-for-byte what they were."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any

import pytest
from mf_planner_fabrics import synthetic_fabric
from pydantic import ValidationError

from meshforge.controller.planner import plan
from meshforge.controller.planner.candidates import MODE3_DISABLED, candidates
from meshforge.controller.planner.replay import canonical_json, from_replay, to_replay
from meshforge.controller.planner.types import ClusterSnapshot, PlanRequest
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit


def _with(snapshot: ClusterSnapshot, *modes: str) -> ClusterSnapshot:
    config = dataclasses.replace(snapshot.config, disabled_modes=frozenset(modes))
    return dataclasses.replace(snapshot, config=config)


def _modes(request: PlanRequest, snapshot: ClusterSnapshot) -> set[str]:
    return {c.mode for c in candidates(request, snapshot).candidates}


def test_mode1_disabled_leaves_a_whole_job_only_its_local_candidate() -> None:
    request, snapshot, _ = synthetic_fabric(4, split=False)
    assert _modes(request, snapshot) == {"local", "mode1"}
    generated = candidates(request, _with(snapshot, "mode1")).candidates
    assert [(c.mode, c.node_id) for c in generated] == [
        ("local", request.submitter_node_id)
    ]


def test_mode1_disabled_without_a_submitter_node_has_no_candidate() -> None:
    request, snapshot, _ = synthetic_fabric(3, split=False)
    request = dataclasses.replace(request, submitter_node_id=None, seeded=False)
    assert candidates(request, _with(snapshot, "mode1")).candidates == ()


def test_a_pinned_job_is_never_affected() -> None:
    request, snapshot, now = synthetic_fabric(4, split=False)
    other = snapshot.nodes[2].node_id
    pinned = dataclasses.replace(request, pinned_node_id=other)
    for modes in (("mode1",), ("mode3",), ("mode1", "mode3")):
        decision = plan(pinned, _with(snapshot, *modes), "cost_model", now)
        assert decision.chosen is not None
        assert decision.chosen.node_id == other


def test_mode3_disabled_runs_a_split_job_unsplit() -> None:
    request, snapshot, now = synthetic_fabric(5, split=True)
    free = plan(request, snapshot, "cost_model", now)
    assert free.chosen is not None and free.chosen.mode == "split"  # the fixture splits
    decision = plan(request, _with(snapshot, "mode3"), "cost_model", now)
    assert decision.chosen is not None
    assert decision.chosen.mode in ("local", "offload")
    assert all(c.mode != "split" for c in decision.candidates)
    assert decision.split is not None
    assert decision.split.margin.forced == MODE3_DISABLED
    assert not decision.split.split


def test_mode1_disabled_split_job_keeps_mode3_and_local_only() -> None:
    request, snapshot, now = synthetic_fabric(5, split=True)
    decision = plan(request, _with(snapshot, "mode1"), "cost_model", now)
    singles = [c for c in decision.candidates if c.mode != "split" and c.feasible]
    assert {c.node_id for c in singles} == {request.submitter_node_id}
    assert any(c.mode == "split" for c in decision.candidates)
    assert decision.chosen is not None and decision.chosen.mode in ("split", "local")


def test_mode1_disabled_split_job_without_local_lane_still_splits() -> None:
    """No single to compare a set with (Local unusable): the set is the plan,
    not an endless wait."""
    request, snapshot, now = synthetic_fabric(5, split=True)
    request = dataclasses.replace(request, submitter_node_id=None, seeded=False)
    decision = plan(request, _with(snapshot, "mode1"), "cost_model", now)
    assert decision.chosen is not None
    assert decision.chosen.mode == "split"


def test_both_disabled_split_job_runs_whole_on_local() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True)
    decision = plan(request, _with(snapshot, "mode1", "mode3"), "cost_model", now)
    assert decision.chosen is not None
    assert (decision.chosen.mode, decision.chosen.node_id) == (
        "local",
        request.submitter_node_id,
    )


def test_a_forced_node_set_is_never_affected() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True)
    wanted = tuple(n.node_id for n in snapshot.nodes[:3])
    forced = dataclasses.replace(request, node_set=wanted)
    for modes in (("mode1",), ("mode3",)):
        decision = plan(forced, _with(snapshot, *modes), "cost_model", now)
        assert decision.chosen is not None and decision.chosen.mode == "split"
        assert set(decision.chosen.node_set or ()) == set(wanted)


def test_rule_policies_obey_the_switch_too() -> None:
    request, snapshot, now = synthetic_fabric(4, split=False)
    for policy in ("random", "round_robin", "least_loaded"):
        decision = plan(request, _with(snapshot, "mode1"), policy, now)
        assert decision.chosen is not None
        assert decision.chosen.node_id == request.submitter_node_id


def test_replay_omits_an_empty_switch_and_round_trips_a_set_one() -> None:
    request, snapshot, now = synthetic_fabric(3, split=True)
    plain = to_replay(request, snapshot, now)
    assert "disabled_modes" not in plain["snapshot"]["config"]
    ablated = _with(snapshot, "mode3", "mode1")
    replay = to_replay(request, ablated, now)
    assert replay["snapshot"]["config"]["disabled_modes"] == ["mode1", "mode3"]
    _, back, _ = from_replay(json.loads(canonical_json(replay)))
    assert back.config.disabled_modes == frozenset({"mode1", "mode3"})
    # the replay is a projection (its digest names what it holds): every
    # other field of the decision is the same
    replayed = plan(request, back, "cost_model", now)
    original = plan(request, ablated, "cost_model", now)
    assert dataclasses.replace(replayed, snapshot_digest="") == dataclasses.replace(
        original, snapshot_digest=""
    )
    assert all(c.mode != "split" for c in original.candidates)


def test_the_digest_is_unchanged_without_the_switch_and_changes_with_it() -> None:
    request, snapshot, now = synthetic_fabric(3, split=False)
    base = plan(request, snapshot, "cost_model", now).snapshot_digest
    explicit_empty = _with(snapshot)
    assert plan(request, explicit_empty, "cost_model", now).snapshot_digest == base
    ablated = plan(request, _with(snapshot, "mode1"), "cost_model", now)
    assert ablated.snapshot_digest != base


class _Manager:
    def get(self, node_id: str) -> Any:
        return None

    def get_lingering(self, _node_id: str) -> Any:
        return None


def test_the_dispatcher_passes_the_setting_to_the_planner() -> None:
    from meshforge.controller.jobs.planning import DispatchPlanner
    from meshforge.controller.settings import JobApiSettings, SplitSettings

    def config(**planner: Any) -> Any:
        return DispatchPlanner(
            manager=_Manager(),  # type: ignore[arg-type]
            settings=JobApiSettings(),
            planner=PlannerSettings(**planner),
            clock=lambda: 1000.0,
            wall_clock=lambda: dt.datetime(2026, 10, 27, tzinfo=dt.UTC),
            started_at=0.0,
            split=SplitSettings(),
        ).config

    assert config().disabled_modes == frozenset()
    assert config(disabled_modes=["mode3"]).disabled_modes == frozenset({"mode3"})


def test_settings_validate_sort_and_dedupe() -> None:
    assert PlannerSettings().disabled_modes == ()
    assert PlannerSettings(
        disabled_modes=["mode3", "mode1", "mode3"]
    ).disabled_modes == (
        "mode1",
        "mode3",
    )
    with pytest.raises(ValidationError):
        PlannerSettings(disabled_modes=["mode2"])
    with pytest.raises(ValidationError):
        PlannerSettings(disabled_modes=["local"])
