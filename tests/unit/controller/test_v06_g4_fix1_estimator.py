"""V0.6 G4 fix round 1 (D131): the transfer term per missing tree, the bandwidth
floor and finite estimates, scaled compute normalised by score, the age of a
``history`` controller link, and decision JSON that PostgreSQL always accepts.

Pure: snapshots by hand, an injected ``now``; every expected number is computed
by hand in the docstring."""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any

import pytest

from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    TermSource,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.decision_json import (
    candidates_json,
    estimate_json,
    json_safe,
)
from meshforge.controller.planner.estimator import (
    MAX_TERM_S,
    MAX_TRANSFER_ITEMS,
    estimate,
)
from meshforge.controller.planner.types import ControllerLinkStat, LinkStat

pytestmark = pytest.mark.unit

MB = 1_000_000
NOW = dt.datetime(2026, 10, 7, 12, 0, tzinfo=dt.UTC)
BIG = "sha256:" + "a" * 64
SMALL = "sha256:" + "b" * 64
THIRD = "sha256:" + "c" * 64
EXEC = frozenset({"exec", "exec-subprocess"})
CFG = PlannerConfig(default_bw_bytes_s=50 * MB, min_bw_bytes_s=1024.0)


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_slots": 2,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "j",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "k",
        "trees": (TreeNeed(BIG, 200 * MB), TreeNeed(SMALL, 10 * MB)),
    }
    base.update(kw)
    return PlanRequest(**base)


def link(bw: float, age_s: float = 10.0) -> LinkStat:
    return LinkStat(bw, 2.0, 4, NOW - dt.timedelta(seconds=age_s))


def snap(*nodes: NodeSnapshot, **kw: Any) -> ClusterSnapshot:
    kw.setdefault("config", CFG)
    return ClusterSnapshot(nodes=tuple(nodes), **kw)


def transfer(req: PlanRequest, n: NodeSnapshot, s: ClusterSnapshot) -> Any:
    return estimate(req, n, "cpu", s, local=False, now=NOW).transfer


# ------------------------------------------------------- per missing tree


def test_each_tree_is_costed_by_its_own_source_and_the_times_are_summed() -> None:
    """BIG (200 MB) only from the controller at 2 MB/s = 100 s (history);
    SMALL (10 MB) from holder h over a 20 MB/s probed link = 0.5 s (measured).
    Sum 100.5 s; the term is as weak as its weakest source: history."""
    target, holder = node("t"), node("h", cached=frozenset({SMALL}))
    s = snap(
        target,
        holder,
        links={("t", "h"): link(20 * MB)},
        controller_links={"t": ControllerLinkStat(2 * MB, 5)},
    )
    term = transfer(request(), target, s)
    assert term.seconds == pytest.approx(100.5)
    assert term.source is TermSource.HISTORY
    assert term.detail["missing_bytes"] == 210 * MB
    assert term.detail["bw_source"] == "mixed" and "bw_from" not in term.detail
    big, small = term.detail["items"]
    assert (big["tree"], big["bytes"], big["seconds"]) == (BIG, 200 * MB, 100.0)
    assert (big["source"], big["bw_from"]) == ("history", "controller")
    assert (small["tree"], small["bytes"], small["seconds"]) == (SMALL, 10 * MB, 0.5)
    assert (small["source"], small["bw_source"], small["bw_from"]) == (
        "measured",
        "probe",
        "h",
    )


def test_a_tree_nobody_measured_makes_the_whole_term_assumed() -> None:
    """SMALL over the probe (measured), BIG over the default 50 MB/s = 4 s
    (assumed): 4.5 s, source assumed - never ``measured``."""
    target, holder = node("t"), node("h", cached=frozenset({SMALL}))
    s = snap(target, holder, links={("t", "h"): link(20 * MB)})
    term = transfer(request(), target, s)
    assert term.seconds == pytest.approx(4.5)
    assert term.source is TermSource.ASSUMED
    assert [i["source"] for i in term.detail["items"]] == ["assumed", "measured"]


def test_two_trees_from_two_holders_each_name_their_own_holder() -> None:
    """BIG from h1 at 10 MB/s = 20 s, SMALL from h2 at 5 MB/s = 2 s: 22 s,
    measured, and each item names the node that really holds it."""
    target = node("t")
    h1, h2 = node("h1", cached=frozenset({BIG})), node("h2", cached=frozenset({SMALL}))
    s = snap(
        target, h1, h2, links={("t", "h1"): link(10 * MB), ("t", "h2"): link(5 * MB)}
    )
    term = transfer(request(), target, s)
    assert term.seconds == pytest.approx(22.0)
    assert term.source is TermSource.MEASURED
    assert [i["bw_from"] for i in term.detail["items"]] == ["h1", "h2"]
    assert term.detail["bw_source"] == "probe" and "bw_from" not in term.detail
    assert term.detail["bw_holders"] == ["h1", "h2"]
    assert term.detail["link_age_s"] == 10.0  # the oldest link used


