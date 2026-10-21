"""V0.14 G3 (D307): the replay carries the G3 inputs as **optional** keys.

* a replay with reliability + objective + the G3 config reproduces its
  decision exactly, for every policy (property test over seeded fabrics);
* a replay stored before G3 (no ``objective``, no ``reliability``, none of the
  G3 config keys) decodes to the pre-G3 behaviour: min_time, no risk term,
  no link rejection, no pruning - and keeps its digest;
* ``REPLAY_VERSION`` is 2 since the G2+G3 merge (1 still reads).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import random
from typing import Any

import pytest
from mf_planner_fabrics import random_case, synthetic_fabric
from mf_planner_golden import decision_view, fields_text

from meshforge.common.manifest import OBJECTIVES, POLICIES
from meshforge.controller.planner import plan
from meshforge.controller.planner.replay import (
    REPLAY_VERSION,
    canonical_json,
    from_replay,
    project,
    to_replay,
)
from meshforge.controller.planner.risk import RISK_TERM
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    PlannerConfig,
    PlanRequest,
    Reliability,
    RiskConfig,
)

pytestmark = pytest.mark.unit

G3_CONFIG_KEYS = (
    "risk",
    "prefer_local_margin",
    "min_useful_bw_bytes_s",
    "min_useful_bytes",
    "prune_dominated",
)


def _with_g3(
    seed: int,
    request: PlanRequest,
    snapshot: ClusterSnapshot,
) -> tuple[PlanRequest, ClusterSnapshot]:
    rng = random.Random(seed * 7919)  # noqa: S311 - test data
    nodes = tuple(
        dataclasses.replace(
            n,
            reliability=None
            if rng.random() < 0.15
            else Reliability(
                failures=(f := rng.randint(0, 6)),
                total=f + rng.randint(0, 40),
                window_days=7.0,
            ),
        )
        for n in snapshot.nodes
    )
    config = dataclasses.replace(
        snapshot.config,
        risk=RiskConfig(
            prior=rng.choice(((1.0, 19.0), (2.0, 8.0))),
            lease_release_s=rng.choice((60.0, 5.0)),
        ),
        prefer_local_margin=rng.choice((0.25, 0.0, 0.6)),
        min_useful_bw_bytes_s=rng.choice((0.0, 1e6, 5e7)),
        min_useful_bytes=rng.choice((64 * 1024**2, 1)),
        prune_dominated=rng.random() < 0.8,
    )
    objective = rng.choice((None, *OBJECTIVES))
    return (
        dataclasses.replace(request, objective=objective),  # type: ignore[arg-type]
        dataclasses.replace(snapshot, nodes=nodes, config=config),
    )


def _roundtrip(
    request: PlanRequest, snapshot: ClusterSnapshot, now: dt.datetime | None
) -> tuple[PlanRequest, ClusterSnapshot, dt.datetime | None]:
    return from_replay(json.loads(canonical_json(to_replay(request, snapshot, now))))


@pytest.mark.parametrize("block", range(4))
def test_a_g3_replay_reproduces_its_decision_for_every_policy(block: int) -> None:
    checked = risked = 0
    for seed in range(block, 240, 4):
        base_request, base_snapshot, now = random_case(seed)
        request, snapshot = _with_g3(seed, base_request, base_snapshot)
        view = project(request, snapshot)
        again = _roundtrip(request, snapshot, now)
        assert again[0].objective == request.objective
        for policy in POLICIES:
            try:
                want = plan(request, view, policy, now)
            except ValueError:
                continue
            got = plan(again[0], again[1], policy, again[2])
            assert fields_text(got) == fields_text(want), (seed, policy)
            assert decision_view(got) == decision_view(want)
            checked += 1
            risked += any(
                c.estimate is not None and RISK_TERM in c.estimate.extra
                for c in got.candidates
            )
    assert checked > 300 and risked > 15, (checked, risked)


def test_synthetic_fabrics_with_every_objective_replay_exactly() -> None:
    for split in (False, True):
        for objective in OBJECTIVES:
            request, snapshot, now = synthetic_fabric(8, split=split, seed=5)
            request, snapshot = _with_g3(3, request, snapshot)
            request = dataclasses.replace(request, objective=objective)
            view = project(request, snapshot)
            back = _roundtrip(request, snapshot, now)
            assert fields_text(plan(*back[:2], "cost_model", back[2])) == fields_text(
                plan(request, view, "cost_model", now)
            )


def _pre_g3(replay: dict[str, Any]) -> dict[str, Any]:
    """``replay`` as G1 stored it: none of the G3 keys."""
    old = json.loads(json.dumps(replay))
    old["request"].pop("objective")
    for node in old["snapshot"]["nodes"]:
        node.pop("reliability")
    for key in G3_CONFIG_KEYS:
        old["snapshot"]["config"].pop(key)
    return old


def test_a_replay_without_the_g3_keys_is_the_pre_g3_planner() -> None:
    request, snapshot, now = synthetic_fabric(6, split=True, seed=2)
    request, snapshot = _with_g3(11, request, snapshot)
    old = _pre_g3(to_replay(request, snapshot, now))
    assert old["replay_version"] == REPLAY_VERSION == 2  # V0.14 G2+G3 merge
    got_request, got_snapshot, _ = from_replay(old)
    assert got_request.objective is None
    assert all(n.reliability is None for n in got_snapshot.nodes)
    legacy = PlannerConfig()
    for key in G3_CONFIG_KEYS:
        assert getattr(got_snapshot.config, key) == getattr(legacy, key), key
    assert got_snapshot.config.min_useful_bw_bytes_s == 0.0
    assert got_snapshot.config.prune_dominated is False
    decision = plan(got_request, got_snapshot, "cost_model", now)
    assert decision.objective is None and decision.pruned == ()
    assert all(
        RISK_TERM not in c.estimate.extra for c in decision.candidates if c.estimate
    )


def test_unset_g3_inputs_leave_the_digest_as_before() -> None:
    request, snapshot, now = synthetic_fabric(4, split=False, seed=1)
    plain = plan(request, snapshot, "cost_model", now).snapshot_digest
    # setting a G3 input to its unset value changes nothing ...
    same = dataclasses.replace(snapshot, config=dataclasses.replace(snapshot.config))
    assert plan(request, same, "cost_model", now).snapshot_digest == plain
    # ... setting it changes the digest (it names what the planner saw)
    for changed_request, changed_snapshot in (
        (dataclasses.replace(request, objective="min_network"), snapshot),
        (
            request,
            dataclasses.replace(
                snapshot,
                config=dataclasses.replace(snapshot.config, prune_dominated=True),
            ),
        ),
        (
            request,
            dataclasses.replace(
                snapshot,
                nodes=(
                    dataclasses.replace(
                        snapshot.nodes[0], reliability=Reliability(0, 0, 7.0)
                    ),
                    *snapshot.nodes[1:],
                ),
            ),
        ),
    ):
        digest = plan(changed_request, changed_snapshot, "cost_model", now)
        assert digest.snapshot_digest != plain


@pytest.mark.parametrize(
    ("where", "bad"),
    [
        (("request", "objective"), 5),
        (("snapshot", "nodes", 0, "reliability"), [1, 2]),
        (
            ("snapshot", "nodes", 0, "reliability"),
            {"failures": True, "total": 1, "window_days": 7},
        ),
        (("snapshot", "nodes", 0, "reliability"), {"failures": 1, "total": 1}),
        (("snapshot", "config", "prune_dominated"), 1),
        (("snapshot", "config", "min_useful_bytes"), 1.5),
        (("snapshot", "config", "risk"), {"prior": [1]}),
    ],
    ids=repr,
)
def test_malformed_g3_keys_are_replay_errors(where: tuple[Any, ...], bad: Any) -> None:
    from meshforge.controller.planner.replay import ReplayError

    request, snapshot, now = synthetic_fabric(3, split=False, seed=1)
    request, snapshot = _with_g3(1, request, snapshot)
    replay = json.loads(canonical_json(to_replay(request, snapshot, now)))
    target: Any = replay
    for key in where[:-1]:
        target = target[key]
    target[where[-1]] = bad
    with pytest.raises(ReplayError):
        from_replay(replay)


def test_an_unknown_objective_decodes_but_does_not_plan() -> None:
    request, snapshot, now = synthetic_fabric(3, split=False, seed=1)
    replay = json.loads(canonical_json(to_replay(request, snapshot, now)))
    replay["request"]["objective"] = "max_fun"
    got_request, got_snapshot, got_now = from_replay(replay)
    assert got_request.objective == "max_fun"
    with pytest.raises(ValueError, match="objective"):
        plan(got_request, got_snapshot, "cost_model", got_now)
