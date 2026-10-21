"""V0.14 G2 (D293, D307): calibration inside ``plan()`` and in the replay.

* **No calibration = today's planner:** ``plan(..., calibration=None)`` is the
  planner before G2 field for field (the G1 golden corpus stays
  byte-identical - ``test_v014_g1_golden.py`` runs unchanged), and a
  calibration whose every factor is 1 changes no second, total, rank or
  choice (only ``detail.calibration`` and the digest name it).
* **Replay with calibration reproduces the decision exactly** (the G1
  property test extended): over seeded random fabrics and random calibrations
  - global, per node, per node + family, nodes not in the fabric, a family map
  naming the job's fingerprint or not - and for every policy, ``plan`` on the
  replay's request, snapshot, ``now`` **and calibration block** equals the
  dispatcher's decision (planned on the projected snapshot and projected
  calibration), every field, and the projection of the calibration drops
  nothing the planner looks up.
* Old replays (no ``calibration`` key) decode to None; a malformed block is a
  :class:`ReplayError`.
* Calibration really moves decisions: a factor can flip the pick and the
  split margin test sees calibrated lanes.
"""

from __future__ import annotations

import dataclasses
import json
import random
from typing import Any

import pytest
from mf_planner_fabrics import random_case
from mf_planner_golden import decision_view, fields_text

from meshforge.common.manifest import POLICIES
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitInfo,
    plan,
)
from meshforge.controller.planner.calibration import (
    CALIBRATED_TERMS,
    Calibration,
    Factor,
    TermFactors,
    project_calibration,
)
from meshforge.controller.planner.replay import (
    ReplayError,
    calibration_from_replay,
    canonical_json,
    from_replay,
    project,
    to_replay,
)

pytestmark = pytest.mark.unit

SEEDS = range(240)


def _sans_calibration(view: dict[str, Any]) -> dict[str, Any]:
    out = dict(view)
    for key in ("snapshot_digest", "fields_sha256"):
        out.pop(key, None)
    out["candidates"] = json.dumps(
        _strip(json.loads(view["candidates"])), sort_keys=True
    )
    return out


def _strip(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _strip(v) for k, v in value.items() if k != "calibration"}
    if isinstance(value, list):
        return [_strip(v) for v in value]
    return value


def random_calibration(
    rng: random.Random, request: PlanRequest, snapshot: ClusterSnapshot
) -> Calibration:
    nodes = [n.node_id for n in snapshot.nodes] + ["node_not_in_fabric"]
    families = ["fam_a", "fam_b"]

    def factor() -> Factor:
        value = rng.choice([1.0, rng.uniform(0.2, 5.0), 0.2, 5.0])
        return Factor(
            round(value, 6), rng.randint(1, 200), round(rng.uniform(0.1, 9), 6)
        )

    terms = {}
    for term in CALIBRATED_TERMS:
        if rng.random() < 0.2:
            continue
        terms[term] = TermFactors(
            global_=factor() if rng.random() < 0.7 else None,
            node={n: factor() for n in nodes if rng.random() < 0.5},
            node_family={
                n: {f: factor() for f in families if rng.random() < 0.5}
                for n in nodes
                if rng.random() < 0.4
            },
        )
    fams: dict[str, str] = {"other_fingerprint": "fam_b"}
    if request.fingerprint is not None and rng.random() < 0.6:
        fams[request.fingerprint] = rng.choice(families)
    return Calibration(version=rng.randint(1, 10**6), terms=terms, families=fams)


