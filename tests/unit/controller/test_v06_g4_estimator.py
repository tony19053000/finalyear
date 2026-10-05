"""V0.6 G4 (D108): estimator v0 term by term against hand-computed values, the
``source`` of every term (a term is no better than its weakest input), the
staleness fallback for network links, and roadmap **criterion 4** on the pure
planner (a tiny job with a large tree stays Local; a heavy job with a small
tree goes to the faster node).

Pure: snapshots by hand, an injected ``now``."""

from __future__ import annotations

import datetime as dt
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
from meshforge.controller.planner.estimator import estimate, weakest
from meshforge.controller.planner.types import (
    ControllerLinkStat,
    EnvBuildStat,
    LinkStat,
)

MB = 1_000_000
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)
TREE = "sha256:" + "a" * 64
INPUT = "sha256:" + "b" * 64
ENV = "e" * 64
EXEC = frozenset({"exec", "exec-subprocess"})
CFG = PlannerConfig(
    default_bw_bytes_s=50 * MB,
    default_compute_s=60.0,
    default_env_s=0.0,
    link_stale_after_s=3600.0,
)


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_slots": 1,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "k",
        "trees": (TreeNeed(TREE, 200 * MB),),
    }
    base.update(kw)
    return PlanRequest(**base)


def link(bw: float, age_s: float = 10.0, rtt: float = 2.0) -> LinkStat:
    return LinkStat(
        bw_bytes_s=bw,
        rtt_ms=rtt,
        samples=4,
        measured_at=NOW - dt.timedelta(seconds=age_s),
    )


def snap(*nodes: NodeSnapshot, **kw: Any) -> ClusterSnapshot:
    return ClusterSnapshot(nodes=tuple(nodes), config=CFG, **kw)


def est(req: PlanRequest, n: NodeSnapshot, s: ClusterSnapshot, **kw: Any) -> Any:
    return estimate(
        req, n, "cpu", s, local=kw.pop("local", False), now=kw.pop("now", NOW)
    )


# ------------------------------------------------------------ weakest


def test_weakest_source_order() -> None:
    m, h, sc, a = (
        TermSource.MEASURED,
        TermSource.HISTORY,
        TermSource.SCALED,
        TermSource.ASSUMED,
    )
    assert weakest(m, h) is h and weakest(h, sc) is sc and weakest(sc, a) is a
    assert weakest(m) is m


# ------------------------------------------------------------ transfer


def test_transfer_nothing_missing_is_zero_measured() -> None:
    n = node("n", cached=frozenset({TREE}))
    term = est(request(), n, snap(n)).transfer
    assert (term.seconds, term.source) == (0.0, TermSource.MEASURED)


def test_transfer_default_bandwidth_is_assumed() -> None:
    n = node("n")
    term = est(request(), n, snap(n)).transfer
    assert term.seconds == pytest.approx(200 * MB / (50 * MB))  # 4 s
    assert term.source is TermSource.ASSUMED
    assert term.detail["bw_source"] == "assumed"


def test_transfer_controller_history_bandwidth() -> None:
    n = node("n")
    s = snap(n, controller_links={"n": ControllerLinkStat(10 * MB, 7)})
    term = est(request(), n, s).transfer
    assert term.seconds == pytest.approx(20.0)
    assert term.source is TermSource.HISTORY
    assert term.detail["bw_from"] == "controller" and term.detail["samples"] == 7


def test_transfer_fresh_link_from_a_holder_is_measured_and_faster_wins() -> None:
    n, h = node("n"), node("h", cached=frozenset({TREE}))
    s = snap(
        n,
        h,
        links={("n", "h"): link(100 * MB, age_s=30)},
        controller_links={"n": ControllerLinkStat(10 * MB, 3)},
    )
    term = est(request(), n, s).transfer
    assert term.seconds == pytest.approx(2.0)
    assert term.source is TermSource.MEASURED
    assert term.detail["bw_from"] == "h" and term.detail["link_age_s"] == 30.0
    # a slower link loses to the controller history
    s2 = snap(
        n,
        h,
        links={("n", "h"): link(5 * MB)},
        controller_links={"n": ControllerLinkStat(10 * MB, 3)},
    )
    term2 = est(request(), n, s2).transfer
    assert term2.seconds == pytest.approx(20.0) and term2.source is TermSource.HISTORY


