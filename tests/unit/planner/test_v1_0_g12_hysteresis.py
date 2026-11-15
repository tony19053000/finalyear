"""V1.0 G12 (D460): mode hysteresis - near-tie decisions keep the previous
decision's mode instead of flapping between local / Mode 1 / Mode 3.

Covered: keep vs switch at the margin boundary; margin 0 disables; no
previous decision = the planner without hysteresis; pins, ``--exclude-node``,
forced sets, rule policies, other objectives and refusals are never
overridden; a split job keeps Mode 3 / keeps a single and never keeps a set
blocked by the measured-rates rule; determinism and the replay round trip;
the decision JSON + explain sentence; the setting's validation.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.common.explain_text import kept_sentence, margin_sentence
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitConfig,
    SplitInfo,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.hysteresis import keep_previous
from meshforge.controller.planner.plan import snapshot_digest
from meshforge.controller.planner.replay import from_replay, to_replay
from meshforge.controller.planner.types import Decision, PlannerConfig
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

EXEC = frozenset({"exec", "exec-subprocess", "exec-split"})
PROJECT = "sha256:" + "a" * 64
ENV = "e" * 64
NOW = dt.datetime(2026, 11, 12, 12, 0, tzinfo=dt.UTC)


def _node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_threads": 8,
        "cpu_slots": 1,
        "cached": frozenset({PROJECT}),
        "cached_envs": {ENV: 1},
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def _snap(
    *nodes: NodeSnapshot, margin: float = 0.10, split: SplitConfig | None = None
) -> ClusterSnapshot:
    return ClusterSnapshot(
        nodes=tuple(sorted(nodes, key=lambda n: n.node_id)),
        config=PlannerConfig(mode_switch_margin=margin, split=split or SplitConfig()),
    )


def _whole(times: dict[str, float], **kw: Any) -> PlanRequest:
    """A job that runs whole; ``times`` = its measured run seconds per node
    (n1 is the submitter: its candidate is Local)."""
    return PlanRequest(
        job_id="job_01J0000000000000000000000",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key=ENV,
        trees=(TreeNeed(PROJECT, 1000),),
        submitter_node_id="n1",
        seeded=True,
        history=History(
            by_node_s=times,
            samples_by_node=dict.fromkeys(times, 5),
            env_hash_by_node=dict.fromkeys(times, ENV),
        ),
        **kw,
    )


def _split(rates: dict[str, float], items: int = 200, **kw: Any) -> PlanRequest:
    return PlanRequest(
        job_id="job_01J0000000000000000000001",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key=ENV,
        trees=(TreeNeed(PROJECT, 1000),),
        submitter_node_id="n1",
        seeded=True,
        history=History(
            item_rate_by_node=rates,
            item_rate_samples_by_node=dict.fromkeys(rates, 20),
            env_hash_by_node=dict.fromkeys(rates, ENV),
            chunk_overhead_s=0.5,
            chunk_overhead_samples=20,
        ),
        split=SplitInfo(items=items),
        **kw,
    )


def _chosen(decision: Decision) -> tuple[str, str]:
    assert decision.chosen is not None
    return decision.chosen.mode, decision.chosen.node_id


def _rows(decision: Decision) -> list[dict[str, Any]]:
    return candidates_json(decision)


# ------------------------------------------------------------------ whole jobs

THREE = (_node("n1"), _node("n2"), _node("n3"))


def test_previous_local_is_kept_when_offload_is_within_the_margin() -> None:
    snapshot = _snap(*THREE)
    request = _whole({"n1": 100.0, "n2": 95.0, "n3": 120.0})
    assert _chosen(plan(request, snapshot, "cost_model", now=NOW)) == (
        "offload",
        "n2",
    )
    kept = plan(
        dataclasses.replace(request, previous_mode="local"),
        snapshot,
        "cost_model",
        now=NOW,
    )
    assert _chosen(kept) == ("local", "n1")
    assert kept.chosen is not None and kept.chosen.kept is not None
    rows = _rows(kept)
    assert rows[0]["rank"] == 1 and rows[0]["mode"] == "local"
    assert rows[0]["kept_mode"] == {
        "previous_mode": "local",
        "best_mode": "offload",
        "best_s": 95.0,
        "kept_s": 100.0,
        "margin": 0.1,
    }
    assert [r["rank"] for r in rows] == [1, 2, 3]
    assert [r["node_id"] for r in rows] == ["n1", "n2", "n3"]
    assert all("kept_mode" not in r for r in rows[1:])
    sentence = margin_sentence(rows)
    assert sentence == (
        "kept previous mode local (new best mode1 95.00s within 10% of 100.00s)"
    )


def test_previous_offload_keeps_its_best_node_not_the_old_one() -> None:
    """Hysteresis is between modes: the kept Mode 1 candidate is the best
    offload now, whichever node the previous decision used."""
    snapshot = _snap(*THREE)
    request = _whole({"n1": 95.0, "n2": 104.0, "n3": 100.0}, previous_mode="offload")
    decision = plan(request, snapshot, "cost_model", now=NOW)
    assert _chosen(decision) == ("offload", "n3")


@pytest.mark.parametrize(
    ("best", "kept"),
    [
        (90.0, False),  # exactly 10 % faster: switch
        (89.999, False),
        (90.001, True),  # not quite 10 % faster: keep
        (99.0, True),
    ],
)
def test_the_margin_boundary(best: float, kept: bool) -> None:
    snapshot = _snap(*THREE)
    request = _whole({"n1": 100.0, "n2": best, "n3": 200.0}, previous_mode="local")
    decision = plan(request, snapshot, "cost_model", now=NOW)
    assert _chosen(decision)[0] == ("local" if kept else "offload")
    assert keep_previous(best, 100.0, 0.10) is kept


def test_margin_zero_disables_hysteresis() -> None:
    request = _whole({"n1": 100.0, "n2": 99.0, "n3": 120.0}, previous_mode="local")
    off = plan(request, _snap(*THREE, margin=0.0), "cost_model", now=NOW)
    assert _chosen(off) == ("offload", "n2")
    assert all("kept_mode" not in r for r in _rows(off))


def test_first_decision_is_unaffected() -> None:
    """No previous decision: exactly the planner without hysteresis, digest
    included (the margin is in the config, so the digest differs only by it)."""
    request = _whole({"n1": 100.0, "n2": 99.0, "n3": 120.0})
    with_margin = plan(request, _snap(*THREE), "cost_model", now=NOW)
    without = plan(request, _snap(*THREE, margin=0.0), "cost_model", now=NOW)
    assert _rows(with_margin) == _rows(without)
    assert with_margin.reason_key == without.reason_key


@pytest.mark.parametrize(
    "change",
    [
        {"pinned_node_id": "n2"},
        {"exclude_nodes": ("n3",)},
        {"objective": "min_network"},
        {"objective": "prefer_local"},
    ],
)
def test_user_choices_are_never_overridden(change: dict[str, Any]) -> None:
    snapshot = _snap(*THREE)
    request = _whole({"n1": 100.0, "n2": 95.0, "n3": 120.0}, **change)
    plain = plan(request, snapshot, "cost_model", now=NOW)
    kept = plan(
        dataclasses.replace(request, previous_mode="local"),
        snapshot,
        "cost_model",
        now=NOW,
    )
    assert _rows(kept) == _rows(plain)


@pytest.mark.parametrize("policy", ["least_loaded", "strongest_suitable", "random"])
def test_rule_policies_are_never_overridden(policy: str) -> None:
    snapshot = _snap(*THREE)
    request = _whole({"n1": 100.0, "n2": 95.0, "n3": 120.0})
    plain = plan(request, snapshot, policy, now=NOW)
    kept = plan(
        dataclasses.replace(request, previous_mode="local"), snapshot, policy, now=NOW
    )
    assert _rows(kept) == _rows(plain)


def test_a_refused_previous_mode_is_not_kept() -> None:
    """Local is refused (its node has no free slot): the offload stands."""
    busy = _node("n1", used_cpu_slots=1)
    snapshot = _snap(busy, _node("n2"), _node("n3"))
    request = _whole({"n1": 100.0, "n2": 95.0, "n3": 120.0}, previous_mode="local")
    decision = plan(request, snapshot, "cost_model", now=NOW)
    assert _chosen(decision) == ("offload", "n2")
    local = next(c for c in decision.candidates if c.mode == "local")
    assert not local.feasible


def test_deterministic_and_replays_byte_identical() -> None:
    snapshot = _snap(*THREE)
    request = _whole({"n1": 100.0, "n2": 95.0, "n3": 120.0}, previous_mode="local")
    first = plan(request, snapshot, "cost_model", now=NOW)
    second = plan(request, snapshot, "cost_model", now=NOW)
    assert _rows(first) == _rows(second)
    assert first.snapshot_digest == second.snapshot_digest
    replay = to_replay(request, snapshot, NOW)
    assert replay["request"]["previous_mode"] == "local"
    assert replay["snapshot"]["config"]["mode_switch_margin"] == 0.1
    back, snap, now = from_replay(replay)
    again = plan(back, snap, "cost_model", now=now)
    assert _rows(again) == _rows(first)
    assert again.snapshot_digest == first.snapshot_digest


def test_an_older_replay_has_no_hysteresis_and_keeps_its_digest() -> None:
    snapshot = _snap(*THREE, margin=0.0)
    request = _whole({"n1": 100.0, "n2": 95.0, "n3": 120.0})
    replay = to_replay(request, snapshot, NOW)
    assert "previous_mode" not in replay["request"]
    assert "mode_switch_margin" not in replay["snapshot"]["config"]
    back, snap, _ = from_replay(replay)
    assert back.previous_mode is None
    assert snap.config.mode_switch_margin == 0.0
    assert snapshot_digest(back, snap) == snapshot_digest(request, snapshot)


def test_a_corrupt_previous_mode_is_refused() -> None:
    from meshforge.controller.planner.replay import ReplayError

    replay = to_replay(_whole({"n1": 1.0}), _snap(_node("n1")), NOW)
    replay["request"]["previous_mode"] = "mode9"
    with pytest.raises(ReplayError):
        from_replay(replay)


# ------------------------------------------------------------------ split jobs


def _split_totals(decision: Decision) -> tuple[float, float]:
    """(best set total, best single total) of a split decision."""
    rows = [r for r in _rows(decision) if r.get("estimate")]
    sets = [r["estimate"]["total_s"] for r in rows if r["mode"] == "split"]
    singles = [r["estimate"]["total_s"] for r in rows if r["mode"] != "split"]
    return min(sets), min(singles)


def _near_tie_split() -> tuple[PlanRequest, ClusterSnapshot]:
    """A split job whose best set misses the split margin (25 %) but is
    within the hysteresis margin of the best single."""
    snapshot = _snap(_node("n1"), _node("n2"), margin=0.5)
    return _split({"n1": 1.0, "n2": 0.2}), snapshot


def test_split_job_keeps_mode3_on_a_near_tie() -> None:
    request, snapshot = _near_tie_split()
    plain = plan(request, snapshot, "cost_model", now=NOW)
    assert _chosen(plain)[0] == "local"
    set_s, single_s = _split_totals(plain)
    assert single_s < set_s < single_s / 0.5  # within the 50 % margin
    kept = plan(
        dataclasses.replace(request, previous_mode="split"),
        snapshot,
        "cost_model",
        now=NOW,
    )
    assert _chosen(kept)[0] == "split"
    assert kept.split is not None and kept.split.split
    assert kept.split.margin.passed is False  # the margin test is still reported
    sentence = margin_sentence(_rows(kept))
    assert sentence is not None and sentence.startswith("kept previous mode mode3")


def test_split_job_keeps_a_single_when_the_set_is_within_the_margin() -> None:
    snapshot = _snap(_node("n1"), _node("n2"), margin=0.5)
    request = _split({"n1": 1.0, "n2": 1.0})
    plain = plan(request, snapshot, "cost_model", now=NOW)
    assert _chosen(plain)[0] == "split"
    set_s, single_s = _split_totals(plain)
    assert set_s > 0.5 * single_s  # the set is not 50 % faster
    kept = plan(
        dataclasses.replace(request, previous_mode="local"),
        snapshot,
        "cost_model",
        now=NOW,
    )
    assert _chosen(kept) == ("local", "n1")
    assert kept.split is not None and not kept.split.split
    assert len(kept.split.lanes) == 1
    # a clearly faster set still wins with the default 10 %
    default = _snap(_node("n1"), _node("n2"))
    switched = plan(
        dataclasses.replace(request, previous_mode="local"),
        default,
        "cost_model",
        now=NOW,
    )
    assert _chosen(switched)[0] == "split"


def test_a_set_blocked_by_the_measured_rates_rule_is_never_kept() -> None:
    snapshot = _snap(
        _node("n1"),
        _node("n2"),
        margin=0.5,
        split=SplitConfig(require_measured_rates=True),
    )
    request = dataclasses.replace(
        _split({}), previous_mode="split"
    )  # no rate recorded anywhere: assumed
    decision = plan(request, snapshot, "cost_model", now=NOW)
    assert _chosen(decision)[0] != "split"
    assert all("kept_mode" not in r for r in _rows(decision))


def test_a_pinned_split_job_is_never_overridden() -> None:
    request, snapshot = _near_tie_split()
    pinned = dataclasses.replace(request, pinned_node_id="n1", previous_mode="split")
    decision = plan(pinned, snapshot, "cost_model", now=NOW)
    assert _chosen(decision) == ("local", "n1")


def test_split_job_hysteresis_replays_byte_identical() -> None:
    request, snapshot = _near_tie_split()
    request = dataclasses.replace(request, previous_mode="split")
    first = plan(request, snapshot, "cost_model", now=NOW)
    back, snap, now = from_replay(to_replay(request, snapshot, NOW))
    again = plan(back, snap, "cost_model", now=now)
    assert _rows(again) == _rows(first)
    assert first.split == again.split


# ------------------------------------------------------------------ explain


def test_kept_sentence_tolerates_garbage() -> None:
    assert kept_sentence(None) is None
    assert kept_sentence([{"rank": 1}]) is None
    assert kept_sentence([{"rank": 1, "kept_mode": "x"}]) is None
    odd = kept_sentence([{"rank": 1, "kept_mode": {"previous_mode": 3}}])
    assert odd == "kept previous mode - (new best - - within - of -)"
    # a whole job without a kept mode has no sentence (as before G12)
    assert margin_sentence([{"rank": 1, "mode": "local", "node_id": "a"}]) is None


# ------------------------------------------------------------------ setting


def test_setting_default_and_bounds() -> None:
    assert PlannerSettings().mode_switch_margin == 0.10
    assert PlannerSettings(mode_switch_margin=0).mode_switch_margin == 0.0
    assert PlannerSettings(mode_switch_margin=0.5).mode_switch_margin == 0.5
    for bad in (-0.01, 0.51, 1.0):
        with pytest.raises(ValidationError):
            PlannerSettings(mode_switch_margin=bad)


def test_active_refuses_ml_forced_sets_and_unknown_modes() -> None:
    from meshforge.controller.planner.hysteresis import MAX_MODE_SWITCH_MARGIN, active

    base = _whole({"n1": 1.0}, previous_mode="local")
    assert active(base, 0.1, "cost_model", ml=False) == 0.1
    assert active(base, 0.1, "cost_model", ml=True) is None  # ML-planned decision
    assert active(base, 0.0, "cost_model", ml=False) is None
    assert active(base, 0.9, "cost_model", ml=False) == MAX_MODE_SWITCH_MARGIN
    forced = dataclasses.replace(base, node_set=("n1", "n2"))
    assert active(forced, 0.1, "cost_model", ml=False) is None
    first = dataclasses.replace(base, previous_mode=None)
    assert active(first, 0.1, "cost_model", ml=False) is None
