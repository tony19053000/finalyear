"""V0.10 G8 (0.10.9; D271, D272, D285): the pure parts.

* telemetry schema v3: the chunk columns' fingerprint is frozen, the
  ``chunk_record`` model = the declared columns + ``stderr_tail``;
* chunk records: stderr tails decoded and bounded at a character boundary,
  failure classes per outcome;
* run records of a split job: strategy from the first plan (``mode3`` /
  ``local`` / ``mode1``), the planner's pick (auto vs forced shadow), the job
  row's numbers, the workload identity of a split benchmark job;
* D274's ideal bound;
* labels: a split job counts once (its job row) and costs its job time, so a
  campaign's mode1 vs mode3 runs get a label;
* the campaign's ``mode3`` strategy and node sets;
* the planner's forced node set (D215 extended);
* chunk message bounds; the speculation times cache (D285); the agent's tail.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.bench.campaign import (
    CampaignSpecError,
    NodeRef,
    expand,
    parse_spec,
    run_dict,
    runs_json,
    spec_digest,
)
from meshforge.bench.records import metric_value
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitInfo,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.split_plan import FORCED_SET
from meshforge.controller.split.messages import (
    MAX_STDERR_TAIL_BYTES,
    parse_chunk_result,
)
from meshforge.controller.split.records import chunk_failure_class, clean_tail
from meshforge.controller.split.speculation import SiteTime, SiteTimesCache
from meshforge.controller.store.models.telemetry import ChunkRecord
from meshforge.controller.telemetry.labels import PlannedRunState, build_labels
from meshforge.controller.telemetry.record import RecordSource, build_record
from meshforge.controller.telemetry.schema import (
    CHUNK_COLUMNS,
    CHUNK_FINGERPRINTS,
    SCHEMA_VERSION,
    fingerprint,
)
from meshforge.controller.telemetry.split_facts import (
    JobTimes,
    SplitFacts,
    chosen_row,
    ideal_bound_s,
)

pytestmark = pytest.mark.unit

EXEC = frozenset({"exec", "exec-subprocess", "exec-split"})


def node(node_id: str, **kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": EXEC,
        "cpu_threads": 8,
        "cpu_slots": 1,
    }
    base.update(kw)
    return NodeSnapshot(node_id=node_id, **base)


def request(items: int = 3000, **kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job_01J0000000000000000000000",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e" * 64,
        "trees": (TreeNeed("sha256:" + "a" * 64, 10_000_000),),
        "submitter_node_id": "n1",
        "seeded": True,
        "split": SplitInfo(items=items),
    }
    base.update(kw)
    return PlanRequest(**base)


def trio() -> ClusterSnapshot:
    return ClusterSnapshot(nodes=(node("n1"), node("n2"), node("n3")))


T0 = dt.datetime(2026, 10, 21, 10, 0, tzinfo=dt.UTC)
JOB = "job_01J00000000000000000000G8X"


# ------------------------------------------------------------ schema v3


def test_chunk_columns_are_frozen_for_v3() -> None:
    assert SCHEMA_VERSION == 3
    assert fingerprint(CHUNK_COLUMNS) == CHUNK_FINGERPRINTS[SCHEMA_VERSION]
    assert max(CHUNK_FINGERPRINTS) == SCHEMA_VERSION


def test_chunk_model_is_the_declared_columns_plus_the_stderr_tail() -> None:
    names = [c.name for c in ChunkRecord.__table__.columns]
    assert names == [*(c.name for c in CHUNK_COLUMNS), "stderr_tail"]
    nullable = {c.name: bool(c.nullable) for c in ChunkRecord.__table__.columns}
    for column in CHUNK_COLUMNS:
        if column.name not in ("job_id", "chunk_index", "lease_seq"):  # the PK
            assert nullable[column.name] == column.nullable, column.name


# ------------------------------------------------------------ chunk records


def test_stderr_tail_keeps_the_end_and_never_cuts_a_character() -> None:
    data = ("é" * 40 + "\nboom\n").encode()
    tail = clean_tail(data, 11)
    assert tail is not None and tail.endswith("boom\n")
    assert "�" not in tail and len(tail.encode()) <= 11
    assert clean_tail(b"a\x00b\xff", 100) == "ab�"
    assert clean_tail(b"", 100) is None
    assert clean_tail(b"x", 0) is None
    assert clean_tail(b"x" * 100_000, 10**6) == "x" * 65_536


@pytest.mark.parametrize(
    ("outcome", "failure", "klass"),
    [
        ("committed", None, "ok"),
        ("lost", None, "ok"),
        ("superseded", None, "unavailable"),
        ("released", None, "unavailable"),
        ("expired", None, "infrastructure"),
        ("failed", "user_error", "workload"),
        ("failed", "oom", "workload"),
        ("failed", "transfer_error", "infrastructure"),
        ("failed", "owner_preempted", "unavailable"),
        ("failed", "nonsense", "infrastructure"),
        ("failed", None, "infrastructure"),
    ],
)
def test_chunk_failure_class(outcome: str, failure: str | None, klass: str) -> None:
    assert chunk_failure_class(outcome, failure) == klass


# ------------------------------------------------------------ run records


def _candidates(mode: str, node_set: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "node_id": node_set[0],
            "mode": mode,
            "rank": 1,
            "node_set": node_set,
            "inactive": node_set[3:],
            "chunk_items": 10,
            "chunks": 30,
            "estimate": {"total_s": 42.5},
        },
        {"node_id": "n9", "mode": "offload", "rank": 2, "estimate": {"total_s": 99.0}},
    ]


def _facts(mode: str = "split", **kw: Any) -> SplitFacts:
    node_set = ["n1", "n2", "n3"] if mode == "split" else ["n2"]
    cands = _candidates(mode, node_set)
    chosen = chosen_row(cands)
    assert chosen is not None
    base: dict[str, Any] = {
        "items": 300,
        "mode": mode,
        "node_set": tuple(node_set),
        "chunk_items": 10,
        "chunks": 30,
        "predicted_s": 42.5,
        "decision_policy": "cost_model",
        "decision_chosen": node_set[0],
        "decision_candidates": cands,
    }
    base.update(kw)
    return SplitFacts(**base)


def _source(role: str, facts: SplitFacts, **kw: Any) -> RecordSource:
    base: dict[str, Any] = {
        "attempt_id": f"att_{role}",
        "job_id": JOB,
        "attempt_no": 1,
        "node_id": "n2",
        "state": "done",
        "created_at": T0,
        "submitter_node_id": "n1",
        "role": role,
        "split": facts,
    }
    base.update(kw)
    return RecordSource(**base)


def test_a_split_jobs_records_carry_its_plan_and_strategy() -> None:
    times = JobTimes(makespan_ms=9000, job_ms=11000, combine_ms=400, ideal_bound_s=7.5)
    facts = _facts(times={"att_combine": times})
    lane = build_record(_source("lane", facts))
    row = build_record(_source("combine", facts))
    for rec in (lane, row):
        assert rec["schema_version"] == 3
        assert rec["strategy"] == "mode3"
        assert rec["split_node_set"] == ["n1", "n2", "n3"]
        assert (rec["split_items"], rec["split_chunk_items"], rec["split_chunks"]) == (
            300,
            10,
            30,
        )
        assert rec["planner_strategy"] == "mode3"
        assert rec["planner_node_set"] == ["n1", "n2", "n3"]
        assert rec["planner_estimate_s"] == 42.5
        assert rec["placement_mode"] == "auto"
    assert lane["attempt_role"] == "lane" and lane["split_job_ms"] is None
    assert row["attempt_role"] == "combine"
    assert (row["split_makespan_ms"], row["split_job_ms"]) == (9000, 11000)
    assert (row["split_combine_ms"], row["split_ideal_bound_s"]) == (400, 7.5)


def test_dont_split_is_local_or_mode1_by_its_lane() -> None:
    facts = _facts("offload")
    assert build_record(_source("lane", facts))["strategy"] == "mode1"
    local = _facts("local", node_set=("n1",))
    assert build_record(_source("lane", local, node_id="n1"))["strategy"] == "local"
    # no decision found: the node rule, never an invented mode3
    unknown = _facts(mode=None, decision_candidates=())  # type: ignore[arg-type]
    assert build_record(_source("lane", unknown, node_id="n1"))["strategy"] == "local"


def test_inactive_nodes_are_not_lanes() -> None:
    from meshforge.controller.telemetry.split_facts import _active

    row = {"node_set": ["a", "b", "c", "d"], "inactive": ["d"]}
    assert _active(row) == ("a", "b", "c")


def test_a_forced_runs_planner_pick_is_its_shadow() -> None:
    facts = _facts(decision_shadow="n2", decision_shadow_set=("n2", "n3"))
    forced = {"mode": "auto", "node_set": ["n1", "n2", "n3"]}
    rec = build_record(_source("lane", facts, placement=forced))
    assert rec["placement_mode"] == "pinned"
    assert rec["strategy"] == "mode3"
    assert rec["planner_node_id"] == "n2"
    assert (rec["planner_strategy"], rec["planner_node_set"]) == ("mode3", ["n2", "n3"])
    one = _facts(decision_shadow="n1")
    rec = build_record(_source("lane", one, placement={"node_id": "n2"}))
    assert (rec["planner_strategy"], rec["planner_node_set"]) == ("local", None)
    none = _facts(decision_shadow=None)
    rec = build_record(_source("lane", none, placement={"node_id": "n2"}))
    assert (rec["planner_strategy"], rec["planner_node_set"]) == (None, None)


def test_whole_run_planner_strategy_and_estimate_skip_set_rows() -> None:
    cands = [
        {"node_id": "n2", "mode": "split", "rank": 1, "estimate": {"total_s": 5.0}},
        {"node_id": "n2", "mode": "offload", "rank": 2, "estimate": {"total_s": 8.0}},
    ]
    rec = build_record(
        RecordSource(
            attempt_id="att_1",
            job_id=JOB,
            attempt_no=1,
            node_id="n2",
            state="done",
            created_at=T0,
            submitter_node_id="n1",
            decision_chosen="n2",
            decision_candidates=cands,
        )
    )
    assert rec["planner_estimate_s"] == 8.0  # D280: never the set row
    assert (rec["planner_strategy"], rec["planner_node_set"]) == ("mode1", None)
    assert rec["attempt_role"] is None and rec["split_items"] is None


def test_a_split_benchmark_jobs_identity_and_status() -> None:
    facts = _facts(times={"att_combine": JobTimes(job_ms=5000)})
    campaign = {
        "campaign_id": "camp_1",
        "group_id": "grp_1",
        "config_id": "cfg_1",
        "repetition": 1,
        "family": "split.hash_files",
        "params": {"files": 64, "size_kb": 256, "rounds": 100, "bogus": 1},
        "seed": 7,
    }
    row = build_record(_source("combine", facts, campaign=campaign))
    lane = build_record(_source("lane", facts, campaign=campaign))
    assert row["workload_family"] == lane["workload_family"] == "split.hash_files"
    assert row["workload_params"] == {"files": 64, "size_kb": 256, "rounds": 100}
    assert row["workload_seed"] == 7
    assert row["workload_status"] == "ok"  # the job row: every chunk checked
    assert lane["workload_status"] is None
    failed = build_record(
        _source("combine", _facts(), campaign=campaign, state="failed")
    )
    assert failed["workload_status"] is None and failed["failure_class"] != "ok"


def test_ideal_bound() -> None:
    # two nodes at 10 and 30 items/s (compute), 400 items -> 10 s, + 2 s start
    # (the slowest lane) + 0.5 s combine
    bound = ideal_bound_s(400, [(100, 10_000), (300, 10_000)], [1500, 2000], 500)
    assert bound == pytest.approx(12.5)
    assert ideal_bound_s(400, [], [1000], 500) is None
    assert ideal_bound_s(400, [(10, 0)], [1000], 500) is None
    assert ideal_bound_s(None, [(10, 10)], [], 0) is None
    assert ideal_bound_s(400, [(400, 1000)], [], None) is None


# ------------------------------------------------------------ labels


def _label_row(config: str, rep: int, job_ms: int, strategy: str) -> dict[str, Any]:
    return {
        "attempt_id": f"att_{config}_{rep}",
        "attempt_no": 1,
        "attempt_created_at": T0 + dt.timedelta(minutes=rep),
        "group_id": "grp_1",
        "config_id": config,
        "repetition": rep,
        "attempt_role": "combine",
        "strategy": strategy,
        "node_id": "n1",
        "split_job_ms": job_ms,
        "total_ms": 3,  # the combine's own phases: never the cost
    }


def test_labels_compare_mode1_and_mode3_by_job_time() -> None:
    runs = [
        PlannedRunState(
            group_id="grp_1",
            config_id=c,
            repetition=r,
            family="split.montecarlo_range",
            params={},
            condition="none",
            strategy=s,
            node_id="n1",
            origin_node_id="n1",
            state="done",
            recorded=True,
        )
        for c, s in (("cfg_m1", "mode1"), ("cfg_m3", "mode3"))
        for r in (1, 2, 3)
    ]
    rows = [_label_row("cfg_m1", r, 30_000 + 50 * r, "mode1") for r in (1, 2, 3)]
    rows += [_label_row("cfg_m3", r, 12_000 + 50 * r, "mode3") for r in (1, 2, 3)]
    assert metric_value(rows[0], "total_ms") == 30_050
    labels = build_labels("camp_1", runs, rows)
    (group,) = labels.groups
    assert group.label == "best" and group.best_strategy == "mode3"
    assert group.margin_pct is not None and group.margin_pct > 100


# ------------------------------------------------------------ campaigns


NODES = [NodeRef("n1", "a"), NodeRef("n2", "b"), NodeRef("n3", "c")]


def _spec(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "g8",
        "seed": 3,
        "families": [{"family": "split.montecarlo_range"}],
        "strategies": ["mode1", "mode3"],
        "origin": "a",
        "repetitions": 3,
        "budget": {"max_runs": 1000},
    }
    base.update(kw)
    return base


def test_mode3_expands_to_the_set_and_mode1_to_every_other_node() -> None:
    spec = parse_spec(_spec())
    runs = expand(spec, NODES).runs
    m3 = [r for r in runs if r.strategy == "mode3"]
    m1 = [r for r in runs if r.strategy == "mode1"]
    assert len(m3) == 3 and len(m1) == 6  # 1 set + 2 nodes, 3 reps
    assert all(r.node_set == ("n1", "n2", "n3") and r.node_id == "n1" for r in m3)
    assert all(r.node_set == () for r in m1)
    assert run_dict(m3[0])["node_set"] == ["n1", "n2", "n3"]
    assert "node_set" not in run_dict(m1[0])
    assert runs_json(runs) == runs_json(expand(spec, NODES).runs)  # deterministic


def test_named_sets_and_their_rules() -> None:
    spec = parse_spec(_spec(mode3_sets=[["a", "b"], ["c", "b", "a"]]))
    m3 = [r for r in expand(spec, NODES).runs if r.strategy == "mode3"]
    assert {r.node_set for r in m3} == {("n1", "n2"), ("n1", "n2", "n3")}
    with pytest.raises(CampaignSpecError, match="two different nodes"):
        expand(parse_spec(_spec(mode3_sets=[["a", "n1"]])), NODES)
    with pytest.raises(CampaignSpecError, match="not one of"):
        expand(parse_spec(_spec(nodes=["a", "b"], mode3_sets=[["a", "c"]])), NODES)
    with pytest.raises(CampaignSpecError, match="listed twice"):
        parse_spec(_spec(mode3_sets=[["a", "a"]]))
    with pytest.raises(CampaignSpecError, match="only with strategy mode3"):
        parse_spec(_spec(strategies=["mode1"], mode3_sets=[["a", "b"]]))
    with pytest.raises(CampaignSpecError, match="needs a split family"):
        parse_spec(_spec(families=[{"family": "cpu.primes"}]))
    with pytest.raises(CampaignSpecError, match="condition none only"):
        parse_spec(_spec(conditions=["none", "cpu_load:1"]))


def test_mode3_applies_to_split_families_only_and_old_ids_are_unchanged() -> None:
    mixed = parse_spec(
        _spec(
            families=[{"family": "split.montecarlo_range"}, {"family": "cpu.primes"}],
        )
    )
    runs = expand(mixed, NODES).runs
    assert {r.family for r in runs if r.strategy == "mode3"} == {
        "split.montecarlo_range"
    }
    # a pre-V0.10 spec: same digest and config ids as before (no node_set key)
    old = parse_spec(_spec(families=[{"family": "cpu.primes"}], strategies=["mode1"]))
    first = expand(old, NODES).runs[0]
    assert "node_set" not in run_dict(first)
    assert spec_digest(old).startswith("sha256:")


def test_a_one_node_campaign_has_no_mode3_set() -> None:
    with pytest.raises(CampaignSpecError, match="at least two nodes"):
        expand(parse_spec(_spec(origin=None, nodes=["a"])), NODES)


# ------------------------------------------------------------ planner


def test_a_forced_set_runs_over_every_node_even_against_the_margin() -> None:
    """Tiny items: the planner would not split; the campaign forces it."""
    history = History(item_rate_by_node={"n1": 3000, "n2": 3000, "n3": 3000})
    free = plan(request(history=history), trio(), "cost_model")
    assert free.split is not None and not free.split.split
    forced = plan(
        request(history=history, node_set=("n1", "n2", "n3")), trio(), "cost_model"
    )
    assert forced.chosen is not None and forced.chosen.mode == "split"
    assert forced.split is not None and forced.split.split
    assert set(forced.split.node_ids) == {"n1", "n2", "n3"}
    assert forced.split.margin.forced == FORCED_SET
    assert not forced.split.margin.passed  # the test still ran and is shown


def test_a_forced_set_waits_when_a_node_cannot_take_a_lane() -> None:
    history = History(item_rate_by_node={"n1": 10, "n2": 10, "n3": 10})
    snapshot = trio()
    busy = type(snapshot)(
        nodes=(node("n1"), node("n2"), node("n3", connected=False)),
        config=snapshot.config,
    )
    decision = plan(
        request(history=history, node_set=("n1", "n2", "n3")), busy, "cost_model"
    )
    assert decision.chosen is None and decision.split is None
    assert any(
        c.rejection is not None and "forced node set" in c.rejection.text
        for c in decision.candidates
    )
    # never a different set or a single node
    assert all(c.rank is None for c in decision.candidates)


def test_a_forced_subset_ignores_other_nodes() -> None:
    history = History(item_rate_by_node={"n1": 10, "n2": 10, "n3": 1000})
    decision = plan(
        request(history=history, node_set=("n1", "n2")), trio(), "cost_model"
    )
    assert decision.split is not None
    assert set(decision.split.node_ids) == {"n1", "n2"}
    assert all(c.node_id != "n3" for c in decision.candidates)


# ------------------------------------------------------------ messages


def _result(**kw: Any) -> agent_pb2.ChunkResult:
    msg = agent_pb2.ChunkResult(
        job_id="job_01J0000000000000000000000A",
        attempt_id="att_01J0000000000000000000000A",
        fencing_token=1,
        chunk_index=0,
        item_id=1,
        lease_seq=1,
        outcome=agent_pb2.ChunkResult.OUTCOME_DONE,
        result_digest="sha256:" + "a" * 64,
    )
    for key, value in kw.items():
        setattr(msg, key, value)
    return msg


def test_chunk_result_tail_and_input_bytes_are_bounded() -> None:
    ok = parse_chunk_result(_result(stderr_tail=b"x" * 10, input_bytes=99))
    assert not isinstance(ok, str)
    assert ok.stderr_tail == b"x" * 10 and ok.input_bytes == 99
    plain = parse_chunk_result(_result())
    assert not isinstance(plain, str)
    assert plain.stderr_tail == b"" and plain.input_bytes is None
    too_big = parse_chunk_result(
        _result(stderr_tail=b"x" * (MAX_STDERR_TAIL_BYTES + 1))
    )
    assert isinstance(too_big, str) and "stderr_tail" in too_big
    huge = parse_chunk_result(_result(input_bytes=2**63))
    assert isinstance(huge, str) and "input_bytes" in huge
    released = parse_chunk_result(
        _result(
            outcome=agent_pb2.ChunkResult.OUTCOME_RELEASED,
            result_digest="",
            stderr_tail=b"x",
        )
    )
    assert isinstance(released, str)


# ------------------------------------------------------------ speculation cache


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_times_cache_reuses_until_a_commit_or_the_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meshforge.controller.split import speculation

    reads: list[int] = []

    async def fake_history(_db: Any, queue_id: int) -> dict[str, SiteTime]:
        reads.append(queue_id)
        return {"n1": SiteTime(0.5, 0.0, "history", 3)}

    monkeypatch.setattr(speculation, "_history", fake_history)
    clock = _Clock()
    cache = SiteTimesCache(ttl_s=5.0, entries=2, clock=clock)
    for _ in range(50):
        got, _entry = await cache.history(None, 1, 4)  # type: ignore[arg-type]
        got["mutated"] = SiteTime(1, 1, "x")  # a caller's change never leaks in
    assert reads == [1] and cache.hits == 49
    await cache.history(None, 1, 5)  # type: ignore[arg-type]  # a commit
    assert reads == [1, 1]
    clock.now = 6.0  # past the TTL
    await cache.history(None, 1, 5)  # type: ignore[arg-type]
    assert reads == [1, 1, 1]
    for queue in (2, 3, 4):
        await cache.history(None, queue, 0)  # type: ignore[arg-type]
    assert len(cache) == 2  # bounded (LRU)
    off = SiteTimesCache(ttl_s=0.0, entries=4)
    for _ in range(3):
        await off.history(None, 9, 1)  # type: ignore[arg-type]
    assert reads.count(9) == 3 and len(off) == 0
    unknown = SiteTimesCache(ttl_s=5.0, entries=4)
    await unknown.history(None, 8, None)  # type: ignore[arg-type]
    await unknown.history(None, 8, None)  # type: ignore[arg-type]
    assert reads.count(8) == 2  # no committed count: never cached
    with pytest.raises(ValueError):
        SiteTimesCache(ttl_s=-1, entries=1)


def test_speculation_config_validates_the_cache_knobs() -> None:
    from meshforge.controller.split.speculation import SpeculationConfig

    with pytest.raises(ValueError):
        SpeculationConfig(cache_s=float("nan"))
    with pytest.raises(ValueError):
        SpeculationConfig(cache_jobs=0)


# ------------------------------------------------------------ agent tail


def test_agent_tail_is_the_bounded_end_of_the_redacted_stream() -> None:
    pytest.importorskip("psutil", reason="needs the agent extra")
    from meshforge.agent.execution.failure_evidence import OutputTail
    from meshforge.agent.execution.lane_chunk import MAX_STDERR_TAIL_BYTES as CAP
    from meshforge.agent.execution.lane_chunk import _tail_of

    tail = OutputTail(CAP)
    tail.add(b"a" * 100_000 + b"END")
    assert _tail_of(tail, 10) == b"aaaaaaaEND"
    assert len(_tail_of(tail, 10**9)) == CAP
    assert _tail_of(tail, 0) == b"" and _tail_of(None, 10) == b""
