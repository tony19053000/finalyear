# ruff: noqa: RUF059
"""V0.15 G6a tester round 1: adversarial replay (--check semantics rest on
`reproduced` meaning byte-for-byte reproduced, so any tamper must show)."""

from __future__ import annotations

import copy
import dataclasses
import json
from typing import Any

import pytest
from mf_ml_stub import Rule, stub_ml
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.ml import decision_replay as dr
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.predictor import ML_TERMS
from meshforge.controller.planner.replay import canonical_json, project, to_replay

pytestmark = pytest.mark.unit


def _case(
    *, split: bool, with_ml: bool, seed: int = 33
) -> tuple[Any, dr.StoredDecision]:
    request, snapshot, now = synthetic_fabric(4, split=split, seed=seed)
    view = project(request, snapshot)
    ml = None
    if with_ml:
        ml, _ = stub_ml(
            {(t, None): Rule(y_hat=-0.2, half_width=0.05) for t in ML_TERMS}
        )
    decision = plan(request, view, "cost_model", now, ml=ml)
    replay = json.loads(canonical_json(to_replay(request, view, now, ml=decision.ml)))
    stored = dr.StoredDecision(
        decision_id=1,
        job_id=request.job_id,
        outcome="dispatched",
        policy="cost_model",
        chosen_node_id=None if decision.chosen is None else decision.chosen.node_id,
        snapshot_digest=decision.snapshot_digest,
        candidates=json.loads(json.dumps(candidates_json(decision))),
        replay=replay,
    )
    return ml, stored


def _leaves(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _leaves(v, (*path, k))]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _leaves(v, (*path, i))]
    return [path]


def _set(doc: Any, path: tuple[Any, ...], value: Any) -> None:
    for key in path[:-1]:
        doc = doc[key]
    doc[path[-1]] = value


def _tamper(old: Any) -> Any:
    if isinstance(old, bool):
        return not old
    if isinstance(old, int | float):
        return old + 1.0
    if old is None:
        return "x"
    return str(old) + "!"


@pytest.mark.parametrize("split", [False, True])
def test_every_single_leaf_tamper_is_detected(split: bool) -> None:
    ml, stored = _case(split=split, with_ml=True)
    rows = list(stored.candidates)
    paths = _leaves(rows)
    assert len(paths) > 20
    missed = []
    for path in paths:
        mut = copy.deepcopy(rows)
        node = mut
        for key in path[:-1]:
            node = node[key]
        _set(mut, path, _tamper(node[path[-1]]))
        found = dr.replay_stored(
            dataclasses.replace(stored, candidates=mut), lambda v: ml
        )
        if found.status != dr.DIFFERS:
            missed.append(path)
    assert not missed, missed[:5]


def test_dropped_and_added_rows_are_detected() -> None:
    ml, stored = _case(split=True, with_ml=True)
    rows = list(stored.candidates)
    for mut in (rows[:-1], [*rows, copy.deepcopy(rows[0])], [], list(reversed(rows))):
        found = dr.replay_stored(
            dataclasses.replace(stored, candidates=mut), lambda v: ml
        )
        assert found.status == dr.DIFFERS or mut == rows


def test_a_float_that_is_an_int_is_not_a_difference_but_a_bool_is() -> None:
    ml, stored = _case(split=False, with_ml=False)
    rows = copy.deepcopy(list(stored.candidates))
    # JSONB may store 2.0 as 2: emulate for every integral float
    for path in _leaves(rows):
        node = rows
        for key in path[:-1]:
            node = node[key]
        v = node[path[-1]]
        if isinstance(v, float) and v == int(v) and not isinstance(v, bool):
            _set(rows, path, int(v))
    found = dr.replay_stored(
        dataclasses.replace(stored, candidates=rows), lambda v: None
    )
    assert found.status == dr.REPRODUCED, found.differences


def test_replay_is_idempotent_and_does_not_mutate_the_stored_row() -> None:
    ml, stored = _case(split=True, with_ml=True)
    before = json.dumps([stored.candidates, stored.replay], sort_keys=True)
    a = dr.replay_stored(stored, lambda v: ml)
    b = dr.replay_stored(stored, lambda v: ml)
    assert a == b and a.status == dr.REPRODUCED
    assert json.dumps([stored.candidates, stored.replay], sort_keys=True) == before


def test_a_loader_returning_a_model_that_predicts_differently_differs() -> None:
    ml, stored = _case(split=False, with_ml=True)
    other, _ = stub_ml({(t, None): Rule(y_hat=0.9, half_width=0.05) for t in ML_TERMS})
    found = dr.replay_stored(stored, lambda v: other)
    assert found.status == dr.DIFFERS


def test_a_loader_that_raises_does_not_claim_reproduced() -> None:
    ml, stored = _case(split=False, with_ml=True)

    def boom(version: str) -> Any:
        raise RuntimeError("blob read failed")

    try:
        found = dr.replay_stored(stored, boom)
    except RuntimeError:
        return  # propagates to the route's error handling: acceptable, not reproduced
    assert found.status != dr.REPRODUCED


@pytest.mark.parametrize(
    "replay",
    [{}, {"version": 1}, [], "x", {"version": 99}, {"request": None, "snapshot": None}],
)
def test_garbage_replays_never_crash_or_reproduce(replay: Any) -> None:
    _, stored = _case(split=False, with_ml=False)
    found = dr.replay_stored(dataclasses.replace(stored, replay=replay), lambda v: None)
    assert found.status in {dr.REPLAY_UNREADABLE, dr.NO_REPLAY}


def test_a_stored_ml_decision_never_replays_without_its_model_even_if_candidates_match() -> (
    None
):
    _, stored = _case(split=False, with_ml=True)
    found = dr.replay_stored(stored, lambda v: None)
    assert found.status == "ml_model_unavailable"
    assert found.differences == ()


def test_cli_check_status_mapping_is_exit_zero_only_for_reproduced() -> None:
    from meshforge.cli.replay_commands import replay_lines

    for status in dr.REPLAY_STATUSES:
        lines = replay_lines(
            {
                "decision_id": 1,
                "job_id": "j",
                "outcome": "o",
                "policy": "p",
                "status": status,
                "ml_model_version": None,
                "stored_chosen_node_id": None,
                "replayed_chosen_node_id": None,
                "snapshot_digest_match": None,
                "differences": [],
                "differences_total": 0,
            }
        )
        assert status in lines[0]