def test_a_link_from_a_node_without_the_tree_is_not_used() -> None:
    n, h = node("n"), node("h")  # h holds nothing
    s = snap(n, h, links={("n", "h"): link(100 * MB)})
    assert est(request(), n, s).transfer.source is TermSource.ASSUMED


def test_link_direction_matters() -> None:
    n, h = node("n"), node("h", cached=frozenset({TREE}))
    # (h, n) = h downloaded from n: not the direction n needs
    s = snap(n, h, links={("h", "n"): link(100 * MB)})
    assert est(request(), n, s).transfer.source is TermSource.ASSUMED


def test_a_stale_link_falls_back_and_is_named() -> None:
    n, h = node("n"), node("h", cached=frozenset({TREE}))
    s = snap(
        n,
        h,
        links={("n", "h"): link(100 * MB, age_s=3601)},
        controller_links={"n": ControllerLinkStat(10 * MB, 2)},
    )
    term = est(request(), n, s).transfer
    assert term.source is TermSource.HISTORY and term.seconds == pytest.approx(20.0)
    assert term.detail["stale_links_ignored"] == ["h"]
    # exactly at the limit it is still fresh
    s_edge = snap(n, h, links={("n", "h"): link(100 * MB, age_s=3600)})
    assert est(request(), n, s_edge).transfer.source is TermSource.MEASURED


def test_without_now_no_link_is_fresh() -> None:
    n, h = node("n"), node("h", cached=frozenset({TREE}))
    s = snap(n, h, links={("n", "h"): link(100 * MB)})
    assert est(request(), n, s, now=None).transfer.source is TermSource.ASSUMED


def test_missing_bytes_count_inputs_and_seeded_local_skips_the_project() -> None:
    req = request(
        trees=(TreeNeed(TREE, 200 * MB), TreeNeed(INPUT, 50 * MB)), seeded=True
    )
    n = node("n")
    local = est(req, n, snap(n), local=True).transfer
    remote = est(req, n, snap(n), local=False).transfer
    assert local.detail["missing_bytes"] == 50 * MB
    assert remote.detail["missing_bytes"] == 250 * MB


# ---------------------------------------------------------------- env


def test_env_cached_on_the_node_is_zero_measured() -> None:
    req = request(history=History(env_hash_by_node={"n": ENV}))
    n = node("n", cached_envs={ENV: 123})
    term = est(req, n, snap(n)).env
    assert (term.seconds, term.source) == (0.0, TermSource.MEASURED)
    assert term.detail == {"env_cached": True, "env_hash": ENV}


def test_env_evicted_uses_build_history_for_the_base() -> None:
    req = request(history=History(env_hash_by_node={"n": ENV}))
    n = node("n")  # the env is gone from its cache
    s = snap(n, env_builds={"python-cpu": EnvBuildStat(42.0, 5)})
    term = est(req, n, s).env
    assert (term.seconds, term.source) == (42.0, TermSource.HISTORY)
    assert term.detail["samples"] == 5 and term.detail["base"] == "python-cpu"


def test_env_other_base_history_is_not_used() -> None:
    n = node("n")
    s = snap(n, env_builds={"cuda-pytorch": EnvBuildStat(300.0, 2)})
    term = est(request(), n, s).env
    assert (term.seconds, term.source) == (0.0, TermSource.ASSUMED)


# ------------------------------------------------------------ compute


def test_compute_own_history_scaled_and_assumed() -> None:
    fast = node("fast", cpu_score=400.0)
    slow = node("slow", cpu_score=100.0)
    s = snap(fast, slow)
    hist = History(by_node_s={"slow": 80.0}, samples_by_node={"slow": 3})
    own = est(request(history=hist), slow, s).compute
    assert (own.seconds, own.source) == (80.0, TermSource.HISTORY)
    scaled = est(request(history=hist), fast, s).compute
    assert scaled.seconds == pytest.approx(20.0) and scaled.source is TermSource.SCALED
    nothing = est(request(), fast, s).compute
    assert (nothing.seconds, nothing.source) == (60.0, TermSource.ASSUMED)


# ------------------------------------------------------------- output


