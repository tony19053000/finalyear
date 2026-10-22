"""V0.14 G5 (D300): the shared explain sentences (``common/explain_text``).

The controller puts these strings in the decisions answer and ``compute
explain`` prints them, so each case of the margin test (D265), the objective
case (D295), pruned nodes (D294) and the risk note (D296) is pinned here once.
"""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.common.explain_text import format_seconds, margin_sentence, term_note

pytestmark = pytest.mark.unit


def _test(**over: Any) -> dict[str, Any]:
    return {
        "set_s": 41.0,
        "best_single_s": 52.0,
        "best_single_node_id": "node_a",
        "margin": 0.1,
        "threshold_s": 46.8,
        "passed": True,
        "forced": None,
    } | over


def _set(rank: int | None, **over: Any) -> dict[str, Any]:
    return {
        "node_id": "node_a",
        "mode": "split",
        "rank": rank,
        "node_set": ["node_a", "node_b", "node_c"],
        "margin_test": _test(),
    } | over


def _single(rank: int | None) -> dict[str, Any]:
    return {
        "node_id": "node_a",
        "mode": "offload",
        "rank": rank,
        "node_set": ["node_a"],
    }


def test_a_chosen_set_says_split_with_the_threshold() -> None:
    assert margin_sentence([_set(1), _single(2)]) == (
        "split over 3 nodes: 41.00s <= (1 - 0.1) x 52.00s (best single node node_a), "
        "needs <= 46.80s"
    )


def test_a_set_above_the_threshold_says_not_split_and_why() -> None:
    failing = _set(2, margin_test=_test(set_s=50.0, passed=False))
    assert margin_sentence([_single(1), failing]) == (
        "not split: the best set 50.00s is above 46.80s = (1 - 0.1) x 52.00s "
        "(best single node node_a)"
    )


def test_a_passing_set_beaten_by_the_objective_says_so() -> None:
    assert margin_sentence([_single(1), _set(2)]) == (
        "not split: the best set 41.00s passed the margin test, but the job's "
        "objective ranked one node first"
    )


def test_a_refused_set_gives_its_reason() -> None:
    refused = _set(
        None,
        reject_code="split_too_small",
        reject_text="3 item(s) are fewer than 2 x split.min_nodes (2)",
        margin_test=None,
    )
    assert margin_sentence([_single(1), refused]) == (
        "not split: 3 item(s) are fewer than 2 x split.min_nodes (2)"
    )


def test_a_pinned_split_job_says_pinned() -> None:
    assert margin_sentence([_single(1)]) == "not split: the job is pinned to one node"


def test_pruned_nodes_are_appended() -> None:
    rows = [_set(1, pruned=[{"node_id": "node_z", "by": "node_a"}]), _single(2)]
    sentence = margin_sentence(rows)
    assert sentence is not None
    assert sentence.endswith(
        "; sets that only add an idle node were not costed: node_z (by node_a) - "
        "the faster node would finish every item before it could start"
    )


def test_a_whole_job_has_no_margin_sentence() -> None:
    assert margin_sentence([{"node_id": "a", "mode": "local", "rank": 1}]) is None
    assert margin_sentence([]) is None


@pytest.mark.parametrize("junk", [None, "x", 3, [1, None, "s"], {"a": 1}])
def test_margin_sentence_survives_junk(junk: Any) -> None:
    assert margin_sentence(junk) is None
    hostile = [_set(1, margin_test="x", pruned=[1, "y"]), _single(2)]
    assert margin_sentence(hostile) == (
        "split over 3 nodes: - <= (1 - -) x - (best single node None), needs <= -"
    )


def test_term_note_names_bandwidth_cached_floor_and_stale() -> None:
    assert term_note({"detail": {"bw_from": "controller"}}) == "from controller"
    assert (
        term_note({"detail": {"bw_from": "controller", "last_transfer_age_s": 7200}})
        == "from controller (last transfer 2h ago)"
    )
    assert term_note({"detail": {"env_cached": True}}) == "cached"
    assert term_note({"detail": {"bw_from": "node_0000AAAA", "bw_clamped": True}}) == (
        "from 0000AAAA, at the bandwidth floor"
    )
    assert (
        term_note({"detail": {"stale_links_ignored": ["a", "b"]}})
        == "2 stale probe(s) ignored"
    )
    assert term_note({"detail": {}}) is None
    assert term_note({"seconds": 1.0}) is None
    assert term_note(None) is None


def test_the_risk_note_names_p_fail_and_its_evidence() -> None:
    single = {"detail": {"p_fail": 0.16, "failures": 3, "total": 20, "window_days": 7}}
    assert term_note(single, name="risk") == "p_fail 16.0% (3 of 20 ends in 7d)"
    prior = {"detail": {"p_fail": 0.05, "failures": 0, "total": 0, "window_days": 7}}
    assert term_note(prior, name="risk") == "p_fail 5.0% (prior)"
    lanes = {
        "detail": {
            "lanes": [
                {"node_id": f"node_0000000{i}", "p_fail": 0.05 * (i + 1)}
                for i in range(5)
            ]
        }
    }
    assert term_note(lanes, name="risk") == (
        "p_fail 00000000 5.0%, 00000001 10.0%, 00000002 15.0% + 2 more"
    )
    # only the risk term reads p_fail
    assert term_note(single, name="compute") is None
    assert term_note({"detail": {"lanes": ["x", {"p_fail": "y"}]}}, name="risk") is None
    assert term_note({"detail": {"p_fail": True}}, name="risk") is None


def test_format_seconds() -> None:
    assert format_seconds(1.5) == "1.50s"
    assert format_seconds(None) == "-"
    assert format_seconds(float("nan")) == "-"
    assert format_seconds(True) == "-"
