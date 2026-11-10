"""V1.0 G9-E (D438): an assumed per-item rate never justifies a split.

Lab L1 C10.10-e (V0.10 part D "don't split"): job ``hf`` - 60 items, about
0.8 s of real work in total - was split over 3 nodes. No run of the workload
was recorded, so every lane's rate was ``assumed`` (``default_compute_s`` for
the whole job, scaled by hardware score): ~66 s whole vs ~43 s split, and the
margin test passed on numbers nobody measured.

Pure planner tests: the hf case runs whole; once a run is recorded (history)
the cost model decides on measured numbers - the tiny job still runs whole, a
long one splits; the switch is configurable, its replay key is optional (an
older replay decodes to the old behaviour) and the explain texts say why.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from meshforge.common.explain_text import margin_sentence
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    SplitConfig,
    SplitInfo,
    TermSource,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import (
    canonical_json,
    from_replay,
    to_replay,
)
from meshforge.controller.settings import SplitSettings

pytestmark = pytest.mark.unit

EXEC = frozenset({"exec", "exec-subprocess", "exec-split"})
PROJECT = "sha256:" + "a" * 64
MB = 1_000_000
L1, L2, L3, L4 = "node_l1", "node_l2", "node_l3", "node_l4"


def _node(node_id: str, score: float) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=EXEC,
        cpu_threads=8,
        cpu_slots=1,
        cpu_score=score,
    )


def _fabric(config: PlannerConfig | None = None) -> ClusterSnapshot:
    """The lab's four nodes: L1 the strongest, the others 0.57-0.78 of it."""
    nodes = (_node(L1, 1.0), _node(L2, 0.78), _node(L3, 0.57), _node(L4, 0.59))
    return ClusterSnapshot(nodes=nodes, config=config or PlannerConfig())


def _hf(history: History | None = None, items: int = 60) -> PlanRequest:
    return PlanRequest(
        job_id="job_01J0000000000000000000000",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        trees=(TreeNeed(PROJECT, 60 * MB),),
        submitter_node_id=L1,
        seeded=True,
        fingerprint="fp-hf",
        history=history or History(),
        split=SplitInfo(items=items),
    )


def _whole_runs(**seconds: float) -> History:
    """Recorded whole runs (``run`` phase medians) of the workload."""
    return History(by_node_s=dict(seconds), samples_by_node=dict.fromkeys(seconds, 1))


def _old_config() -> PlannerConfig:
    return PlannerConfig(split=SplitConfig(require_measured_rates=False))


# ------------------------------------------------------------- the lab case


def test_hf_with_only_assumed_rates_runs_whole() -> None:
    decision = plan(_hf(), _fabric(), "cost_model")
    assert decision.chosen is not None
    assert decision.chosen.mode != "split"
    assert decision.split is not None and not decision.split.split
    test = decision.split.margin
    assert not test.passed
    assert test.blocked == "rates_assumed"
    # on time alone the set would have won (the lab's 43 s vs 66 s)
    assert test.set_s is not None and test.threshold_s is not None
    assert test.set_s <= test.threshold_s
    rows = [r for r in candidates_json(decision) if r["mode"] == "split"]
    assert rows and all(not r["margin_test"]["passed"] for r in rows)
    assert all(r["margin_test"]["blocked"] == "rates_assumed" for r in rows)


def test_hf_before_the_fix_split_on_assumed_rates() -> None:
    """The old behaviour (``require_measured_rates = false``) is the lab bug."""
    decision = plan(_hf(), _fabric(_old_config()), "cost_model")
    assert decision.chosen is not None and decision.chosen.mode == "split"
    assert decision.split is not None and decision.split.margin.passed
    assert decision.split.margin.blocked is None
    rows = [r for r in candidates_json(decision) if r["mode"] == "split"]
    assert all("blocked" not in r["margin_test"] for r in rows)