def test_one_source_for_every_tree_is_named_once_at_the_top() -> None:
    """Both trees on h: 210 MB / 10 MB/s = 21 s; the top of the detail says
    ``probe`` from ``h`` (what ``compute explain`` prints) and the items agree."""
    target, holder = node("t"), node("h", cached=frozenset({BIG, SMALL}))
    s = snap(target, holder, links={("t", "h"): link(10 * MB)})
    term = transfer(request(), target, s)
    assert term.seconds == pytest.approx(21.0)
    assert term.detail["bw_source"] == "probe" and term.detail["bw_from"] == "h"
    assert term.detail["bw_bytes_s"] == 10 * MB
    assert {i["bw_from"] for i in term.detail["items"]} == {"h"}


def test_a_single_missing_tree_keeps_the_flat_detail() -> None:
    target, holder = node("t"), node("h", cached=frozenset({BIG}))
    s = snap(target, holder, links={("t", "h"): link(10 * MB)})
    term = transfer(request(trees=(TreeNeed(BIG, 200 * MB),)), target, s)
    assert term.seconds == pytest.approx(20.0)
    assert "items" not in term.detail and term.detail["bw_from"] == "h"


def test_the_controller_wins_a_tree_only_when_it_is_faster_than_the_probe() -> None:
    """BIG is on h (10 MB/s) and the controller serves at 40 MB/s: 5 s from
    the controller (history), not 20 s measured."""
    target, holder = node("t"), node("h", cached=frozenset({BIG}))
    s = snap(
        target,
        holder,
        links={("t", "h"): link(10 * MB)},
        controller_links={"t": ControllerLinkStat(40 * MB, 3)},
    )
    term = transfer(request(trees=(TreeNeed(BIG, 200 * MB),)), target, s)
    assert term.seconds == pytest.approx(5.0)
    assert term.source is TermSource.HISTORY


def test_a_stale_link_of_one_tree_is_named_and_that_tree_falls_back() -> None:
    """h1's link (BIG) is 2 h old: BIG falls back to the default (4 s,
    assumed) and h1 is named; SMALL still rides h2's fresh link (2 s)."""
    target = node("t")
    h1, h2 = node("h1", cached=frozenset({BIG})), node("h2", cached=frozenset({SMALL}))
    s = snap(
        target,
        h1,
        h2,
        links={("t", "h1"): link(10 * MB, 7200.0), ("t", "h2"): link(5 * MB)},
    )
    term = transfer(request(), target, s)
    assert term.seconds == pytest.approx(6.0)
    assert term.source is TermSource.ASSUMED
    assert term.detail["stale_links_ignored"] == ["h1"]


def test_the_item_list_is_bounded_but_the_seconds_cover_every_tree() -> None:
    """40 inputs of 1 MB over the default 50 MB/s = 0.8 s; the detail keeps
    16 items and counts the other 24."""
    trees = tuple(TreeNeed(f"sha256:{i:064x}", 1 * MB) for i in range(40))
    target = node("t")
    term = transfer(request(trees=trees), target, snap(target))
    assert term.seconds == pytest.approx(0.8)
    assert len(term.detail["items"]) == MAX_TRANSFER_ITEMS == 16
    assert term.detail["items_omitted"] == 24
    assert term.detail["missing_bytes"] == 40 * MB


def test_equal_links_break_on_the_node_id_in_any_snapshot_order() -> None:
    target = node("t")
    holders = [node(h, cached=frozenset({BIG})) for h in ("h3", "h1", "h2")]
    links = {("t", h.node_id): link(10 * MB) for h in holders}
    req = request(trees=(TreeNeed(BIG, 200 * MB),))
    for order in (holders, holders[::-1], [holders[1], holders[2], holders[0]]):
        term = transfer(req, target, snap(target, *order, links=links))
        assert term.detail["bw_from"] == "h1"


# ---------------------------------------------------- history link's age


def test_a_history_term_shows_the_age_of_the_last_controller_transfer() -> None:
    """D131: controller links have no staleness rule, so explain shows how old
    the newest transfer behind the number is (here 2 h)."""
    target = node("t")
    last = NOW - dt.timedelta(hours=2)
    s = snap(target, controller_links={"t": ControllerLinkStat(20 * MB, 6, last)})
    term = transfer(request(trees=(TreeNeed(BIG, 200 * MB),)), target, s)
    assert term.source is TermSource.HISTORY
    assert term.detail["last_transfer_age_s"] == 7200.0
    assert term.detail["samples"] == 6


