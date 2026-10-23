"""V0.15 G6a (G5 debt; D364, Q23): the production caller of
``replay_decision`` - re-plan one stored decision and compare it.

* a stored decision (ML or not, whole job or split) replays ``reproduced``
  when the loader gives back its model set;
* a changed stored row is ``differs`` with the differing paths (and their
  total), a changed digest names the digest first;
* the model set gone -> ``ml_model_unavailable``, never a calibration
  re-plan; no replay -> ``no_replay``; garbage -> ``replay_unreadable``;
* ``json_differences`` compares numbers by value, everything else exactly;
* :class:`DecisionReplayer` calls the loader through ``RegistryLoader.
  blocking`` from a worker thread (the event loop keeps running);
* the API's status literal equals the module's status list.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import typing
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


def _stored_json(value: Any) -> Any:
    return json.loads(json.dumps(value))


def _replace(stored: dr.StoredDecision, **kw: Any) -> dr.StoredDecision:
    return dataclasses.replace(stored, **kw)


def _case(*, split: bool, with_ml: bool) -> tuple[Any, dr.StoredDecision]:
    request, snapshot, now = synthetic_fabric(4, split=split, seed=33)
    view = project(request, snapshot)
    ml = None
    if with_ml:
        rules = {(t, None): Rule(y_hat=-0.2, half_width=0.05) for t in ML_TERMS}
        ml, _ = stub_ml(rules)
    decision = plan(request, view, "cost_model", now, ml=ml)
    replay = json.loads(canonical_json(to_replay(request, view, now, ml=decision.ml)))
    stored = dr.StoredDecision(
        decision_id=7,
        job_id=request.job_id,
        outcome="dispatched",
        policy="cost_model",
        chosen_node_id=None if decision.chosen is None else decision.chosen.node_id,
        snapshot_digest=decision.snapshot_digest,
        candidates=_stored_json(candidates_json(decision)),
        replay=replay,
    )
    return ml, stored


@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
@pytest.mark.parametrize("with_ml", [False, True], ids=["no-ml", "ml"])
def test_a_stored_decision_reproduces(split: bool, with_ml: bool) -> None:
    ml, stored = _case(split=split, with_ml=with_ml)
    asked: list[str] = []

    def load(version: str) -> Any:
        asked.append(version)
        return ml if ml is not None and version == ml.version else None

    found = dr.replay_stored(stored, load)
    assert found.status == dr.REPRODUCED, found.differences
    assert found.differences == () and found.differences_total == 0
    assert found.snapshot_digest_match is True
    assert found.replayed_chosen_node_id == stored.chosen_node_id
    if with_ml:
        assert found.ml_model_version == "stub-v1" and asked == ["stub-v1"]
    else:
        assert found.ml_model_version is None and asked == []


def test_a_changed_row_differs_with_its_paths() -> None:
    ml, stored = _case(split=False, with_ml=True)
    rows = copy.deepcopy(list(stored.candidates))
    rows[0]["estimate"]["terms"]["compute"]["seconds"] += 1.0
    rows[-1]["rank"] = 99
    changed = _replace(stored, candidates=rows)
    found = dr.replay_stored(changed, lambda v: ml)
    assert found.status == dr.DIFFERS
    assert found.differences_total == 2
    assert found.differences[0].startswith(
        "candidates[0].estimate.terms.compute.seconds: "
    )
    assert found.differences[1].startswith(f"candidates[{len(rows) - 1}].rank: 99 != ")


def test_a_changed_digest_is_named_first() -> None:
    _, stored = _case(split=False, with_ml=False)
    found = dr.replay_stored(_replace(stored, snapshot_digest="0" * 64), lambda v: None)
    assert found.status == dr.DIFFERS
    assert found.snapshot_digest_match is False
    assert found.differences[0].startswith("snapshot_digest: " + "0" * 64)


def test_the_differences_list_is_bounded() -> None:
    _, stored = _case(split=True, with_ml=False)
    rows = copy.deepcopy(list(stored.candidates))
    for row in rows:
        row["node_id"] = "elsewhere"
        row["score"] = -1.0
        row["feasible"] = "maybe"
        row["extra"] = 1
    found = dr.replay_stored(_replace(stored, candidates=rows), lambda v: None)
    assert found.differences_total > dr.MAX_DIFFERENCES
    assert len(found.differences) == dr.MAX_DIFFERENCES


@pytest.mark.parametrize("split", [False, True], ids=["whole", "split"])
def test_a_gone_model_set_is_unavailable_never_a_calibration_replan(
    split: bool,
) -> None:
    _, stored = _case(split=split, with_ml=True)
    found = dr.replay_stored(stored, lambda v: None)
    assert found.status == "ml_model_unavailable"
    assert found.ml_model_version == "stub-v1"
    assert found.replayed_chosen_node_id is None
    assert found.snapshot_digest_match is None


def test_no_replay_and_an_unreadable_one() -> None:
    _, stored = _case(split=False, with_ml=True)
    none = dr.replay_stored(_replace(stored, replay=None), lambda v: None)
    assert none.status == dr.NO_REPLAY and none.ml_model_version is None
    broken = dict(stored.replay or {})
    broken["snapshot"] = "nope"
    bad = dr.replay_stored(_replace(stored, replay=broken), lambda v: None)
    assert bad.status == dr.REPLAY_UNREADABLE
    assert bad.ml_model_version == "stub-v1"  # the ml key still names it
    unknown = dr.replay_stored(_replace(stored, replay={"version": 99}), lambda v: None)
    assert unknown.status == dr.REPLAY_UNREADABLE


def test_json_differences_rules() -> None:
    assert dr.json_differences({"a": 1}, {"a": 1.0}) == []
    assert dr.json_differences({"a": [1, 2]}, {"a": [1, 2]}) == []
    assert dr.json_differences({"a": True}, {"a": 1}) == ["a: true != 1"]
    assert dr.json_differences({"a": None}, {"a": 0}) == ["a: null != 0"]
    assert dr.json_differences({"a": [1]}, {"a": [1, 2]}) == ["a[1]: missing != 2"]
    assert dr.json_differences({"b": 1}, {}) == ["b: 1 != missing"]
    assert dr.json_differences("x" * 200, "y")[0].endswith('... != "y"')


def test_the_api_status_literal_is_the_module_list() -> None:
    pytest.importorskip("fastapi", reason="needs the controller extra")
    from meshforge.controller.api import decision_replay_routes as routes

    assert set(typing.get_args(routes.ReplayStatus.__value__)) == set(
        dr.REPLAY_STATUSES
    )


class _Loader:
    """``RegistryLoader``'s surface the replayer uses (load + blocking)."""

    def __init__(self, ml: Any) -> None:
        self.ml = ml
        self.threads: list[bool] = []

    async def load(self, version: str) -> Any:
        return self.ml if version == self.ml.version else None

    def blocking(self, loop: asyncio.AbstractEventLoop) -> Any:
        from meshforge.controller.ml.registry_loader import RegistryLoader

        return RegistryLoader.blocking(self, loop)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_replayer_loads_through_the_event_loop_from_a_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ml, stored = _case(split=False, with_ml=True)

    async def read(_engine: Any, decision_id: int) -> dr.StoredDecision | None:
        return stored if decision_id == 7 else None

    monkeypatch.setattr(dr, "read_decision", read)
    replayer = dr.DecisionReplayer(engine=None, loader=_Loader(ml))  # type: ignore[arg-type]
    found = await replayer.replay(7)
    assert found is not None and found.status == dr.REPRODUCED
    assert await replayer.replay(8) is None