def test_measured_tiny_job_still_runs_whole() -> None:
    """Once hf ran (0.8 s whole on L1) the cost model knows splitting it
    costs more in per-chunk overhead than it saves."""
    decision = plan(_hf(_whole_runs(node_l1=0.8)), _fabric(), "cost_model")
    assert decision.split is not None and not decision.split.split
    test = decision.split.margin
    assert not test.passed and test.blocked is None
    assert test.set_s is not None and test.threshold_s is not None
    assert test.set_s > test.threshold_s


def test_measured_long_job_splits() -> None:
    """Measured evidence of a long job lets the set win as before."""
    history = _whole_runs(node_l1=600.0)
    decision = plan(_hf(history, items=600), _fabric(), "cost_model")
    assert decision.chosen is not None and decision.chosen.mode == "split"
    assert decision.split is not None and decision.split.split
    assert decision.split.margin.passed and decision.split.margin.blocked is None


def test_forced_benchmark_set_still_splits_on_assumed_rates() -> None:
    """A campaign's forced ``mode3`` run (D271) runs the set whatever the test
    says - the test (and its block) is still recorded."""
    request = _hf()
    forced = PlanRequest(
        **{
            **{f: getattr(request, f) for f in request.__dataclass_fields__},
            "node_set": (L1, L2, L3),
            "benchmark": True,
        }
    )
    decision = plan(forced, _fabric(), "cost_model")
    assert decision.split is not None and decision.split.split
    assert decision.split.margin.blocked == "rates_assumed"


@pytest.mark.parametrize("objective", ["min_network", "prefer_local"])
def test_objectives_never_split_on_assumed_rates(objective: str) -> None:
    request = _hf()
    request = PlanRequest(
        **{
            **{f: getattr(request, f) for f in request.__dataclass_fields__},
            "objective": objective,
        }
    )
    decision = plan(request, _fabric(), "cost_model")
    assert decision.split is not None and not decision.split.split


# ------------------------------------------------------------ configuration


def test_settings_default_requires_measured_rates() -> None:
    assert SplitSettings().require_measured_rates is True
    assert SplitConfig().require_measured_rates is True


def test_replay_key_is_written_only_when_on_and_absent_decodes_off() -> None:
    request = _hf()
    on = to_replay(request, _fabric(), None)
    assert on["snapshot"]["config"]["split"]["require_measured_rates"] is True
    off = to_replay(request, _fabric(_old_config()), None)
    assert "require_measured_rates" not in off["snapshot"]["config"]["split"]
    # a replay stored before G9-E has no key: it re-plans the old way
    _, snapshot, _ = from_replay(json.loads(canonical_json(off)))
    assert snapshot.config.split.require_measured_rates is False
    _, snapshot_on, _ = from_replay(json.loads(canonical_json(on)))
    assert snapshot_on.config.split.require_measured_rates is True


# ------------------------------------------------------------------ explain


def test_explain_summary_says_rates_were_assumed() -> None:
    decision = plan(_hf(), _fabric(), "cost_model")
    text = margin_sentence(candidates_json(decision))
    assert text is not None and text.startswith("not split")
    assert "assumed" in text


def test_explain_margin_cell_shows_the_block() -> None:
    from meshforge.cli.explain_commands import _margin_cell

    decision = plan(_hf(), _fabric(), "cost_model")
    row = next(r for r in candidates_json(decision) if r["mode"] == "split")
    cell = _margin_cell(row)
    assert cell.startswith("fail: ")
    assert " <= " in cell and cell.endswith(", blocked: rates_assumed")


# ------------------------------------------- r1: the manifest's item_seconds


def _hinted(item_seconds: float, items: int = 60, **kw: Any) -> PlanRequest:
    request = _hf(kw.pop("history", None), items=items)
    return replace(
        request,
        split=replace(
            request.split or SplitInfo(items=items), item_seconds=item_seconds
        ),
    )