def test_an_unknown_transfer_time_is_none_not_zero() -> None:
    target = node("t")
    s = snap(target, controller_links={"t": ControllerLinkStat(20 * MB, 6)})
    term = transfer(request(trees=(TreeNeed(BIG, 200 * MB),)), target, s)
    assert term.detail["last_transfer_age_s"] is None


# ------------------------------------------------- floor and finiteness


@pytest.mark.parametrize("bw", [1e-300, 5e-324, 0.0, 1.0, 1023.9, math.nan, -5.0])
def test_a_link_below_the_floor_is_costed_at_the_floor_and_flagged(bw: float) -> None:
    """1 MiB over a probed link slower than 1024 B/s is costed at 1024 B/s =
    1024 s (D131) - finite, and flagged ``bw_clamped``."""
    target, holder = node("t"), node("h", cached=frozenset({BIG}))
    s = snap(target, holder, links={("t", "h"): link(bw)})
    term = transfer(request(trees=(TreeNeed(BIG, 1024 * 1024),)), target, s)
    assert term.seconds == pytest.approx(1024.0)
    assert term.detail["bw_clamped"] is True
    assert term.detail["bw_bytes_s"] == 1024.0


def test_a_link_at_the_floor_is_not_flagged() -> None:
    target, holder = node("t"), node("h", cached=frozenset({BIG}))
    s = snap(target, holder, links={("t", "h"): link(1024.0)})
    term = transfer(request(trees=(TreeNeed(BIG, 1024 * 1024),)), target, s)
    assert term.seconds == pytest.approx(1024.0) and "bw_clamped" not in term.detail


def test_the_floor_is_the_configured_one() -> None:
    cfg = PlannerConfig(min_bw_bytes_s=4096.0)
    target, holder = node("t"), node("h", cached=frozenset({BIG}))
    s = snap(target, holder, links={("t", "h"): link(1.0)}, config=cfg)
    term = transfer(request(trees=(TreeNeed(BIG, 4096 * 100),)), target, s)
    assert term.seconds == pytest.approx(100.0)


@pytest.mark.parametrize("ctl", [1e-300, 0.0, math.nan])
def test_controller_history_and_output_use_the_floor_too(ctl: float) -> None:
    """A controller link of ~0 B/s: transfer 2048 B / 1024 = 2 s, output
    1024 B / 1024 = 1 s - not Infinity."""
    target = node("t")
    s = snap(target, controller_links={"t": ControllerLinkStat(ctl, 2)})
    req = request(
        trees=(TreeNeed(BIG, 2048),),
        history=History(output_bytes=1024.0, output_samples=1),
    )
    e = estimate(req, target, "cpu", s, local=False, now=NOW)
    assert e.transfer.seconds == pytest.approx(2.0)
    assert e.output.seconds == pytest.approx(1.0)
    assert e.output.detail["bw_clamped"] is True


@pytest.mark.parametrize(
    "history",
    [
        History(by_node_s={"t": math.inf}),
        History(by_node_s={"t": math.nan}),
        History(by_node_s={"o": 1e308}),
        History(by_node_s={"o": 10.0}, output_bytes=math.inf),
        History(by_node_s={"o": 10.0}, output_bytes=math.nan),
    ],
)
def test_every_estimate_is_finite_whatever_the_history_says(history: History) -> None:
    target = node("t", cpu_score=1e-300)
    other = node("o", cpu_score=1e300)
    s = snap(target, other)
    huge = request(trees=(TreeNeed(BIG, 2**62),), history=history)
    e = estimate(huge, target, "cpu", s, local=False, now=NOW)
    assert math.isfinite(e.total_s)
    for term in e.terms().values():
        assert 0.0 <= term.seconds <= MAX_TERM_S
    json.dumps(estimate_json(e), allow_nan=False)  # what PostgreSQL will accept


def test_a_negative_history_cannot_win_by_a_negative_time() -> None:
    target = node("t")
    req = request(history=History(by_node_s={"t": -50.0}))
    assert estimate(req, target, "cpu", snap(target), local=False).compute.seconds == 0


# --------------------------------------------- scaled compute (normalised)