def _check(
    request: PlanRequest, snapshot: ClusterSnapshot, now: Any, cal: Calibration
) -> int:
    cut = project(request, snapshot)
    job_cal = project_calibration(cal, request, cut)
    replay = json.loads(
        canonical_json(to_replay(request, snapshot, now, calibration=job_cal))
    )
    again = from_replay(replay)
    back = calibration_from_replay(replay)
    assert back == job_cal
    chose = 0
    for policy in POLICIES:
        original = plan(request, cut, policy, now, calibration=job_cal)
        replayed = plan(*again[:2], policy, again[2], calibration=back)
        assert fields_text(replayed) == fields_text(original), policy
        assert decision_view(replayed) == decision_view(original), policy
        # the projection of the calibration drops nothing plan() looks up
        full = plan(request, cut, policy, now, calibration=cal)
        assert _sans_calibration(decision_view(full)) == _sans_calibration(
            decision_view(original)
        ), policy
        assert fields_text(
            dataclasses.replace(full, snapshot_digest="")
        ) == fields_text(dataclasses.replace(original, snapshot_digest="")), policy
        chose += original.chosen is not None
    return chose


@pytest.mark.parametrize("block", range(6))
def test_calibrated_decisions_replay_exactly_for_every_policy(block: int) -> None:
    chose = 0
    scaled = 0
    for seed in SEEDS[block::6]:
        request, snapshot, now = random_case(seed)
        rng = random.Random(10_000 + seed)  # noqa: S311 - seeded test data
        cal = random_calibration(rng, request, snapshot)
        chose += _check(request, snapshot, now, cal)
        decision = plan(
            request, project(request, snapshot), "cost_model", now, calibration=cal
        )
        scaled += "calibration" in json.dumps(decision_view(decision)["candidates"])
    assert chose > 0
    assert scaled > 0  # the draw really scaled terms


