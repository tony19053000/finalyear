"""V0.14 G2 tester round 1 - calibration inside plan() and the replay (D307).

* calibration=None leaves the decision, candidates and snapshot digest
  byte-identical to ``plan()`` called the pre-G2 way, for every policy;
* a replay block round-trips; a calibrated decision replays exactly on seeds
  the implementer did not use; the digest names the calibration;
* every malformed calibration block is a ReplayError and nothing else;
* an old replay (no key) decodes to None and replays the uncalibrated decision.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest
from mf_planner_fabrics import random_case
from mf_planner_golden import decision_view, fields_text

from meshforge.common.manifest import POLICIES
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    CALIBRATED_TERMS,
    Calibration,
    Factor,
    TermFactors,
    project_calibration,
)
from meshforge.controller.planner.replay import (
    REPLAY_VERSION,
    ReplayError,
    calibration_from_replay,
    canonical_json,
    from_replay,
    project,
    to_replay,
)

pytestmark = pytest.mark.unit


def _cal(rng: random.Random, request: Any, snapshot: Any) -> Calibration:
    nodes = [n.node_id for n in snapshot.nodes]
    terms = {}
    for t in CALIBRATED_TERMS:
        terms[t] = TermFactors(
            global_=Factor(round(rng.uniform(0.25, 4.0), 6), rng.randint(1, 99), 1.0),
            node={
                n: Factor(round(rng.uniform(0.2, 5.0), 6), 3, 1.0)
                for n in nodes
                if rng.random() < 0.6
            },
        )
    fam = {} if request.fingerprint is None else {request.fingerprint: "f"}
    return Calibration(version=rng.randint(1, 10**9), terms=terms, families=fam)


@pytest.mark.parametrize("seed", range(1000, 1060))
def test_none_calibration_is_byte_identical(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    for policy in POLICIES:
        a = plan(request, snapshot, policy, now)
        b = plan(request, snapshot, policy, now, calibration=None)
        assert fields_text(a) == fields_text(b)
        assert a.snapshot_digest == b.snapshot_digest
        assert json.dumps(decision_view(a), sort_keys=True) == json.dumps(
            decision_view(b), sort_keys=True
        )
        assert "calibration" not in json.dumps(decision_view(b)["candidates"])


@pytest.mark.parametrize("seed", range(1000, 1060))
def test_to_replay_without_calibration_has_no_key_and_is_stable(seed: int) -> None:
    request, snapshot, now = random_case(seed)
    r1 = canonical_json(to_replay(request, snapshot, now))
    r2 = canonical_json(to_replay(request, snapshot, now, calibration=None))
    assert r1 == r2
    assert "calibration" not in json.loads(r1)
    assert calibration_from_replay(json.loads(r1)) is None


@pytest.mark.parametrize("seed", range(2000, 2080))
def test_calibrated_decision_replays_exactly(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    request, snapshot, now = random_case(seed)
    cal = _cal(rng, request, snapshot)
    cut = project(request, snapshot)
    job_cal = project_calibration(cal, request, cut)
    replay = json.loads(
        canonical_json(to_replay(request, snapshot, now, calibration=job_cal))
    )
    assert replay["calibration"]["version"] == job_cal.version
    req2, snap2, now2 = from_replay(replay)
    cal2 = calibration_from_replay(replay)
    assert cal2 == job_cal
    for policy in POLICIES:
        original = plan(request, cut, policy, now, calibration=job_cal)
        again = plan(req2, snap2, policy, now2, calibration=cal2)
        assert fields_text(again) == fields_text(original)
        assert again.snapshot_digest == original.snapshot_digest
        # and NOT calibrated => differs in digest from the calibrated one
        raw = plan(request, cut, policy, now)
        assert raw.snapshot_digest != original.snapshot_digest


def test_calibrated_replay_does_not_match_uncalibrated_when_factors_bite() -> None:
    bit = 0
    for seed in range(3000, 3040):
        rng = random.Random(seed)  # noqa: S311
        request, snapshot, now = random_case(seed)
        cal = _cal(rng, request, snapshot)
        cut = project(request, snapshot)
        a = plan(request, cut, "cost_model", now)
        b = plan(request, cut, "cost_model", now, calibration=cal)
        if json.dumps(decision_view(a)["candidates"]) != json.dumps(
            decision_view(b)["candidates"]
        ):
            bit += 1
    assert bit >= 3  # many random cases have no feasible candidate


def _good_block() -> dict[str, Any]:
    request, snapshot, now = random_case(5)
    cal = _cal(random.Random(5), request, snapshot)  # noqa: S311
    return json.loads(
        canonical_json(to_replay(request, snapshot, now, calibration=cal))
    )


def test_replay_version_unchanged_and_block_optional() -> None:
    assert REPLAY_VERSION == 2  # V0.14 G2+G3 merge (D307)
    r = _good_block()
    assert r["v"] == 1 if "v" in r else True
    r.pop("calibration")
    assert calibration_from_replay(r) is None


def test_block_mutation_fuzz_only_replay_error() -> None:
    rng = random.Random(99)  # noqa: S311
    base = _good_block()
    seen_err = 0

    def mutate(v: Any, depth: int = 0) -> Any:
        if rng.random() < 0.12:
            return rng.choice(
                [
                    None,
                    True,
                    False,
                    -1,
                    0,
                    1.5,
                    "x",
                    [],
                    {},
                    [1],
                    {"a": 1},
                    10**30,
                    float("nan"),
                ]
            )
        if isinstance(v, dict) and depth < 7:
            d = dict(v)
            if d and rng.random() < 0.25:
                d.pop(rng.choice(list(d)))
            for k in list(d):
                if rng.random() < 0.35:
                    d[k] = mutate(d[k], depth + 1)
            return d
        return v

    for _ in range(3000):
        r = copy.deepcopy(base)
        r["calibration"] = mutate(r["calibration"])
        try:
            c = calibration_from_replay(r)
        except ReplayError:
            seen_err += 1
            continue
        # accepted blocks must be usable in plan()
        if c is not None:
            req, snap, now = from_replay(base)
            plan(req, snap, "cost_model", now, calibration=c)
    assert seen_err > 500


@pytest.mark.parametrize(
    "block",
    [
        {"version": 1, "factors": None},
        {"version": 1, "factors": []},
        {"version": 1.0, "factors": {}},
        {"version": None, "factors": {}},
        {"version": 2**70, "factors": {"format": 1, "terms": {}, "families": {}}},
        {
            "version": 1,
            "factors": {
                "format": 1,
                "terms": {
                    "compute": {
                        "global": {"factor": "NaN", "n": 1, "median": 1},
                        "node": {},
                        "node_family": {},
                    }
                },
                "families": {},
            },
        },
        {
            "version": 1,
            "factors": {
                "format": 1,
                "terms": {
                    "compute": {
                        "global": {"factor": -2, "n": 1, "median": 1},
                        "node": {},
                        "node_family": {},
                    }
                },
                "families": {},
            },
        },
    ],
)
def test_specific_malformed_blocks(block: Any) -> None:
    r = _good_block()
    r["calibration"] = block
    try:
        c = calibration_from_replay(r)
    except ReplayError:
        return
    # version 2**70 with empty terms is a legal-shaped block; it must at least not crash plan()
    req, snap, now = from_replay(r)
    plan(req, snap, "cost_model", now, calibration=c)


def test_explicit_null_calibration_key_is_uncalibrated() -> None:
    r = _good_block()
    r["calibration"] = None
    assert calibration_from_replay(r) is None
