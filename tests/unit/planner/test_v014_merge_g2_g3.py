"""V0.14 G2 + G3 merge: calibration and the risk term together (D293, D296,
D307). The merge conditions, one test group each:

* **M1** - calibration is applied to the per-phase terms first; the risk term
  is computed from the **calibrated** ``T`` (and calibrated retry overhead)
  and added after them. Whole jobs and split jobs.
* **M2** - calibration never scales ``risk`` (``Estimate.extra``):
  ``NOT_CALIBRATED["risk"] == "derived"``, the term carries no
  ``detail.calibration`` and ``compute explain`` shows it without a factor.
* **M3** - calibration's match / fit / held-out error compare observed phases
  against the **un-risked** per-term predictions: decisions on a fabric whose
  nodes have ``p_fail > 0``, planned with risk on and with risk off, fit
  exactly the same factors (same canonical bytes) and the same held-out error.
* **M4** - ``REPLAY_VERSION`` is 2; the decoder reads 1 and 2; the G2
  ``calibration`` block and the G3 optional keys decode under both, absent =
  uncalibrated / pre-G3; the golden corpus (v1 replays, no G2/G3 keys)
  reproduces byte for byte under either version label; a property test over
  random fabrics with a random calibration **and** random reliability replays
  every policy's decision exactly.

(M5 - no new migration - is ``tests/component/store/test_store_migrations.py``
``HEAD = "0031"``.)
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import gzip
import io
import json
import random
import re
from pathlib import Path
from typing import Any

import pytest
from mf_planner_fabrics import random_case, synthetic_fabric
from mf_planner_golden import decision_view, fields_text
from rich.console import Console

from meshforge.cli.explain_commands import decision_tables
from meshforge.common.manifest import OBJECTIVES, POLICIES
from meshforge.controller.api.decision_routes import CandidateOut
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import (
    CALIBRATED_TERMS,
    NOT_CALIBRATED,
    Calibration,
    CalibrationConfig,
    DecisionOutcome,
    Factor,
    TermFactors,
    calibrate_estimate,
    calibration_json,
    canonical,
    fit,
    holdout_error,
    match,
    project_calibration,
    split_holdout,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import (
    READABLE_VERSIONS,
    REPLAY_VERSION,
    ReplayError,
    calibration_from_replay,
    canonical_json,
    from_replay,
    project,
    to_replay,
)
from meshforge.controller.planner.risk import RISK_TERM, p_fail, retry_overhead_s
from meshforge.controller.planner.types import (
    CandidateEval,
    ClusterSnapshot,
    Decision,
    Estimate,
    PlanRequest,
    Reliability,
    RiskConfig,
)

pytestmark = pytest.mark.unit

GOLDEN = Path(__file__).parent / "data" / "v014_g1_golden.json.gz"


def _everything(factor: float, version: int = 3) -> Calibration:
    """Every calibrated term scaled by ``factor`` (global key)."""
    return Calibration(
        version=version,
        terms={
            t: TermFactors(global_=Factor(factor, 12, factor)) for t in CALIBRATED_TERMS
        },
    )


def _shaky(snapshot: ClusterSnapshot, failures: int = 6) -> ClusterSnapshot:
    """Every node with ``p_fail > 0`` from history (different per node)."""
    return dataclasses.replace(
        snapshot,
        nodes=tuple(
            dataclasses.replace(
                n, reliability=Reliability(failures + i, 30 + 3 * i, 7.0)
            )
            for i, n in enumerate(snapshot.nodes)
        ),
    )


def _key(row: CandidateEval) -> tuple[str, str, tuple[str, ...]]:
    return (row.mode, row.node_id, row.node_set)


def _rows(decision: Decision) -> dict[tuple[str, str, tuple[str, ...]], Estimate]:
    return {_key(c): c.estimate for c in decision.candidates if c.estimate is not None}


def _sans_risk(estimate: Estimate) -> Estimate:
    return dataclasses.replace(
        estimate, extra={k: v for k, v in estimate.extra.items() if k != RISK_TERM}
    )


# ---------------------------------------------------------------- M1


@pytest.mark.parametrize("seed", range(6))
def test_m1_whole_job_risk_is_computed_from_the_calibrated_terms(seed: int) -> None:
    request, snapshot, now = synthetic_fabric(4, split=False, seed=seed)
    shaky = _shaky(snapshot)
    cal = _everything(2.5)
    calibrated = plan(request, snapshot, "cost_model", now, calibration=cal)
    both = plan(request, shaky, "cost_model", now, calibration=cal)
    raw_risked = plan(request, shaky, "cost_model", now)
    base = _rows(calibrated)
    got = _rows(both)
    assert base.keys() == got.keys() and base
    singles = sorted(base.items(), key=lambda kv: (kv[1].total_s, kv[0][1]))
    nodes = {n.node_id: n for n in shaky.nodes}
    for key, estimate in got.items():
        risk = estimate.extra[RISK_TERM]
        # the calibrated terms are exactly the risk-off calibrated ones ...
        assert _sans_risk(estimate) == base[key]
        assert "calibration" in estimate.compute.detail  # really scaled
        # ... risk is p_fail x (calibrated T + calibrated retry overhead) ...
        other = next(e for k, e in singles if k != key)
        retry = retry_overhead_s(other)
        p = p_fail(nodes[key[1]].reliability, shaky.config.risk)  # type: ignore[arg-type]
        assert risk.seconds == pytest.approx(p * (base[key].total_s + retry))
        assert risk.detail["retry_overhead_s"] == pytest.approx(retry, abs=1e-6)
        # ... added after: the total is the calibrated total plus the risk
        assert estimate.total_s == pytest.approx(base[key].total_s + risk.seconds)
        # and it is not the risk of the uncalibrated T
        assert risk.seconds != pytest.approx(
            _rows(raw_risked)[key].extra[RISK_TERM].seconds
        )


def test_m1_split_job_lanes_are_calibrated_before_the_risk_is_added() -> None:
    """Split jobs: each lane's stage + env are calibrated (G2) before any
    candidate is costed; the lane risk is then added on top (the non-risk
    terms are exactly the risk-off calibrated ones). A lane's chunk time
    (items x per-item + overhead) is not a calibrated term for a split job
    (G2: ``no_phase``), so its risk uses it as is."""
    compared = moved = 0
    for seed in range(1500):
        request, snapshot, now = random_case(seed)
        if request.split is None:
            continue
        shaky = _shaky(snapshot)
        cal = _everything(3.0)
        try:
            calibrated = plan(request, snapshot, "cost_model", now, calibration=cal)
        except ValueError:
            continue
        both = plan(request, shaky, "cost_model", now, calibration=cal)
        base = _rows(calibrated)
        got = _rows(both)
        assert base.keys() == got.keys(), seed
        for key, estimate in got.items():
            assert _sans_risk(estimate) == base[key], (seed, key)
            risk = estimate.extra[RISK_TERM]  # every node has reliability
            assert list(estimate.extra)[-1] == RISK_TERM  # always last
            assert estimate.total_s == pytest.approx(base[key].total_s + risk.seconds)
            compared += 1
        raw = _rows(plan(request, snapshot, "cost_model", now))
        moved += any(raw[k].total_s != base[k].total_s for k in base)
    assert compared > 40 and moved > 15, (compared, moved)


# ---------------------------------------------------------------- M2


def test_m2_risk_is_listed_as_never_calibrated() -> None:
    assert NOT_CALIBRATED["risk"] == "derived"
    assert RISK_TERM not in CALIBRATED_TERMS


def test_m2_calibration_never_scales_or_annotates_the_risk_term() -> None:
    request, snapshot, now = synthetic_fabric(4, split=False, seed=2)
    shaky = _shaky(snapshot)
    # a factor table that even names "risk" must not touch it
    cal = dataclasses.replace(
        _everything(4.0),
        terms={
            **_everything(4.0).terms,
            RISK_TERM: TermFactors(global_=Factor(5.0, 50, 5.0)),
        },
    )
    decision = plan(request, shaky, "cost_model", now, calibration=cal)
    for estimate in _rows(decision).values():
        risk = estimate.extra[RISK_TERM]
        assert "calibration" not in risk.detail
        # applying a calibration to a risked estimate leaves the risk alone
        again = calibrate_estimate(estimate, cal, node_id="node_000", family=None)
        assert again.extra[RISK_TERM] == risk


def _explain_text(decision: Decision) -> str:
    rows = [
        CandidateOut.model_validate(row).model_dump(mode="json", exclude_none=True)
        for row in candidates_json(decision)
    ]
    payload = {
        "decisions": [
            {
                "id": 1,
                "attempt_no": 1,
                "attempt_id": None,
                "outcome": "dispatched",
                "policy": decision.policy,
                "chosen_node_id": decision.chosen.node_id if decision.chosen else None,
                "created_at": "2026-10-22T10:00:00Z",
                "snapshot_digest": decision.snapshot_digest,
                "candidates": rows,
                "actual": None,
            }
        ]
    }
    out = io.StringIO()
    console = Console(file=out, width=500, color_system=None)
    for table in decision_tables(payload):
        console.print(table)
    return out.getvalue()


def test_m2_explain_shows_risk_without_a_factor() -> None:
    request, snapshot, now = synthetic_fabric(3, split=False, seed=1)
    decision = plan(
        request, _shaky(snapshot), "cost_model", now, calibration=_everything(2.0)
    )
    text = _explain_text(decision)
    assert "RISK" in text and "calibrated" in text  # the other terms show theirs
    # V0.14 G5 (D300): the risk cell is "<s> (history, p_fail P% (F of T ends
    # in Wd))" - its note, then the closing bracket: never "..., x2.00 calibrated"
    risk_seconds = {
        f"{c.estimate.extra[RISK_TERM].seconds:.2f}s (history, "
        for c in decision.candidates
        if c.estimate is not None
    }
    whole = {
        cell: re.compile(re.escape(cell) + r"p_fail [\d.]+% \([^()]*\)\)")
        for cell in risk_seconds
    }
    for line in text.splitlines():
        for cell, pattern in whole.items():
            if cell in line and "p_fail" in line:
                assert pattern.search(line), line
    assert all(pattern.search(text) for pattern in whole.values())


# ---------------------------------------------------------------- M3

_T0 = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)


def _outcome(decision: Decision, index: int, rng: random.Random) -> DecisionOutcome:
    """The stored decision joined to a ``done`` attempt on its chosen node
    whose phases took ``ratio x`` the un-risked prediction (ratio per node)."""
    assert decision.chosen is not None
    rows = candidates_json(decision)
    chosen = next(r for r in rows if r.get("rank") == 1)
    terms = chosen["estimate"]["terms"]
    node = decision.chosen.node_id
    ratio = 0.5 + (sum(map(ord, node)) % 7) * 0.4
    phases: dict[str, int | None] = {}
    for term, phase in (
        ("transfer", "stage"),
        ("env", "env"),
        ("compute", "run"),
        ("output", "collect"),
    ):
        if term in terms:
            seconds = terms[term]["seconds"] * ratio * rng.uniform(0.9, 1.1)
            phases[phase] = round(seconds * 1000)
    return DecisionOutcome(
        decision_id=index,
        attempt_id=f"att_{index:04d}",
        chosen_node_id=node,
        attempt_node_id=node,
        attempt_state="done",
        attempt_role="lane" if chosen.get("node_set") else "run",
        candidates=rows,
        phases_ms=phases,
        finished_at=_T0 + dt.timedelta(minutes=index),
        family="fam_a" if index % 2 else None,
        fingerprint=None,
    )


def _pairs() -> list[tuple[Decision, Decision]]:
    """(risk off, risk on) decisions over many fabrics, both choosing the same
    candidate (so the attempt measured the same work)."""
    out: list[tuple[Decision, Decision]] = []
    for seed in range(60):
        split = seed % 3 == 0
        request, snapshot, now = synthetic_fabric(4, split=split, seed=seed)
        off = plan(request, snapshot, "cost_model", now)
        on = plan(request, _shaky(snapshot, failures=3), "cost_model", now)
        if off.chosen is None or on.chosen is None:
            continue
        if _key(off.chosen) != _key(on.chosen):
            continue
        if on.chosen.mode == "split":
            continue  # a Mode 3 set never matches (G2); nothing to compare
        assert on.chosen.estimate is not None
        assert RISK_TERM in on.chosen.estimate.extra
        out.append((off, on))
    return out


def test_m3_fitted_factors_are_identical_with_risk_on_or_off() -> None:
    pairs = _pairs()
    assert len(pairs) >= 20, len(pairs)
    runs_off = []
    runs_on = []
    for index, (off, on) in enumerate(pairs):
        # the same seed per index: both attempts took the same time
        got_off = match(_outcome(off, index, random.Random(index)))  # noqa: S311
        got_on = match(_outcome(on, index, random.Random(index)))  # noqa: S311
        assert got_off is not None and got_on is not None
        assert got_off.terms == got_on.terms  # risk never enters a prediction
        runs_off.append(got_off)
        runs_on.append(got_on)
    assert any(r.split for r in runs_on) and any(not r.split for r in runs_on)
    config = CalibrationConfig()
    fit_off, held_off = split_holdout(runs_off)
    fit_on, held_on = split_holdout(runs_on)
    cal_off = fit(fit_off, config)
    cal_on = fit(fit_on, config)
    assert cal_off.factor_count > 0
    assert canonical(calibration_json(cal_on)) == canonical(calibration_json(cal_off))
    assert holdout_error(held_on, cal_on) == holdout_error(held_off, cal_off)
    assert holdout_error(held_on, None) == holdout_error(held_off, None)


def test_m3_a_calibrated_risked_decision_is_matched_on_its_raw_terms() -> None:
    """A decision planned with calibration **and** risk: the prediction read
    back is ``raw_s`` of each term - the same as the uncalibrated risk-off
    decision's term seconds."""
    request, snapshot, now = synthetic_fabric(4, split=False, seed=7)
    plain = plan(request, snapshot, "cost_model", now)
    both = plan(
        request, _shaky(snapshot), "cost_model", now, calibration=_everything(2.0)
    )
    assert plain.chosen is not None and both.chosen is not None
    if plain.chosen.node_id != both.chosen.node_id:
        pytest.skip("risk + calibration moved the pick on this fabric")
    a = match(_outcome(plain, 1, random.Random(1)))  # noqa: S311
    b = match(_outcome(both, 1, random.Random(1)))  # noqa: S311
    assert a is not None and b is not None
    assert [(o.term, o.predicted_s) for o in a.terms] == [
        (o.term, o.predicted_s) for o in b.terms
    ]