@pytest.mark.parametrize("seed", range(0, 240, 3))
def test_no_calibration_is_the_planner_before_g2(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    for policy in POLICIES:
        assert fields_text(plan(request, snapshot, policy, now, calibration=None)) == (
            fields_text(plan(request, snapshot, policy, now))
        )


@pytest.mark.parametrize("seed", range(0, 240, 2))
def test_factor_one_everywhere_changes_no_second_rank_or_choice(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    ones = Calibration(
        version=1,
        terms={t: TermFactors(global_=Factor(1.0, 5, 1.0)) for t in CALIBRATED_TERMS},
    )
    for policy in POLICIES:
        plain = plan(request, snapshot, policy, now)
        unit = plan(request, snapshot, policy, now, calibration=ones)
        assert _sans_calibration(decision_view(unit)) == _sans_calibration(
            decision_view(plain)
        ), policy
        assert (unit.chosen is None) == (plain.chosen is None)
        if plain.chosen is not None and unit.chosen is not None:
            assert unit.chosen.node_id == plain.chosen.node_id
            assert unit.chosen.node_set == plain.chosen.node_set


def test_a_replay_without_a_calibration_block_is_uncalibrated() -> None:
    request, snapshot, now = random_case(3)
    replay = json.loads(canonical_json(to_replay(request, snapshot, now)))
    assert "calibration" not in replay
    assert calibration_from_replay(replay) is None


@pytest.mark.parametrize(
    "block",
    [
        "x",
        [],
        {"factors": {}},
        {"version": "1", "factors": {}},
        {"version": -1, "factors": {}},
        {"version": True, "factors": {}},
        {"version": 1},
        {"version": 1, "factors": {"format": 9}},
        {"version": 1, "factors": {"format": 1, "terms": [], "families": {}}},
    ],
)
def test_a_malformed_calibration_block_is_a_replay_error(block: Any) -> None:
    request, snapshot, now = random_case(4)
    replay = json.loads(canonical_json(to_replay(request, snapshot, now)))
    replay["calibration"] = block
    with pytest.raises(ReplayError):
        calibration_from_replay(replay)


# ------------------------------------------------------------ it does decide


def _node(node_id: str, score: float) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=frozenset({"exec", "exec-split"}),
        cpu_slots=4,
        cpu_score=score,
    )


def _whole() -> tuple[PlanRequest, ClusterSnapshot]:
    request = PlanRequest(
        job_id="job_cal",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="k",
        fingerprint="fp1",
        history=History(
            by_node_s={"a": 10.0, "b": 12.0}, samples_by_node={"a": 5, "b": 5}
        ),
    )
    snapshot = ClusterSnapshot(nodes=(_node("a", 10.0), _node("b", 10.0)))
    return request, snapshot


def test_a_node_factor_flips_the_cost_model_pick() -> None:
    request, snapshot = _whole()
    assert plan(request, snapshot, "cost_model").chosen.node_id == "a"  # type: ignore[union-attr]
    slow_a = Calibration(
        version=4,
        terms={"compute": TermFactors(node={"a": Factor(1.5, 20, 1.6)})},
    )
    decision = plan(request, snapshot, "cost_model", calibration=slow_a)
    assert decision.chosen is not None and decision.chosen.node_id == "b"
    a_row = next(c for c in decision.candidates if c.node_id == "a")
    assert a_row.estimate is not None
    assert a_row.estimate.compute.seconds == 15.0
    assert a_row.estimate.compute.detail["calibration"] == {
        "factor": 1.5,
        "n": 20,
        "key_level": "node",
        "version": 4,
        "raw_s": 10.0,
    }


def test_the_family_key_is_found_through_the_fingerprint() -> None:
    request, snapshot = _whole()
    cal = Calibration(
        version=5,
        terms={
            "compute": TermFactors(
                node={"a": Factor(1.1, 3, 1.2)},
                node_family={"a": {"matmul": Factor(2.0, 9, 2.5)}},
            )
        },
        families={"fp1": "matmul"},
    )
    decision = plan(request, snapshot, "cost_model", calibration=cal)
    a_row = next(c for c in decision.candidates if c.node_id == "a")
    assert a_row.estimate is not None
    assert a_row.estimate.compute.detail["calibration"]["key_level"] == "node_family"
    other = dataclasses.replace(request, fingerprint="unknown")
    decision = plan(other, snapshot, "cost_model", calibration=cal)
    a_row = next(c for c in decision.candidates if c.node_id == "a")
    assert a_row.estimate is not None
    assert a_row.estimate.compute.detail["calibration"]["key_level"] == "node"


def test_split_lanes_are_calibrated_before_the_margin_test() -> None:
    from meshforge.controller.planner.types import EnvBuildStat

    nodes = tuple(_node(n, 10.0) for n in ("a", "b", "c"))
    request = PlanRequest(
        job_id="job_split",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="k",
        fingerprint="fp2",
        history=History(item_rate_by_node=dict.fromkeys(("a", "b", "c"), 1.0)),
        split=SplitInfo(items=300, min_nodes=2),
    )
    # every lane builds its environment (60 s, history): splitting pays
    snapshot = ClusterSnapshot(
        nodes=nodes, env_builds={"python-cpu": EnvBuildStat(median_s=60.0, samples=4)}
    )
    before = plan(request, snapshot, "cost_model")
    assert before.split is not None and before.split.split
    assert before.split.node_ids == ("a", "b", "c")
    # b and c really build 5x slower: their lanes start at 300 s, the set no
    # longer beats the best single node by the margin -> "don't split"
    slow = Calibration(
        version=9,
        terms={
            "env": TermFactors(
                node={"b": Factor(5.0, 50, 9.0), "c": Factor(5.0, 50, 9.0)}
            )
        },
    )
    after = plan(request, snapshot, "cost_model", calibration=slow)
    assert after.split is not None and not after.split.split
    margin = after.split.margin
    assert margin.set_s is not None and margin.threshold_s is not None
    assert margin.set_s > margin.threshold_s
    assert after.chosen is not None and after.chosen.node_id == "a"
    sets = [c for c in after.candidates if c.mode == "split" and c.lanes]
    starts = {lane.node_id: lane.start_s for c in sets for lane in c.lanes}
    assert starts["b"] == pytest.approx(300.0) and starts["a"] == pytest.approx(60.0)
