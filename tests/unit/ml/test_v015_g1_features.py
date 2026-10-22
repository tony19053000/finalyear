"""V0.15 G1 (D334): features from the replay snapshot - every feature, NaN never 0.

* A real stored decision (pinned Mode 1 on a 3-node fabric): every feature
  checked against the replay's own numbers.
* Each missing input -> that feature is NaN (never 0); non-finite inputs too;
  a candidate absent from the snapshot -> every node feature NaN.
* Replay v1 and v2 give identical features; the V0.14 golden corpus (397
  recorded planner calls, replay v1, whole and split jobs) decodes and gives
  finite-or-NaN features for every node, also after a v2 round trip.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import math
from pathlib import Path
from typing import Any

import pytest
from mf_ml_fixtures import stored

from meshforge.controller.ml.features import Predicted, features, features_from_replay
from meshforge.controller.ml.schema import FEATURES, NUMERIC_FEATURES, FeatureGroup
from meshforge.controller.planner.replay import from_replay, to_replay
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    ControllerLinkStat,
    LinkStat,
    NodeSnapshot,
    PlanRequest,
    SplitInfo,
)

pytestmark = pytest.mark.unit

CORPUS = Path(__file__).parents[1] / "planner" / "data" / "v014_g1_golden.json.gz"
NODE_FEATURES = tuple(
    f.name
    for f in FEATURES
    if f.group in (FeatureGroup.HARDWARE, FeatureGroup.LOAD)
    or f.name.startswith("cache_")
)


def _pred(**kw: Any) -> Predicted:
    base: dict[str, Any] = {"raw_s": 10.0, "source": "history", "detail": {}}
    return Predicted(**(base | kw))


def _same(a: dict[str, float], b: dict[str, float]) -> bool:
    return list(a) == list(b) and all(
        (math.isnan(x) and math.isnan(y)) or x == y
        for x, y in zip(a.values(), b.values(), strict=True)
    )


def _node(snapshot: ClusterSnapshot, node_id: str) -> NodeSnapshot:
    return next(n for n in snapshot.nodes if n.node_id == node_id)


def _with_node(
    snapshot: ClusterSnapshot, node_id: str, **changes: Any
) -> ClusterSnapshot:
    return dataclasses.replace(
        snapshot,
        nodes=tuple(
            dataclasses.replace(n, **changes) if n.node_id == node_id else n
            for n in snapshot.nodes
        ),
    )


# ------------------------------------------------------------ exact values


def test_every_feature_of_a_real_stored_decision() -> None:
    s = stored(pinned="node_001")
    request, snapshot, now = from_replay(s.replay)
    row = next(r for r in s.candidates if r["node_id"] == "node_001")
    term = row["estimate"]["terms"]["transfer"]
    got = features(
        request,
        snapshot,
        now,
        "node_001",
        Predicted(term["seconds"], term["source"], term["detail"]),
    )
    node = _node(snapshot, "node_001")
    assert list(got) == list(NUMERIC_FEATURES)
    assert got["hw_cpu_gflops"] == node.cpu_score
    assert math.isnan(got["hw_gpu_tflops"])  # never measured
    assert got["hw_cpu_threads"] == 8.0
    assert got["hw_ram_bytes"] == float(16 * 1024**3)
    assert got["hw_gpu_count"] == 0.0  # no GPU is a known 0
    assert math.isnan(got["hw_gpu_vram_max_bytes"])
    assert got["load_cpu_percent"] == node.cpu_percent
    assert got["load_ram_free_bytes"] == float(8 * 1024**3)
    assert got["load_live_attempts"] == 0.0
    assert got["load_used_cpu_slots"] == 0.0
    assert got["load_cpu_slots"] == 2.0
    assert got["load_owner_active"] == 0.0
    assert got["cache_missing_bytes"] == 200e6  # node_001 holds no tree
    assert got["cache_missing_frac"] == 1.0
    assert got["cache_env_cached"] == 0.0
    assert math.isnan(got["cache_items_tree_held"])  # not a split job
    assert got["net_local"] == 0.0  # node_000 submitted
    assert got["net_ctl_bw_bytes_s"] == 2e7
    assert math.isnan(got["net_peer_bw_bytes_s"])  # no peer links in the fabric
    assert math.isnan(got["net_peer_rtt_ms"])
    assert math.isnan(got["net_peer_link_age_s"])
    assert got["net_term_bw_bytes_s"] == 2e7  # what the transfer term used
    assert got["job_input_bytes"] == 200e6
    assert math.isnan(got["job_items"])
    assert math.isnan(got["job_item_bytes"])
    assert math.isnan(got["job_chunk_items"])
    assert got["job_lanes"] == 1.0
    assert math.isnan(got["job_cpus"])
    assert math.isnan(got["job_ram_bytes"])
    assert math.isnan(got["job_vram_bytes"])
    assert got["job_gpu"] == 0.0
    assert got["job_hist_output_bytes"] == 5e6
    assert got["an_raw_s"] == 10.0
    assert (got["an_src_history"], got["an_src_measured"]) == (1.0, 0.0)
    assert (got["an_src_scaled"], got["an_src_assumed"]) == (0.0, 0.0)
    assert got["an_term_samples"] == 5.0
    assert math.isnan(got["an_hist_run_s"])  # node_001 has no history
    assert got["an_hist_samples"] == 0.0


def test_local_node_with_history_and_a_split_job() -> None:
    s = stored(split=True)
    request, snapshot, now = from_replay(s.replay)
    got = features(request, snapshot, now, "node_000", _pred(lanes=3, chunk_items=166))
    assert got["net_local"] == 1.0
    assert got["cache_missing_bytes"] == 0.0  # seeded local + holds the tree
    assert got["cache_items_tree_held"] == 1.0
    assert got["job_items"] == 2000.0
    assert got["job_item_bytes"] == 1e8
    assert got["job_chunk_items"] == 166.0
    assert got["job_lanes"] == 3.0
    assert got["an_hist_run_s"] == request.history.by_node_s["node_000"]
    assert got["an_hist_samples"] == 5.0
    other = features(request, snapshot, now, "node_002", _pred())
    assert other["cache_items_tree_held"] == 0.0


def test_peer_link_features_pick_the_fastest_link_from_a_holder() -> None:
    s = stored(learnable=False)  # the synthetic fabric's full link matrix
    request, snapshot, now = from_replay(s.replay)
    got = features(request, snapshot, now, "node_001", _pred())
    holder = "node_000"  # the only node caching the job tree
    link = snapshot.links[("node_001", holder)]
    assert got["net_peer_bw_bytes_s"] == link.bw_bytes_s
    assert got["net_peer_rtt_ms"] == link.rtt_ms
    assert now is not None
    assert got["net_peer_link_age_s"] == (now - link.measured_at).total_seconds()
    # the holder itself has no link from a holder
    own = features(request, snapshot, now, holder, _pred())
    assert math.isnan(own["net_peer_bw_bytes_s"])


def test_gpu_and_env_cache_flags() -> None:
    s = stored()
    request, snapshot, now = from_replay(s.replay)
    snapshot = _with_node(
        snapshot,
        "node_001",
        gpu_vram_bytes=(0, 8 * 1024**3),
        gpu_score=12.5,
        cached_envs={"a" * 64: 100},
    )
    request = dataclasses.replace(
        request,
        gpu="required",
        history=dataclasses.replace(
            request.history, env_hash_by_node={"node_001": "a" * 64}
        ),
    )
    got = features(request, snapshot, now, "node_001", _pred())
    assert got["hw_gpu_count"] == 2.0
    assert got["hw_gpu_vram_max_bytes"] == float(8 * 1024**3)  # 0 = did not say
    assert got["hw_gpu_tflops"] == 12.5
    assert got["cache_env_cached"] == 1.0
    assert got["job_gpu"] == 1.0


# ------------------------------------------------------------ NaN, never 0


def _base() -> tuple[PlanRequest, ClusterSnapshot, Any]:
    s = stored(pinned="node_001")
    return from_replay(s.replay)


@pytest.mark.parametrize(
    ("change", "feature"),
    [
        ({"cpu_score": None}, "hw_cpu_gflops"),
        ({"cpu_score": float("inf")}, "hw_cpu_gflops"),
        ({"cpu_score": float("nan")}, "hw_cpu_gflops"),
        ({"gpu_score": None}, "hw_gpu_tflops"),
        ({"cpu_threads": None}, "hw_cpu_threads"),
        ({"ram_bytes": None}, "hw_ram_bytes"),
        ({"cpu_percent": None}, "load_cpu_percent"),
        ({"cpu_percent": float("nan")}, "load_cpu_percent"),
        ({"ram_free_bytes": None}, "load_ram_free_bytes"),
    ],
)
def test_a_missing_node_input_is_nan(change: dict[str, Any], feature: str) -> None:
    request, snapshot, now = _base()
    snapshot = _with_node(snapshot, "node_001", **change)
    got = features(request, snapshot, now, "node_001", _pred())
    assert math.isnan(got[feature]), feature


@pytest.mark.parametrize(
    ("change", "feature"),
    [
        ({"cpus": None}, "job_cpus"),
        ({"ram_bytes": None}, "job_ram_bytes"),
        ({"vram_bytes": None}, "job_vram_bytes"),
        ({"split": None}, "job_items"),
        ({"split": None}, "job_item_bytes"),
        ({"split": SplitInfo(items=10, item_bytes=None)}, "job_item_bytes"),
        ({"split": SplitInfo(items=10, items_tree=None)}, "cache_items_tree_held"),
        ({"trees": ()}, "cache_missing_frac"),
    ],
)
def test_a_missing_job_input_is_nan(change: dict[str, Any], feature: str) -> None:
    request, snapshot, now = _base()
    request = dataclasses.replace(request, **change)
    got = features(request, snapshot, now, "node_001", _pred())
    assert math.isnan(got[feature]), feature


def test_missing_history_links_and_detail_are_nan() -> None:
    request, snapshot, now = _base()
    request = dataclasses.replace(
        request,
        history=dataclasses.replace(request.history, output_bytes=None),
    )
    snapshot = dataclasses.replace(snapshot, controller_links={})
    got = features(request, snapshot, now, "node_001", _pred(detail={}))
    for name in (
        "job_hist_output_bytes",
        "net_ctl_bw_bytes_s",
        "net_term_bw_bytes_s",
        "an_term_samples",
        "an_hist_run_s",
        "job_chunk_items",
    ):
        assert math.isnan(got[name]), name
    unknown = features(request, snapshot, now, "node_001", _pred(source=None))
    for name in (
        "an_src_measured",
        "an_src_history",
        "an_src_scaled",
        "an_src_assumed",
    ):
        assert math.isnan(unknown[name]), name
    odd = features(request, snapshot, now, "node_001", _pred(raw_s=float("inf")))
    assert math.isnan(odd["an_raw_s"])


def test_a_stale_link_without_now_has_no_age_and_a_bad_bandwidth_is_skipped() -> None:
    request, snapshot, _now = _base()
    snapshot = dataclasses.replace(
        snapshot,
        links={
            ("node_001", "node_000"): LinkStat(
                bw_bytes_s=float("nan"), rtt_ms=1.0, samples=1, measured_at=_now
            ),
            ("node_001", "node_002"): LinkStat(  # node_002 holds nothing
                bw_bytes_s=9e9, rtt_ms=1.0, samples=1, measured_at=_now
            ),
        },
        controller_links={
            "node_001": ControllerLinkStat(bw_bytes_s=float("inf"), samples=1)
        },
    )
    got = features(request, snapshot, None, "node_001", _pred())
    assert math.isnan(got["net_peer_bw_bytes_s"])  # NaN bw never wins, 9e9 not a holder
    assert math.isnan(got["net_peer_link_age_s"])
    assert math.isnan(got["net_ctl_bw_bytes_s"])  # non-finite -> NaN, not 0


def test_a_candidate_absent_from_the_snapshot_has_every_node_feature_nan() -> None:
    request, snapshot, now = _base()
    got = features(request, snapshot, now, "node_gone", _pred())
    for name in NODE_FEATURES:
        assert math.isnan(got[name]), name
    assert got["job_input_bytes"] == 200e6  # job features stay


def test_no_feature_is_ever_zero_for_a_missing_input_anywhere() -> None:
    """Everything optional removed at once: every value is NaN or a fact
    the planner knows as a number (counts, flags, sizes)."""
    request, snapshot, now = _base()
    bare = dataclasses.replace(
        _node(snapshot, "node_001"),
        cpu_score=None,
        gpu_score=None,
        cpu_threads=None,
        ram_bytes=None,
        cpu_percent=None,
        ram_free_bytes=None,
    )
    snapshot = dataclasses.replace(
        snapshot, nodes=(bare,), controller_links={}, links={}
    )
    request = dataclasses.replace(
        request,
        cpus=None,
        ram_bytes=None,
        vram_bytes=None,
        history=dataclasses.replace(request.history, output_bytes=None),
    )
    got = features(request, snapshot, now, "node_001", _pred(detail={}))
    known_numbers = {
        "hw_gpu_count",
        "load_live_attempts",
        "load_used_cpu_slots",
        "load_cpu_slots",
        "load_owner_active",
        "cache_missing_bytes",
        "cache_missing_frac",
        "cache_env_cached",
        "net_local",
        "job_input_bytes",
        "job_lanes",
        "job_gpu",
        "an_raw_s",
        "an_src_measured",
        "an_src_history",
        "an_src_scaled",
        "an_src_assumed",
        "an_hist_samples",
    }
    for name, value in got.items():
        if name not in known_numbers:
            assert math.isnan(value), name


# ------------------------------------------------------------ replay v1/v2


def _as_v1(replay: dict[str, Any]) -> dict[str, Any]:
    old = json.loads(json.dumps(replay))
    old["replay_version"] = 1
    old["request"].pop("objective", None)
    for node in old["snapshot"]["nodes"]:
        node.pop("reliability", None)
    for key in (
        "risk",
        "prefer_local_margin",
        "min_useful_bw_bytes_s",
        "min_useful_bytes",
        "prune_dominated",
    ):
        old["snapshot"]["config"].pop(key, None)
    return old


@pytest.mark.parametrize("kind", ["whole", "split"])
def test_replay_v1_and_v2_give_identical_features(kind: str) -> None:
    s = stored(split=kind == "split", learnable=False)
    assert s.replay["replay_version"] == 2
    for node in s.snapshot.nodes:
        v2 = features_from_replay(s.replay, node.node_id, _pred())
        v1 = features_from_replay(_as_v1(s.replay), node.node_id, _pred())
        assert _same(v1, v2)


def _corpus() -> list[dict[str, Any]]:
    with gzip.open(CORPUS, "rt", encoding="utf-8") as fh:
        entries: list[dict[str, Any]] = json.load(fh)["entries"]
    return entries


def test_every_golden_corpus_replay_gives_finite_or_nan_features() -> None:
    entries = _corpus()
    assert len(entries) >= 390
    checked = 0
    for entry in entries:
        replay = entry["replay"]
        assert replay["replay_version"] == 1
        request, snapshot, now = from_replay(replay)
        again = to_replay(request, snapshot, now, projected=False)  # v2
        for node_id in [n.node_id for n in snapshot.nodes] + ["node_missing"]:
            for source in ("measured", "history", "scaled", "assumed", None):
                pred = _pred(source=source, detail={"samples": 3, "bw_bytes_s": 1e6})
                got = features_from_replay(replay, node_id, pred)
                assert list(got) == list(NUMERIC_FEATURES)
                assert all(isinstance(v, float) for v in got.values())
                assert not any(math.isinf(v) for v in got.values())
                assert _same(got, features_from_replay(again, node_id, pred))
                checked += 1
    assert checked > 2000