# ---------------------------------------------------------------- M4


def test_m4_replay_version_is_2_and_1_still_reads() -> None:
    assert REPLAY_VERSION == 2
    assert frozenset({1, 2}) == READABLE_VERSIONS
    request, snapshot, now = synthetic_fabric(3, split=True, seed=1)
    replay = json.loads(canonical_json(to_replay(request, snapshot, now)))
    assert replay["replay_version"] == 2
    for version in (1, 2):
        replay["replay_version"] = version
        assert from_replay(replay)[0] == request
    for bad in (0, 3, True, "2", 2.0, None):
        replay["replay_version"] = bad
        with pytest.raises(ReplayError):
            from_replay(replay)
        with pytest.raises(ReplayError):
            calibration_from_replay(replay)


def _random_calibration(
    rng: random.Random, request: PlanRequest, snapshot: ClusterSnapshot
) -> Calibration:
    nodes = [n.node_id for n in snapshot.nodes] + ["node_not_in_fabric"]

    def factor() -> Factor:
        value = rng.choice([1.0, rng.uniform(0.2, 5.0), 0.2, 5.0])
        return Factor(round(value, 6), rng.randint(1, 200), round(value, 6))

    terms = {
        term: TermFactors(
            global_=factor() if rng.random() < 0.7 else None,
            node={n: factor() for n in nodes if rng.random() < 0.5},
            node_family={n: {"fam_a": factor()} for n in nodes if rng.random() < 0.3},
        )
        for term in CALIBRATED_TERMS
        if rng.random() < 0.85
    }
    families = {}
    if request.fingerprint is not None and rng.random() < 0.6:
        families[request.fingerprint] = "fam_a"
    return Calibration(version=rng.randint(1, 10**6), terms=terms, families=families)