def test_declared_long_job_splits_on_its_first_run() -> None:
    """Lead r1: a long GPU-style job declares 10 s per item and splits on
    run 1 - no history, no switch."""
    decision = plan(_hinted(10.0, items=600), _fabric(), "cost_model")
    assert decision.chosen is not None and decision.chosen.mode == "split"
    assert decision.split is not None and decision.split.split
    test = decision.split.margin
    assert test.passed and test.blocked is None
    singles = [r for r in candidates_json(decision) if r["mode"] != "split"]
    assert {r["rate_source"] for r in singles if r.get("rate_source")} == {"declared"}
    # the strongest node runs at the declared rate, a weaker one slower
    by_node = {
        r["node_id"]: r["rate_items_s"] for r in singles if r.get("rate_items_s")
    }
    assert by_node[L1] == pytest.approx(0.1)
    assert by_node[L3] == pytest.approx(0.1 * 0.57)


def test_declared_tiny_job_still_runs_whole() -> None:
    """A declared rate is evidence, not a licence: hf's real 0.013 s per item
    stays whole on its own per-chunk overhead."""
    decision = plan(_hinted(0.8 / 60), _fabric(), "cost_model")
    assert decision.split is not None and not decision.split.split
    assert decision.split.margin.blocked is None
    assert not decision.split.margin.passed


def test_recorded_history_wins_over_the_declared_rate() -> None:
    hinted = _hinted(10.0, items=600, history=_whole_runs(node_l1=0.8))
    decision = plan(hinted, _fabric(), "cost_model")
    assert decision.split is not None and not decision.split.split
    singles = [r for r in candidates_json(decision) if r["mode"] != "split"]
    assert "declared" not in {r.get("rate_source") for r in singles}


def test_declared_is_weaker_than_scaled_and_stronger_than_assumed() -> None:
    from meshforge.controller.planner.estimator import weakest

    D, A, S = TermSource.DECLARED, TermSource.ASSUMED, TermSource.SCALED
    assert weakest(D, S) is D and weakest(D, A) is A


def test_item_seconds_replay_key_only_when_given() -> None:
    plain = to_replay(_hf(), _fabric(), None)
    assert "item_seconds" not in plain["request"]["split"]
    hinted = to_replay(_hinted(2.5), _fabric(), None)
    assert hinted["request"]["split"]["item_seconds"] == 2.5
    request, _, _ = from_replay(json.loads(canonical_json(hinted)))
    assert request.split is not None and request.split.item_seconds == 2.5
    request, _, _ = from_replay(json.loads(canonical_json(plain)))
    assert request.split is not None and request.split.item_seconds is None
    # an absent estimate leaves the digest of every decision before it
    assert (
        plan(_hf(), _fabric(), "cost_model").snapshot_digest
        == plan(
            replace(_hf(), split=replace(_hf().split or SplitInfo(items=60))),
            _fabric(),
            "cost_model",
        ).snapshot_digest
    )
    assert (
        plan(_hinted(2.5), _fabric(), "cost_model").snapshot_digest
        != plan(_hf(), _fabric(), "cost_model").snapshot_digest
    )


def test_manifest_item_seconds_is_validated_and_hash_stable() -> None:
    from pydantic import ValidationError

    from meshforge.common.manifest import MAX_SPLIT_ITEM_SECONDS, Manifest

    base: dict[str, Any] = {
        "name": "hf",
        "entrypoint": "python main.py {items_file} {out}",
        "split": {"over": {"range": {"stop": 60}}},
    }
    plain = Manifest.model_validate(base)
    assert plain.split is not None and plain.split.item_seconds is None
    assert "item_seconds" not in plain.canonical_json()
    for good in (3, 0.25, MAX_SPLIT_ITEM_SECONDS):
        m = Manifest.model_validate(
            {**base, "split": {**base["split"], "item_seconds": good}}
        )
        assert m.split is not None and m.split.item_seconds == float(good)
        assert '"item_seconds"' in m.canonical_json()
    for bad in (
        0,
        -1,
        "2.5",
        True,
        float("inf"),
        float("nan"),
        MAX_SPLIT_ITEM_SECONDS * 2,
    ):
        with pytest.raises(ValidationError):
            Manifest.model_validate(
                {**base, "split": {**base["split"], "item_seconds": bad}}
            )