def test_scaled_compute_takes_the_best_normalised_run_not_the_fewest_seconds() -> None:
    """D131 (the tester's example): X (score 100) ran it in 10 s, Y (score 10)
    in 11 s. For a new score-100 node, X predicts 10 x 100 / 100 = 10 s and Y
    predicts 11 x 10 / 100 = 1.1 s: the best normalised value is Y's, 1.1 s."""
    new = node("n", cpu_score=100.0)
    x, y = node("x", cpu_score=100.0), node("y", cpu_score=10.0)
    req = request(history=History(by_node_s={"x": 10.0, "y": 11.0}))
    term = estimate(req, new, "cpu", snap(new, x, y), local=False, now=NOW).compute
    assert term.seconds == pytest.approx(1.1)
    assert term.source is TermSource.SCALED
    assert term.detail["from_node_id"] == "y"
    assert term.detail["score_ratio"] == pytest.approx(0.1)


def test_the_fewest_seconds_do_not_win_when_they_come_from_a_strong_node() -> None:
    """X (score 400) ran 5 s = 2000 units of work; Y (score 100) ran 8 s =
    800 units. A score-200 node: 800 / 200 = 4 s from Y (X would say 10 s)."""
    new = node("n", cpu_score=200.0)
    x, y = node("x", cpu_score=400.0), node("y", cpu_score=100.0)
    req = request(history=History(by_node_s={"x": 5.0, "y": 8.0}))
    term = estimate(req, new, "cpu", snap(new, x, y), local=False, now=NOW).compute
    assert term.seconds == pytest.approx(4.0)
    assert term.detail["from_node_id"] == "y"


def test_a_node_without_a_score_is_skipped_for_scaling_when_another_has_one() -> None:
    """X has no score (3 s raw), Y has one (8 s, score 100): the new score-200
    node scales from Y = 4 s rather than taking X's 3 s unscaled."""
    new = node("n", cpu_score=200.0)
    x, y = node("x"), node("y", cpu_score=100.0)
    req = request(history=History(by_node_s={"x": 3.0, "y": 8.0}))
    term = estimate(req, new, "cpu", snap(new, x, y), local=False, now=NOW).compute
    assert term.seconds == pytest.approx(4.0) and term.source is TermSource.SCALED


def test_equal_normalised_runs_break_on_the_node_id() -> None:
    new = node("n", cpu_score=100.0)
    x, y = node("x", cpu_score=50.0), node("y", cpu_score=100.0)
    req = request(history=History(by_node_s={"x": 8.0, "y": 4.0}))
    for order in ((new, x, y), (y, x, new)):
        term = estimate(req, new, "cpu", snap(*order), local=False, now=NOW).compute
        assert term.detail["from_node_id"] == "x" and term.seconds == pytest.approx(4.0)


@pytest.mark.parametrize("score", [math.inf, math.nan, -3.0, 0.0])
def test_an_unusable_score_never_scales(score: float) -> None:
    new = node("n", cpu_score=score)
    y = node("y", cpu_score=100.0)
    req = request(history=History(by_node_s={"y": 8.0}))
    term = estimate(req, new, "cpu", snap(new, y), local=False, now=NOW).compute
    assert term.seconds == 8.0 and term.detail["scaled"] is False


# ------------------------------------------------------- decision JSON


def test_json_safe_replaces_every_non_finite_number() -> None:
    dirty = {
        "a": math.inf,
        "b": [1.0, -math.inf, {"c": math.nan, "d": (math.inf, 2)}],
        "e": "Infinity",
        "f": True,
        "g": 7,
    }
    assert json_safe(dirty) == {
        "a": None,
        "b": [1.0, None, {"c": None, "d": [None, 2]}],
        "e": "Infinity",
        "f": True,
        "g": 7,
    }


def test_a_decision_over_an_absurd_fabric_is_strict_json() -> None:
    """The tester's T-1 fabric, pure: a 1e-300 B/s probed link and a 400 MB
    tree. The stored candidates must serialise with ``allow_nan=False``."""
    local = node("l", cached=frozenset({BIG}), cpu_score=math.inf)
    remote = node("r", cpu_score=1e-300)
    s = snap(local, remote, links={("r", "l"): link(1e-300)})
    req = request(
        trees=(TreeNeed(BIG, 400 * MB),),
        submitter_node_id="l",
        seeded=True,
        history=History(by_node_s={"l": 2.0}),
    )
    decision = plan(req, s, "cost_model", NOW)
    assert decision.chosen is not None and decision.chosen.node_id == "l"
    text = json.dumps(candidates_json(decision), allow_nan=False)
    assert "Infinity" not in text and "NaN" not in text
    remote_row = next(c for c in candidates_json(decision) if c["node_id"] == "r")
    assert remote_row["estimate"]["terms"]["transfer"]["seconds"] == pytest.approx(
        400 * MB / 1024.0
    )