def test_output_history_over_the_controller_link() -> None:
    n = node("n")
    hist = History(output_bytes=30 * MB, output_samples=4)
    s = snap(n, controller_links={"n": ControllerLinkStat(10 * MB, 2)})
    term = est(request(history=hist), n, s).output
    assert term.seconds == pytest.approx(3.0) and term.source is TermSource.HISTORY
    assert term.detail["symmetric"] is True and term.detail["out_bytes"] == 30 * MB
    # no controller link: the default bandwidth, so the term is assumed
    term2 = est(request(history=hist), n, snap(n)).output
    assert term2.seconds == pytest.approx(0.6) and term2.source is TermSource.ASSUMED


def test_no_output_history_is_zero_assumed() -> None:
    n = node("n")
    term = est(request(), n, snap(n)).output
    assert (term.seconds, term.source) == (0.0, TermSource.ASSUMED)


def test_total_is_the_sum_of_hand_computed_terms() -> None:
    n, h = node("n", cpu_score=100.0), node("h", cached=frozenset({TREE}))
    hist = History(
        by_node_s={"n": 12.5},
        samples_by_node={"n": 2},
        output_bytes=20 * MB,
        output_samples=2,
        env_hash_by_node={"n": ENV},
    )
    s = snap(
        n,
        h,
        links={("n", "h"): link(40 * MB)},
        controller_links={"n": ControllerLinkStat(8 * MB, 3)},
        env_builds={"python-cpu": EnvBuildStat(30.0, 1)},
    )
    guess = est(request(history=hist), n, s)
    # transfer 200/40 = 5 ; env 30 (not cached) ; compute 12.5 ; output 20/8 = 2.5
    assert guess.total_s == pytest.approx(5.0 + 30.0 + 12.5 + 2.5)


# ------------------------------------------------------- criterion 4


def _criterion_4(
    *, tree_bytes: int, run_on_local_s: float, link_bw: float
) -> tuple[str | None, Any]:
    """Local L (seeded, slow CPU) vs remote R (4x faster CPU) over a link of
    ``link_bw`` measured by probe; history: ran on L only."""
    local = node("L", cpu_score=100.0)
    remote = node("R", cpu_score=400.0)
    req = request(
        trees=(TreeNeed(TREE, tree_bytes),),
        submitter_node_id="L",
        seeded=True,
        history=History(by_node_s={"L": run_on_local_s}, samples_by_node={"L": 5}),
    )
    s = snap(
        local,
        remote,
        controller_links={"R": ControllerLinkStat(link_bw, 5)},
    )
    decision = plan(req, s, "cost_model", NOW)
    chosen = None if decision.chosen is None else decision.chosen.node_id
    return chosen, decision


def test_criterion_4_tiny_job_large_tree_stays_local() -> None:
    chosen, decision = _criterion_4(
        tree_bytes=500 * MB, run_on_local_s=2.0, link_bw=5 * MB
    )
    assert chosen == "L"
    by_node = {c.node_id: c for c in decision.candidates}
    assert by_node["L"].mode == "local" and by_node["R"].mode == "offload"
    local_est, remote_est = by_node["L"].estimate, by_node["R"].estimate
    assert local_est is not None and remote_est is not None
    # Local: nothing to move (seeded), 2 s of compute; remote: 100 s transfer
    assert local_est.transfer.seconds == 0.0
    assert remote_est.transfer.seconds == pytest.approx(100.0)
    assert remote_est.compute.seconds == pytest.approx(0.5)  # scaled 4x
    assert remote_est.compute.source is TermSource.SCALED


def test_criterion_4_inverse_heavy_compute_small_tree_goes_remote() -> None:
    chosen, decision = _criterion_4(
        tree_bytes=1 * MB, run_on_local_s=400.0, link_bw=5 * MB
    )
    assert chosen == "R"
    by_node = {c.node_id: c for c in decision.candidates}
    assert by_node["R"].estimate is not None
    assert by_node["R"].estimate.total_s == pytest.approx(0.2 + 100.0)


@pytest.mark.parametrize("policy", ["cost_model"])
def test_criterion_4_is_deterministic(policy: str) -> None:
    first = _criterion_4(tree_bytes=500 * MB, run_on_local_s=2.0, link_bw=5 * MB)[1]
    again = _criterion_4(tree_bytes=500 * MB, run_on_local_s=2.0, link_bw=5 * MB)[1]
    assert first == again and first.policy == policy
