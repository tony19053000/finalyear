"""V0.15 G1 tester round 1: adversarial tests derived from the brief (D333-D346).

Malformed input is a counted skip, never a crash; group splits never leak a
fingerprint (or a family) across train/test, including on tiny/degenerate and
built datasets; the Parquet file is canonical (order-, timezone- and
duplicate-independent) and round-trips NaN, unicode and empty datasets; D345
and D346 target definitions; features survive JSON-roundtripped replays and
NaN/inf/None telemetry.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import io
import json
import math
import random
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from mf_ml_fixtures import chosen, set_run, stored, whole_run

from meshforge.controller.ml import dataset as ds
from meshforge.controller.ml.features import Predicted, features
from meshforge.controller.ml.schema import NUMERIC_FEATURES, TERM_NAMES
from meshforge.controller.ml.splits import (
    NO_FAMILY,
    group_families,
    group_kfold,
    leave_one_family_out,
)
from meshforge.controller.ml.targets import (
    ChunkOutcome,
    Skip,
    observations,
)
from meshforge.controller.planner.replay import from_replay

pytestmark = pytest.mark.unit

PHASES = {"stage": 12_000, "env": 25_000, "run": 400_000, "collect": 300}
T0 = dt.datetime(2026, 10, 22, 9, 0, tzinfo=dt.UTC)


def _good(i: int = 0, **kw: Any) -> Any:
    return whole_run(
        stored(pinned="node_001"),
        phases=PHASES,
        decision_id=i + 1,
        attempt_id=f"att_{i}",
        finished_at=T0 + dt.timedelta(seconds=i),
        **kw,
    )


# ------------------------------------------------ malformed records are skips


@pytest.mark.parametrize(
    "replay",
    [
        {},
        {"version": 999},
        {"version": 2},
        {"version": 2, "request": None, "snapshot": None, "now": None},
        {"version": 2, "request": {}, "snapshot": {}, "now": "not-a-date"},
        {"version": "x"},
        [],
        "garbage",
        42,
    ],
)
def test_garbage_replay_is_a_counted_skip_not_a_crash(replay: Any) -> None:
    run = dataclasses.replace(_good(), replay=replay)
    data = ds.build([run, _good(1)])
    assert data.counts["skipped"][Skip.UNREADABLE_REPLAY.value] == 1
    assert data.counts["runs"] == 2
    assert data.counts["rows"] > 0  # the good run still contributes


def test_truncated_replay_every_top_level_key_removed() -> None:
    base = stored(pinned="node_001").replay
    for key in list(base):
        broken = {k: v for k, v in base.items() if k != key}
        data = ds.build([dataclasses.replace(_good(), replay=broken)])
        assert data.counts["runs"] == 1
        if key in ("request", "snapshot", "now", "version"):
            assert data.counts["skipped"]["unreadable_replay"] == 1, key


@pytest.mark.parametrize("candidates", [[], [None], [5, "x", []], [{}], [{"rank": 1}]])
def test_garbage_candidates_are_unmatched_not_a_crash(candidates: Any) -> None:
    run = _good()
    run = dataclasses.replace(
        run, outcome=dataclasses.replace(run.outcome, candidates=candidates)
    )
    data = ds.build([run])
    assert data.counts["rows"] == 0
    assert data.counts["skipped"]["unmatched"] >= 1


@pytest.mark.parametrize(
    "estimate",
    [
        None,
        5,
        "x",
        [],
        {"terms": None},
        {"terms": []},
        {"terms": {"compute": 5}},
        {"terms": {"compute": {"detail": "x", "source": 3}}},
    ],
)
def test_garbage_estimate_never_crashes(estimate: Any) -> None:
    run = _good()
    cands = copy.deepcopy(run.outcome.candidates)
    for row in cands:
        row["estimate"] = estimate
    run = dataclasses.replace(
        run, outcome=dataclasses.replace(run.outcome, candidates=cands)
    )
    data = ds.build([run])
    assert data.counts["rows"] == 0
    assert sum(data.counts["skipped"].values()) >= 1


@pytest.mark.parametrize(
    "bad",
    [-5, True, float("nan"), float("inf"), -float("inf"), 1.5, None],
)
def test_weird_phase_values_never_crash(bad: Any) -> None:
    phases = {**PHASES, "stage": bad, "env": bad, "run": bad}
    run = whole_run(stored(pinned="node_001"), phases=phases)
    rows, _skipped = observations(run)
    for obs in rows:
        assert math.isfinite(obs.y) and obs.actual_s > 0
    ds.build([run])


@pytest.mark.parametrize(
    "bad",
    [
        float("nan"),
        float("inf"),
        -1,
        True,
        None,
        1e30,
    ],
)
def test_weird_combine_and_lane_phases_never_crash(bad: Any) -> None:
    s = stored(forced_set=True)
    run = set_run(
        s,
        lane_phases={
            n["node_id"] if isinstance(n, dict) else n: {
                "stage": bad,
                "env": bad,
                "run": bad,
                "collect": 1,
            }
            for n in chosen(s)["node_set"]
        },
        combine_phases={"stage": bad, "env": bad, "run": bad, "collect": 1},
    )
    data = ds.build([run])
    for row in data.rows:
        assert math.isfinite(row["y"]), row["term"]
        assert row["actual_s"] > 0 and row["raw_s"] > 0


@pytest.mark.parametrize(
    "chunk",
    [
        ChunkOutcome(items=0, run_ms=100, outcome="committed"),
        ChunkOutcome(items=-5, run_ms=100, outcome="committed"),
        ChunkOutcome(items=10, run_ms=0, outcome="committed"),
        ChunkOutcome(items=10, run_ms=-3, outcome="committed"),
        ChunkOutcome(items=10, run_ms=None, outcome="committed"),
        ChunkOutcome(items=10, run_ms=100, outcome="lost"),
    ],
)
def test_degenerate_chunks_never_make_a_non_finite_rate(chunk: ChunkOutcome) -> None:
    s = stored(forced_set=True)
    run = set_run(s, chunks=dict.fromkeys(chosen(s)["node_set"], (chunk,)))
    data = ds.build([run])
    for row in data.rows:
        if row["term"] == "chunk_rate":
            assert math.isfinite(row["y"]) and row["actual_s"] > 0


def test_negative_items_chunk_does_not_produce_a_row() -> None:
    s = stored(forced_set=True)
    node = chosen(s)["node_set"][0]
    run = set_run(
        s,
        chunks={
            n: (ChunkOutcome(items=-10, run_ms=100, outcome="committed"),)
            for n in chosen(s)["node_set"]
        },
    )
    rows, _skipped = observations(run)
    assert not [o for o in rows if o.term == "chunk_rate" and o.node_id == node]


# --------------------------------------------------- D345 / D346 end to end


def test_d345_cached_env_and_resident_trees_are_counted_exact_for_set_lanes() -> None:
    s = stored(forced_set=True, learnable=False)
    run = set_run(s)
    rows, skipped = observations(run)
    # every skip is a valid reason, every learnable row has positive finite values
    assert set(skipped) <= {r.value for r in Skip}
    for o in rows:
        assert o.raw_s > 0 and o.actual_s > 0 and math.isfinite(o.y)
        if o.term in ("lane_stage",):
            assert o.detail.get("missing_bytes") != 0
        if o.term in ("lane_env",):
            assert o.detail.get("env_cached") is not True


def test_d346_collect_does_not_move_the_combine_target() -> None:
    s = stored(forced_set=True)
    lo = [
        o
        for o in observations(
            set_run(
                s, combine_phases={"stage": 400, "env": None, "run": 1600, "collect": 1}
            )
        )[0]
        if o.term == "combine"
    ]
    hi = [
        o
        for o in observations(
            set_run(
                s,
                combine_phases={
                    "stage": 400,
                    "env": None,
                    "run": 1600,
                    "collect": 10**9,
                },
            )
        )[0]
        if o.term == "combine"
    ]
    assert lo and hi
    assert lo[0].actual_s == hi[0].actual_s == pytest.approx(2.0)


def test_d346_stage_and_env_and_run_all_count() -> None:
    s = stored(forced_set=True)
    got = {}
    for phases, want in (
        ({"stage": 1000, "env": 2000, "run": 4000, "collect": 9}, 7.0),
        ({"stage": None, "env": None, "run": 4000, "collect": 9}, 4.0),
        ({"stage": 1000, "env": None, "run": 0, "collect": 9}, 1.0),
    ):
        rows = [
            o
            for o in observations(set_run(s, combine_phases=phases))[0]
            if o.term == "combine"
        ]
        assert rows, phases
        got[tuple(phases.values())] = rows[0].actual_s
        assert rows[0].actual_s == pytest.approx(want)


def test_d346_combine_run_phase_skipped_is_counted_not_zero_row() -> None:
    s = stored(forced_set=True)
    rows, skipped = observations(
        set_run(s, combine_phases={"stage": 1, "env": 1, "run": None, "collect": 1})
    )
    assert not [o for o in rows if o.term == "combine"]
    assert skipped[Skip.SKIPPED_PHASE.value] >= 1


def test_combine_on_other_node_than_predicted_is_node_mismatch() -> None:
    s = stored(forced_set=True)
    nodes = chosen(s)["node_set"]
    pred = chosen(s)["estimate"]["terms"]["combine"]["detail"]["combine_node_id"]
    other = next(n for n in nodes if n != pred)
    rows, skipped = observations(set_run(s, combine_node=other))
    assert skipped[Skip.NODE_MISMATCH.value] == 1
    assert not [o for o in rows if o.term == "combine"]


def test_every_term_name_has_a_count_even_when_zero_and_skips_cover_all_reasons() -> (
    None
):
    data = ds.build([])
    assert list(data.counts["rows_by_term"]) == list(TERM_NAMES)
    assert set(data.counts["skipped"]) == {r.value for r in Skip}
    assert data.counts["rows"] == 0 and data.rows == ()


# ------------------------------------------------------------- group splits


def _built(n_groups: int = 12, per: int = 3, seed: int = 0) -> ds.Dataset:
    rng = random.Random(seed)  # noqa: S311
    s = stored(pinned="node_001")
    fams = ["cpu.primes", "cpu.zlib", "io.copy", None]
    runs = []
    for g in range(n_groups):
        fam = fams[g % len(fams)]
        for k in range(per):
            runs.append(  # noqa: PERF401 - readability
                whole_run(
                    s,
                    phases={**PHASES, "run": rng.randint(50_000, 900_000)},
                    decision_id=g * 100 + k + 1,
                    attempt_id=f"att_{g}_{k}",
                    family=fam,
                    fingerprint=f"fp_{g}",
                    finished_at=T0 + dt.timedelta(seconds=g * 10 + k),
                )
            )
    rng.shuffle(runs)
    return ds.build(runs)


def test_splits_on_a_real_built_dataset_never_leak_and_cover_everything() -> None:
    data = _built()
    n = len(data.rows)
    assert n > 0
    for folds in (data.leave_one_family_out(), data.group_kfold(4, seed=3)):
        tested: list[int] = []
        for f in folds:
            assert set(f.train).isdisjoint(f.test)
            assert sorted(f.train + f.test) == list(range(n))
            g_test = {data.rows[i]["group"] for i in f.test}
            g_train = {data.rows[i]["group"] for i in f.train}
            assert g_test.isdisjoint(g_train)
            tested.extend(f.test)
        assert sorted(tested) == list(range(n))


def test_lofo_never_trains_on_the_held_out_family_even_across_fingerprints() -> None:
    data = _built()
    for f in data.leave_one_family_out():
        fam_test = {data.rows[i]["family"] or NO_FAMILY for i in f.test}
        fam_train = {data.rows[i]["family"] or NO_FAMILY for i in f.train}
        assert fam_test == {f.name}
        assert f.name not in fam_train


def test_lofo_degenerate_inputs() -> None:
    assert leave_one_family_out([]) == []
    one: list[dict[str, Any]] = [{"group": "g", "family": "a"}] * 3
    folds = leave_one_family_out(one)
    assert len(folds) == 1 and folds[0].train == () and folds[0].test == (0, 1, 2)
    only_none: list[dict[str, Any]] = [
        {"group": "g1", "family": None},
        {"group": "g2", "family": ""},
    ]
    folds = leave_one_family_out(only_none)
    assert [f.name for f in folds] == [NO_FAMILY]


def test_split_family_names_that_look_like_none_marker_or_unicode() -> None:
    rows: list[dict[str, Any]] = [
        {"group": "g1", "family": "(none)"},  # literal collides with NO_FAMILY
        {"group": "g2", "family": None},
        {"group": "g3", "family": "résumé.日本"},
        {"group": "g4", "family": "résumé.日本"},
    ]
    folds = leave_one_family_out(rows)
    for f in folds:
        assert not {rows[i]["group"] for i in f.train} & {
            rows[i]["group"] for i in f.test
        }
    assert {f.name for f in folds} == {"(none)", "résumé.日本"}


@pytest.mark.parametrize("k", [1, 0, -1])
def test_kfold_rejects_k_below_two(k: int) -> None:
    with pytest.raises(ValueError):
        group_kfold(
            [{"group": "a", "family": "x"}, {"group": "b", "family": "x"}], k, 0
        )


def test_kfold_with_k_equal_groups_is_leave_one_group_out() -> None:
    rows: list[dict[str, Any]] = [
        {"group": f"g{i}", "family": "x"} for i in range(5) for _ in range(2)
    ]
    folds = group_kfold(rows, 5, 1)
    assert len(folds) == 5 and all(len(f.test) == 2 for f in folds)
    with pytest.raises(ValueError):
        group_kfold(rows, 6, 1)
    with pytest.raises(ValueError):
        group_kfold([], 2, 1)


def test_kfold_seed_changes_the_partition_but_never_the_invariant() -> None:
    rows = [{"group": f"g{i}", "family": "x"} for i in range(30)]
    a = group_kfold(rows, 3, 1)
    b = group_kfold(rows, 3, 2)
    assert [f.test for f in a] != [f.test for f in b]
    assert [f.test for f in a] == [f.test for f in group_kfold(rows, 3, 1)]


def test_group_families_tie_break_is_the_smaller_name() -> None:
    rows = [{"group": "g", "family": "b"}, {"group": "g", "family": "a"}]
    assert group_families(rows) == {"g": "a"}


def test_split_is_independent_of_row_order_per_group() -> None:
    rows = [{"group": f"g{i % 7}", "family": f"f{i % 3}"} for i in range(40)]
    shuffled = rows[:]
    random.Random(5).shuffle(shuffled)  # noqa: S311
    for k in (2, 3, 7):
        a = {
            tuple(sorted(rows[i]["group"] for i in f.test))
            for f in group_kfold(rows, k, 9)
        }
        b = {
            tuple(sorted(shuffled[i]["group"] for i in f.test))
            for f in group_kfold(shuffled, k, 9)
        }
        assert {frozenset(x) for x in a} == {frozenset(x) for x in b}


# ----------------------------------------------- determinism and Parquet


def test_ten_shuffles_same_digest_and_duplicates_are_stable() -> None:
    runs = [_good(i) for i in range(5)] + [_good(2)]  # one exact duplicate
    want = ds.build(runs).digest
    for seed in range(10):
        r = runs[:]
        random.Random(seed).shuffle(r)  # noqa: S311
        assert ds.build(r).digest == want


def test_naive_and_aware_and_offset_timestamps_sort_and_round_trip_as_utc() -> None:
    s = stored(pinned="node_001")
    tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
    stamps = [
        dt.datetime(2026, 10, 22, 9, 0),  # noqa: DTZ001 - naive on purpose: taken as UTC
        dt.datetime(2026, 10, 22, 14, 30, 1, tzinfo=tz),  # 09:00:01 UTC
        dt.datetime(2026, 10, 22, 9, 0, 2, tzinfo=dt.UTC),
    ]
    runs = [
        whole_run(
            s, phases=PHASES, decision_id=i + 1, attempt_id=f"a{i}", finished_at=t
        )
        for i, t in enumerate(stamps)
    ]
    data = ds.build(runs[::-1])
    seen = [r["finished_at"] for r in data.rows]
    assert seen == sorted(seen) and all(t.tzinfo is not None for t in seen)
    table = pq.read_table(io.BytesIO(data.parquet))
    back = table.column("finished_at").to_pylist()
    assert back == seen
    assert next(r["decision_id"] for r in data.rows) == 1


def test_parquet_round_trip_values_nan_unicode_and_digest() -> None:
    s = stored(pinned="node_001")
    runs = [
        whole_run(
            s, phases=PHASES, family="ünï.日本語/\\ path with space", fingerprint="fp_ü"
        )
    ]
    data = ds.build(runs)
    assert data.digest == "sha256:" + hashlib.sha256(data.parquet).hexdigest()
    table = pq.read_table(io.BytesIO(data.parquet))
    assert table.num_rows == len(data.rows)
    assert table.column_names == list(ds.COLUMN_NAMES)
    for i, row in enumerate(data.rows):
        for name in (*NUMERIC_FEATURES, "y", "raw_s", "actual_s"):
            got = table.column(name)[i].as_py()
            want = row[name]
            if isinstance(want, float) and math.isnan(want):
                # NaN is a value, not a null (nulls would mean "unknown" twice)
                assert got is not None and math.isnan(got), name
            else:
                assert got == want, name
        assert table.column("family")[i].as_py() == row["family"]
        assert table.column("group")[i].as_py() == "fp_ü"
    # y really is ln(actual/raw) read back from the file
    ys = table.column("y").to_pylist()
    raws = table.column("raw_s").to_pylist()
    acts = table.column("actual_s").to_pylist()
    for y, r, a in zip(ys, raws, acts, strict=True):
        assert y == pytest.approx(math.log(a / r))
        assert r > 0 and a > 0 and math.isfinite(y)


def test_empty_dataset_is_a_valid_deterministic_parquet_with_schema() -> None:
    a, b = ds.build([]), ds.build([])
    assert a.parquet == b.parquet and a.digest == b.digest
    table = pq.read_table(io.BytesIO(a.parquet))
    assert table.num_rows == 0 and table.column_names == list(ds.COLUMN_NAMES)
    assert a.leave_one_family_out() == []
    with pytest.raises(ValueError):
        a.group_kfold(2, 0)


def test_all_skipped_dataset_equals_empty_dataset_bytes() -> None:
    skipped = ds.build([_good(replay=False)])
    assert skipped.counts["rows"] == 0
    assert skipped.parquet == ds.build([]).parquet
    assert skipped.counts["skipped"]["missing_replay"] == 1


def test_one_row_dataset() -> None:
    s = stored(pinned="node_001")
    run = whole_run(s, phases={"stage": 0, "env": 0, "run": 400_000, "collect": 1})
    data = ds.build([run])
    assert data.counts["rows"] >= 1
    folds = data.leave_one_family_out()
    assert len(folds) == 1 and folds[0].train == ()


def test_duplicate_run_does_not_change_nothing_but_doubles_rows() -> None:
    one = ds.build([_good()])
    two = ds.build([_good(), _good()])
    assert two.counts["rows"] == 2 * one.counts["rows"]


def test_dataset_metadata_has_only_versions() -> None:
    meta = pq.read_schema(io.BytesIO(ds.build([_good()]).parquet)).metadata
    keys = {k for k in meta if k.startswith(b"meshforge.")}
    assert keys == {ds.DATASET_METADATA_KEY, ds.FEATURE_METADATA_KEY}


# --------------------------------------------------------------- features


def test_features_of_a_json_roundtripped_replay_equal_the_original() -> None:
    s = stored(pinned="node_001")
    pred = Predicted(raw_s=3.0, source="history")
    direct = features(*from_replay(s.replay), "node_001", pred)
    via_json = features(
        *from_replay(json.loads(json.dumps(s.replay))), "node_001", pred
    )
    for k in NUMERIC_FEATURES:
        a, b = direct[k], via_json[k]
        assert (math.isnan(a) and math.isnan(b)) or a == b, k


def test_features_never_inf_and_order_is_the_schema_with_hostile_telemetry() -> None:
    request, snapshot, now = from_replay(stored(pinned="node_001").replay)
    hostile = dataclasses.replace(
        snapshot,
        nodes=tuple(
            dataclasses.replace(
                n,
                cpu_percent=float("inf"),
                ram_free_bytes=float("-inf"),  # type: ignore[arg-type]
                cpu_score=float("nan"),
                gpu_score=None,
            )
            for n in snapshot.nodes
        ),
    )
    for node in hostile.nodes:
        got = features(
            request,
            hostile,
            now,
            node.node_id,
            Predicted(raw_s=float("inf"), source="bogus"),
        )
        assert list(got) == list(NUMERIC_FEATURES)
        for k, v in got.items():
            assert not math.isinf(v), k
        assert math.isnan(got["load_cpu_percent"])
        assert math.isnan(got["load_ram_free_bytes"])
        assert math.isnan(got["hw_cpu_gflops"])
        assert math.isnan(got["an_raw_s"])
        for src in (
            "an_src_measured",
            "an_src_history",
            "an_src_scaled",
            "an_src_assumed",
        ):
            assert math.isnan(got[src])  # unknown source is unknown, not "all zero"


def test_unknown_node_and_no_now_do_not_crash() -> None:
    request, snapshot, _ = from_replay(stored(pinned="node_001").replay)
    got = features(request, snapshot, None, "ghost-node-ü", Predicted(1.0, None))
    assert list(got) == list(NUMERIC_FEATURES)
    assert math.isnan(got["hw_cpu_gflops"]) and math.isnan(got["net_peer_link_age_s"])


def test_a_dataset_feature_matrix_has_no_zero_where_input_was_missing() -> None:
    """Replay with node telemetry stripped -> NaN columns in the dataset rows."""
    s = stored(pinned="node_001")
    replay = copy.deepcopy(s.replay)
    for node in replay["snapshot"]["nodes"]:
        for key in ("cpu_percent", "ram_free_bytes"):
            node.pop(key, None)
    run = dataclasses.replace(_good(), replay=replay)
    data = ds.build([run])
    if data.counts["skipped"]["unreadable_replay"]:
        return  # refusing it is also acceptable (counted, not imputed)
    assert data.rows
    for row in data.rows:
        assert math.isnan(row["load_cpu_percent"])
        assert math.isnan(row["load_ram_free_bytes"])