def _random_g3(
    rng: random.Random, request: PlanRequest, snapshot: ClusterSnapshot
) -> tuple[PlanRequest, ClusterSnapshot]:
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
    return (
        dataclasses.replace(request, objective=rng.choice((None, *OBJECTIVES))),
        dataclasses.replace(snapshot, nodes=nodes, config=config),
    )


@pytest.mark.parametrize("block", range(6))
def test_m4_calibrated_risked_decisions_replay_exactly_under_v1_and_v2(
    block: int,
) -> None:
    """Property test: random fabric x random calibration x random reliability
    (and the other G3 inputs); for every policy the replay reproduces the
    dispatcher's decision field for field, read as version 2 and as 1."""
    checked = both = 0
    for seed in range(block, 240, 6):
        rng = random.Random(77_000 + seed)  # noqa: S311 - seeded test data
        base_request, base_snapshot, now = random_case(seed)
        request, snapshot = _random_g3(rng, base_request, base_snapshot)
        cal = _random_calibration(rng, request, snapshot)
        view = project(request, snapshot)
        job_cal = project_calibration(cal, request, view)
        stored = json.loads(
            canonical_json(to_replay(request, snapshot, now, calibration=job_cal))
        )
        assert stored["replay_version"] == 2
        for version in (2, 1):
            replay = {**stored, "replay_version": version}
            got_request, got_snapshot, got_now = from_replay(replay)
            got_cal = calibration_from_replay(replay)
            assert got_cal == job_cal
            for policy in POLICIES:
                try:
                    want = plan(request, view, policy, now, calibration=job_cal)
                except ValueError:
                    continue
                got = plan(
                    got_request, got_snapshot, policy, got_now, calibration=got_cal
                )
                assert fields_text(got) == fields_text(want), (seed, policy, version)
                assert decision_view(got) == decision_view(want)
                checked += 1
                rows = decision_view(got)["candidates"]  # canonical JSON text
                both += RISK_TERM in rows and '"calibration"' in rows
    assert checked > 300 and both > 10, (checked, both)


def test_m4_absent_keys_are_uncalibrated_and_pre_g3_under_both_versions() -> None:
    request, snapshot, now = synthetic_fabric(5, split=True, seed=4)
    request, snapshot = _random_g3(random.Random(3), request, snapshot)  # noqa: S311
    full = json.loads(
        canonical_json(to_replay(request, snapshot, now, calibration=_everything(2.0)))
    )
    bare = json.loads(json.dumps(full))
    bare.pop("calibration")
    bare["request"].pop("objective")
    for node in bare["snapshot"]["nodes"]:
        node.pop("reliability")
    for key in (
        "risk",
        "prefer_local_margin",
        "min_useful_bw_bytes_s",
        "min_useful_bytes",
        "prune_dominated",
    ):
        bare["snapshot"]["config"].pop(key)
    views = []
    for version in (1, 2):
        replay = {**bare, "replay_version": version}
        got_request, got_snapshot, got_now = from_replay(replay)
        assert calibration_from_replay(replay) is None
        assert got_request.objective is None
        assert all(n.reliability is None for n in got_snapshot.nodes)
        assert got_snapshot.config.prune_dominated is False
        assert got_snapshot.config.min_useful_bw_bytes_s == 0.0
        decision = plan(got_request, got_snapshot, "cost_model", got_now)
        text = json.dumps(decision_view(decision)["candidates"])
        assert RISK_TERM not in text and '"calibration"' not in text
        views.append(decision_view(decision))
    assert views[0] == views[1]


def test_m4_golden_corpus_is_byte_identical_under_either_version_label() -> None:
    """The 397 G1 entries are v1 replays without any G2/G3 key: read as 1 or
    relabelled 2 they plan to exactly the recorded view (unset inputs stay out
    of the digest and of ``fields_text``)."""
    with gzip.open(GOLDEN, "rt", encoding="utf-8") as fh:
        entries: list[dict[str, Any]] = json.load(fh)["entries"]
    assert len(entries) == 397
    compared = 0
    for entry in entries:
        if entry["error"] is not None:
            continue
        assert entry["replay"]["replay_version"] == 1
        assert "calibration" not in entry["replay"]
        for version in (1, 2):
            replay = {**entry["replay"], "replay_version": version}
            request, snapshot, now = from_replay(replay)
            got = plan(request, snapshot, entry["policy"], now)
            assert decision_view(got) == entry["expected"], (entry["source"], version)
        compared += 1
    assert compared > 350, compared
