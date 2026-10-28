# ruff: noqa: E501, PT006
"""V1.0 G1 tester: adversarial tests of the soak analyzer (D410/D411).

Derived from the success criteria, not the implementation: a real leak must
FAIL however the restarts are arranged, a warm-up plateau must PASS, missing
data is never a PASS, attribution has honest edges, and the report CLI must
survive damaged raw files without a traceback and without a false PASS.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import soak_world as w

import soak
from _soak import jsonl, rules
from _soak.inputs import Note
from _soak.plan import PLAN_FILE, VERDICT_FILE, write_json

pytestmark = pytest.mark.unit
MIB = w.MIB
AGENT = {"agent": ("agent", "L2")}


def _agent(**kw: Any) -> dict[str, list[dict[str, Any]]]:
    s = w.healthy_samples()
    s["L2"] = w.sampler("L2", AGENT, **kw)
    return s


def _mem_row(samples: dict[str, Any]) -> dict[str, Any]:
    rule = rules.rule_memory(w.world(samples=samples))
    return next(r for r in rule["details"] if r["series"] == "L2/agent")


def _hrow(samples: dict[str, Any], metric: str = "fds") -> dict[str, Any]:
    rule = rules.rule_handles(w.world(samples=samples))
    return next(
        r
        for r in rule["details"]
        if r["series"] == "L2/agent" and r["metric"] == metric
    )


# ---------------------------------------------------------------- memory
def test_leak_hidden_by_three_restarts_still_fails() -> None:
    """Sawtooth: restart every 20 min, each lifetime leaks 30 MiB/h."""
    row = _mem_row(
        _agent(
            rss={"agent": lambda t: (100 + 30 * (t % 1200) / 3600) * MIB},
            pids={"agent": lambda t: 100 + int(t // 1200)},
        )
    )
    assert row["lifetimes"] >= 3
    assert row["verdict"] == rules.FAIL, row


def test_leak_hidden_by_very_frequent_restarts_is_never_a_pass() -> None:
    """Restart every 5 min (shorter than useful): may be INCONCLUSIVE, must
    not be PASS - 200 MiB/h leak."""
    row = _mem_row(
        _agent(
            rss={"agent": lambda t: (100 + 200 * (t % 300) / 3600) * MIB},
            pids={"agent": lambda t: 100 + int(t // 300)},
        )
    )
    assert row["verdict"] != rules.PASS, row


def test_all_lifetimes_shorter_than_warmup_is_inconclusive() -> None:
    row = _mem_row(
        _agent(
            rss={"agent": lambda t: 100 * MIB},
            pids={"agent": lambda t: 100 + int(t // 50)},
        )
    )
    assert row["verdict"] == rules.INCONCLUSIVE, row


def test_warmup_plateau_with_noise_passes() -> None:
    """Ramp 60 -> 100 MiB during the plan warm-up (first minute), then +-1 MiB noise."""

    def rss(t: float) -> float:
        base = 60 + 40 * min(t, 60) / 60
        return (base + (1 if int(t / 30) % 2 else -1)) * MIB

    row = _mem_row(_agent(rss={"agent": rss}))
    assert row["verdict"] == rules.PASS, row


def test_leak_straddling_a_sampler_gap_still_fails() -> None:
    """Sampler killed for 20 min in the middle: ticks missing, leak persists."""
    s = _agent(rss={"agent": lambda t: (100 + 40 * t / 3600) * MIB})
    s["L2"] = [
        r for r in s["L2"] if r.get("type") != "tick" or not 1200 < _toff(r) < 2400
    ]
    row = _mem_row(s)
    assert row["verdict"] == rules.FAIL, row


def _toff(tick: dict[str, Any]) -> float:
    from _soak.stats import parse_ts

    return (parse_ts(tick["at"]) - w.T0).total_seconds()  # type: ignore[operator]


def test_gap_covering_quiet_two_is_inconclusive_not_pass() -> None:
    s = _agent()
    s["L2"] = [r for r in s["L2"] if r.get("type") != "tick" or _toff(r) < 3200]
    row = _mem_row(s)
    assert row["verdict"] == rules.INCONCLUSIVE, row


def test_pid_reuse_with_new_create_time_counts_as_a_restart() -> None:
    s = _agent(rss={"agent": lambda t: (100 + 30 * (t % 1800) / 3600) * MIB})
    for r in s["L2"]:
        if r.get("type") == "tick" and _toff(r) >= 1800:
            r["procs"]["agent"]["create_time"] = 999999.0  # same pid, new process
    row = _mem_row(s)
    assert row["lifetimes"] == 2
    assert row["verdict"] == rules.FAIL, row


def test_leak_only_in_the_long_lifetime_other_short_lifetime_flat() -> None:
    """Long lifetime (0-3000 s) leaks 25 MiB/h; restart at 3000 s with a flat
    process. quiet-2 (post restart) looks like quiet-1; must still FAIL."""
    row = _mem_row(
        _agent(
            rss={"agent": lambda t: (100 + (25 * t / 3600 if t < 3000 else 0)) * MIB},
            pids={"agent": lambda t: 100 if t < 3000 else 101},
        )
    )
    assert row["verdict"] == rules.FAIL, row


def test_single_sample_spike_in_flat_series_is_not_a_leak() -> None:
    """One GC/alloc spike (+40 MiB) in one sample near the end of a flat run:
    a robust judge should not call this a leak (median based growth is 0)."""
    s = _agent(rss={"agent": lambda t: (140 if 3500 <= t < 3530 else 100) * MIB})
    row = _mem_row(s)
    assert row["growth_pct"] == 0.0
    assert row["verdict"] == rules.PASS, row


def test_decreasing_memory_passes() -> None:
    row = _mem_row(_agent(rss={"agent": lambda t: (200 - 20 * t / 3600) * MIB}))
    assert row["verdict"] == rules.PASS, row


def test_memory_with_no_samples_at_all_is_not_pass() -> None:
    rule = rules.rule_memory(w.world(samples={}))
    assert rule["verdict"] != rules.PASS


def test_zero_rss_ticks_are_ignored_not_crashing() -> None:
    s = _agent(rss={"agent": lambda t: 0.0 if t % 90 == 0 else 100 * MIB})
    assert _mem_row(s)["verdict"] in (rules.PASS, rules.INCONCLUSIVE)


def test_garbage_tick_records_do_not_crash_the_rules() -> None:
    s = _agent()
    s["L2"] += [
        {"type": "tick", "at": "not a date", "procs": {}},
        {"type": "tick", "at": w.at(100), "procs": {"agent": "x"}},
        {"type": "tick", "at": w.at(100), "procs": None},
        {"type": "event", "kind": "process_restarted"},
        {"type": "wat"},
    ]
    out = rules.evaluate(w.world(samples=s), generated_at=w.T0, meta={})
    assert out["verdict"] in ("PASS", "FAIL", "INCONCLUSIVE")


# --------------------------------------------------------------- handles
def test_fd_leak_hidden_by_restarts_every_20_min_fails() -> None:
    row = _hrow(
        _agent(
            fds={"agent": lambda t: 40 + (t % 1200) / 20},
            pids={"agent": lambda t: 100 + int(t // 1200)},
        )
    )
    assert row["verdict"] == rules.FAIL, row


def test_thread_ramp_within_warmup_then_flat_passes() -> None:
    row = _hrow(_agent(threads={"agent": lambda t: 12 + min(t, 60) / 3}), "threads")
    assert row["verdict"] == rules.PASS, row


def test_thread_leak_fails() -> None:
    row = _hrow(_agent(threads={"agent": lambda t: 12 + t / 60}), "threads")
    assert row["verdict"] == rules.FAIL, row


def test_fd_zero_baseline_one_extra_tolerated_ten_not() -> None:
    ok = _hrow(_agent(fds={"agent": lambda t: 0 if t < 1800 else 1}))
    assert ok["verdict"] == rules.PASS, ok
    bad = _hrow(_agent(fds={"agent": lambda t: 0 if t < 1800 else 10}))
    assert bad["verdict"] == rules.FAIL, bad


def test_handles_none_on_windows_is_inconclusive() -> None:
    s = w.healthy_samples()
    for r in s["L3"]:
        if r.get("type") == "tick":
            r["procs"]["agent"]["handles"] = None
    rule = rules.rule_handles(w.world(samples=s))
    rows = [
        r
        for r in rule["details"]
        if r["series"] == "L3/agent" and r["metric"] == "handles"
    ]
    assert rows[0]["verdict"] == rules.INCONCLUSIVE


# ------------------------------------------------------------- lost jobs
def _lj(spec: Any, view: Any, actions: Any = None, **kw: Any) -> dict[str, Any]:
    world = w.world(jobs=[(spec, view)], actions=actions, **kw)
    return rules.rule_lost_jobs(world, rules.Attributor(world))


def _exh(
    job: str, end_s: float, failure: str = "node_lost", node: str = "node_L2"
) -> Any:
    attempts = [
        {"attempt_id": f"{job}-{i}", "node_id": node, "state": "lost", "role": "run",
         "ended_at": w.at(end_s)}
        for i in range(5)
    ]  # fmt: skip
    return w.view(job, state="failed", failure=failure, end_s=end_s, attempts=attempts)


def test_attribution_window_edges_kill_agent() -> None:
    # kill_agent: started 1000, instant, grace 300 -> explains up to 1300
    inside = _lj(
        w.spec(0), _exh("job0", 1300), [w.action("A", "kill_agent", "L2", 1000)]
    )
    assert inside["verdict"] == rules.PASS
    outside = _lj(
        w.spec(0), _exh("job0", 1301), [w.action("A", "kill_agent", "L2", 1000)]
    )
    assert outside["verdict"] == rules.FAIL
    before = _lj(
        w.spec(0), _exh("job0", 960), [w.action("A", "kill_agent", "L2", 1000)]
    )
    assert before["verdict"] == rules.FAIL  # failure 40 s BEFORE the cause (skew 30 s)


def test_failure_on_a_node_with_unknown_host_is_not_blamed_on_any_chaos() -> None:
    r = _lj(
        w.spec(0),
        _exh("job0", 1000, node="node_mystery"),
        [w.action("A", "kill_agent", "L2", 990)],
    )
    # unknown node: cannot be attributed to L2 -> must not silently pass
    assert r["verdict"] == rules.FAIL, r["details"]


def test_non_fails_jobs_chaos_does_not_explain_a_failed_job() -> None:
    # kill_controller restarts procs; netem/net_down fails_jobs True; cancel_mode3 False
    r = _lj(
        w.spec(0),
        _exh("job0", 1000),
        [w.action("A", "cancel_mode3", "L1", 990, job_id="other")],
    )
    assert r["verdict"] == rules.FAIL


def test_note_covering_host_explains_failure_but_other_host_note_does_not() -> None:
    n_ok = Note(
        w.T0 + timedelta(seconds=900), w.T0 + timedelta(seconds=1100), "L2", "wifi"
    )
    n_bad = Note(
        w.T0 + timedelta(seconds=900), w.T0 + timedelta(seconds=1100), "L3", "wifi"
    )
    assert _lj(w.spec(0), _exh("job0", 1000), notes=[n_ok])["verdict"] == rules.PASS
    assert _lj(w.spec(0), _exh("job0", 1000), notes=[n_bad])["verdict"] == rules.FAIL


def test_running_job_at_collection_is_lost() -> None:
    r = _lj(w.spec(0), w.view("job0", state="running"))
    assert r["verdict"] == rules.FAIL
    assert r["details"]["lost"][0]["reason"] == "not_terminal"


def test_succeeded_with_two_commits_or_no_digest_is_lost() -> None:
    v = w.view("job0")
    v["output_digest"] = None
    assert _lj(w.spec(0), v)["details"]["lost"][0]["reason"] == "no_output"
    v2 = w.view(
        "job0",
        attempts=[
            {
                "attempt_id": "a1",
                "node_id": "node_L2",
                "state": "done",
                "role": "run",
                "ended_at": w.at(5),
            },
            {
                "attempt_id": "a2",
                "node_id": "node_L3",
                "state": "done",
                "role": "run",
                "ended_at": w.at(6),
            },
        ],
    )
    assert (
        _lj(w.spec(0), v2)["details"]["lost"][0]["reason"]
        == "duplicate_or_missing_commit"
    )


def test_no_jobs_at_all_is_not_pass() -> None:
    world = w.world(jobs=[])
    assert rules.rule_lost_jobs(world, rules.Attributor(world))["verdict"] != rules.PASS


def test_late_job_exact_boundary() -> None:
    spec = w.spec(0)
    dl = spec.deadline_s
    ok = _lj(spec, w.view("job0", end_s=dl + rules.LATE_AFTER_DEADLINE_S))
    assert not ok["details"]["late"]
    late = _lj(spec, w.view("job0", end_s=dl + rules.LATE_AFTER_DEADLINE_S + 5))
    assert late["details"]["late"] and late["verdict"] == rules.FAIL


def test_view_with_malformed_transitions_does_not_crash() -> None:
    v = w.view("job0")
    v["transitions"] = [None, {"at": "garbage"}, "x"]
    try:
        r = _lj(w.spec(0), v)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"lost-jobs rule crashed on malformed transitions: {exc!r}")
    assert r["verdict"] in ("PASS", "FAIL", "INCONCLUSIVE")


# --------------------------------------------------- heartbeat / crashes
def _off(node: str, t: float, **kw: Any) -> dict[str, Any]:
    return {"at": w.at(t), "node_id": node, "hostname": "x", "field": "state",
            "old": "online", "new": "offline", **kw}  # fmt: skip


def _hb(
    transitions: list[dict[str, Any]], actions: Any = None, outages: Any = None
) -> dict[str, Any]:
    world = w.world(
        fabric={
            "nodes": {"node_L2": {}, "node_L3": {}},
            "transitions": transitions,
            "outages": outages or [],
            "elapsed_s": 4320.0,
        },
        actions=actions,
    )
    return rules.rule_heartbeat(world, rules.Attributor(world))


def test_offline_one_second_after_grace_is_unexplained() -> None:
    # net_down_short: 30 s duration from 1000, grace 180 -> window end 1210
    act = [w.action("A", "net_down_short", "L2", 1000, 1030)]
    assert _hb([_off("node_L2", 1210)], act)["verdict"] == rules.PASS
    assert _hb([_off("node_L2", 1211)], act)["verdict"] == rules.FAIL


def test_offline_of_other_node_not_explained_by_nodes_chaos() -> None:
    act = [w.action("A", "net_down_short", "L2", 1000, 1030)]
    assert _hb([_off("node_L3", 1010)], act)["verdict"] == rules.FAIL


def test_unparseable_offline_timestamp_is_unexplained_not_crash() -> None:
    r = _hb([{**_off("node_L2", 1), "at": "yesterday-ish"}])
    assert r["verdict"] == rules.FAIL


def test_online_transitions_and_other_fields_are_ignored() -> None:
    online = {**_off("node_L2", 100), "new": "online"}
    other = {**_off("node_L2", 100), "field": "hostname"}
    assert _hb([online, other])["verdict"] == rules.PASS


def test_appeared_offline_counts() -> None:
    r = _hb([{**_off("node_L2", 500), "field": "appeared"}])
    assert r["verdict"] == rules.FAIL


def test_failed_or_skipped_chaos_never_explains_offline() -> None:
    for outcome in ("failed", "skipped", "missed"):
        act = [w.action("A", "net_down_short", "L2", 1000, 1030, outcome=outcome)]
        assert _hb([_off("node_L2", 1010)], act)["verdict"] == rules.FAIL, outcome


def test_unknown_chaos_kind_in_log_explains_nothing_and_does_not_crash() -> None:
    act = [w.action("A", "teleport", "L2", 1000, 1030)]
    assert _hb([_off("node_L2", 1010)], act)["verdict"] == rules.FAIL


def test_restart_event_attribution_needs_matching_role_and_host() -> None:
    def ev(host: str, role: str) -> list[dict[str, Any]]:
        return [{"type": "event", "kind": "process_restarted", "at": w.at(1005),
                 "target": "agent", "role": role, "host": host, "old_pid": 1, "pid": 2}]  # fmt: skip

    def crashes(host: str, role: str, act: Any) -> dict[str, Any]:
        s = w.healthy_samples()
        s["L2"] += ev(host, role)
        world = w.world(samples=s, actions=act)
        return rules.rule_crashes(world, rules.Attributor(world))

    act = [w.action("A", "kill_agent", "L2", 1000)]
    assert crashes("L2", "agent", act)["verdict"] == rules.PASS
    assert crashes("L3", "agent", act)["verdict"] == rules.FAIL
    assert crashes("L2", "controller", act)["verdict"] == rules.FAIL
    assert crashes("L2", "agent", [])["verdict"] == rules.FAIL


def test_missing_then_found_same_target_is_not_never_returned() -> None:
    s = w.healthy_samples()
    s["L2"] += [
        {
            "type": "event",
            "kind": "process_missing",
            "at": w.at(100),
            "target": "agent",
            "role": "agent",
            "host": "L2",
        },
        {
            "type": "event",
            "kind": "process_found",
            "at": w.at(200),
            "target": "agent",
            "role": "agent",
            "host": "L2",
            "pid": 5,
        },
    ]
    world = w.world(samples=s)
    r = rules.rule_crashes(world, rules.Attributor(world))
    assert r["details"]["never_returned"] == []


# ---------------------------------------------------------------- chaos
def test_chaos_count_boundary() -> None:
    # 1 h plan: chaos window = 0.8 h = 48 min -> needs ceil(48/45) = 2
    one = w.world(actions=[w.action("A1", "kill_agent", "L2", 700)])
    assert rules.rule_chaos(one)["verdict"] == rules.FAIL
    two = w.world(
        actions=[
            w.action("A1", "kill_agent", "L2", 700),
            w.action("A2", "kill_agent", "L2", 1700),
        ]
    )
    assert rules.rule_chaos(two)["verdict"] == rules.PASS


@pytest.mark.parametrize("outcome", ["failed", "skipped", "missed", "delegated"])
def test_non_applied_outcomes_do_not_count_as_chaos(outcome: str) -> None:
    acts = [
        w.action(f"A{i}", "kill_agent", "L2", 700 + i, outcome=outcome)
        for i in range(5)
    ]
    assert rules.rule_chaos(w.world(actions=acts))["verdict"] == rules.FAIL


# ------------------------------------------------------- report CLI damage
class _Clock:
    def monotonic(self) -> float:
        return 0.0

    def wall(self) -> Any:
        return w.T0 + timedelta(hours=2)

    def sleep(self, s: float) -> None:
        pass


def _write_run(run: Path) -> None:
    world = w.world()
    write_json(run / PLAN_FILE, world.plan.as_dict())
    for label, records in world.samples.items():
        for r in records:
            jsonl.append(run / f"samples.{label.lower()}.jsonl", r)
    for r in w.ledger_records(
        [(s.seq, f"job{s.seq}", s.offset_s) for s in world.specs]
    ):
        jsonl.append(run / "ledger.jsonl", r)
    write_json(run / "jobs-final.json", world.final)


def _report(run: Path, capsys: Any) -> tuple[int, str]:
    code = soak.main(
        ["report", str(run), "--md", str(run.parent / "r.md")], clock=_Clock()
    )
    return code, capsys.readouterr().out


def test_torn_tail_in_samples_is_reported_as_problem_and_never_pass(
    tmp_path: Path, capsys: Any
) -> None:
    run = tmp_path / "run"
    _write_run(run)
    with (run / "samples.l2.jsonl").open("ab") as fh:
        fh.write(b'{"type":"tick","at":"2026-11-02T10:')
    code, _ = _report(run, capsys)
    assert code in (soak.EXIT_FAIL, soak.EXIT_INCONCLUSIVE)
    verdict = json.loads((run / VERDICT_FILE).read_text())
    assert any("torn" in p for p in verdict["problems"])


@pytest.mark.parametrize(
    "victim,payload",
    [
        ("plan.json", b"{not json"),
        ("plan.json", b""),
        ("plan.json", b"[]"),
        ("jobs-final.json", b'{"jobs": '),
        ("jobs-final.json", b"null"),
        ("jobs-final.json", b'{"jobs": []}'),
        ("jobs-final.json", b'{"jobs": {"job0": 5}}'),
        (
            "ledger.jsonl",
            b'{"type":"intent","seq":1}\nGARBAGE\n{"type":"intent","seq":2}\n',
        ),
        ("ledger.jsonl", b"\xff\xfe\x00garbage\n"),
        ("samples.l2.jsonl", b'{"type":"header"}\n[1,2]\n{"type":"tick"}\n'),
        ("samples.l2.jsonl", b""),
        ("notes.json", b"{oops"),
        ("notes.json", b'{"events": [1, "x", null]}'),
        ("notes.json", b'{"events": "not a list"}'),
        ("notes.json", b'{"events": [{"from": "bad", "to": "worse"}]}'),
        ("notes.json", b"[]"),
        (
            "chaos-log.jsonl",
            b'{"type":"end"}\n{"type":"end","id":1,"started_at":"x"}\n',
        ),
        (
            "chaos-log.jsonl",
            b'{"type":"end","id":"A","kind":"kill_agent","target":"L2","started_at":"2026-11-02T09:10:00+00:00"}\n',
        ),
        ("fabric-soak.checkpoint.json", b"{broken"),
        ("fabric-soak.checkpoint.json", b'{"kind":"wrong"}'),
    ],
)
def test_report_on_damaged_raw_never_traceback_never_pass(
    tmp_path: Path, capsys: Any, victim: str, payload: bytes
) -> None:
    run = tmp_path / "run"
    _write_run(run)
    (run / victim).write_bytes(payload)
    try:
        code, out = _report(run, capsys)
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"report crashed on {victim}={payload!r}: {exc!r}")
    assert code != soak.EXIT_OK, out
    assert code in (
        soak.EXIT_IO,
        soak.EXIT_FAIL,
        soak.EXIT_INCONCLUSIVE,
        soak.EXIT_USAGE,
    )


def test_report_on_empty_folder_and_missing_folder(tmp_path: Path) -> None:
    assert soak.main(["report", str(tmp_path)], clock=_Clock()) == soak.EXIT_IO
    assert soak.main(["report", str(tmp_path / "nope")], clock=_Clock()) in (
        soak.EXIT_IO,
        soak.EXIT_USAGE,
    )


def test_report_markdown_renders_hostile_text(tmp_path: Path, capsys: Any) -> None:
    run = tmp_path / "run"
    _write_run(run)
    write_json(
        run / "notes.json",
        {
            "events": [
                {
                    "from": w.at(0),
                    "to": w.at(10),
                    "host": "L2",
                    "reason": "a|b\nc `x` é中",
                }
            ]
        },
    )
    code, _ = _report(run, capsys)
    assert code != soak.EXIT_IO
    assert (run.parent / "r.md").read_text(encoding="utf-8").startswith("# V1.0 soak")


def test_report_is_idempotent_and_deterministic_apart_from_timestamp(
    tmp_path: Path, capsys: Any
) -> None:
    run = tmp_path / "run"
    _write_run(run)
    _report(run, capsys)
    a = json.loads((run / VERDICT_FILE).read_text())
    _report(run, capsys)
    b = json.loads((run / VERDICT_FILE).read_text())
    a.pop("generated_at"), b.pop("generated_at")
    assert a == b


# ------------------------------------------------- jsonl / sampler resume
def test_append_after_torn_tail_then_load_keeps_good_records(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    jsonl.append(p, {"a": 1})
    with p.open("ab") as fh:
        fh.write(b'{"b": ')
    jsonl.append(p, {"c": 3})
    jsonl.append(p, {"d": 4})
    loaded = jsonl.load(p)
    assert [next(iter(r)) for r in loaded.records] == ["a", "c", "d"]
    assert loaded.torn_tail


def test_jsonl_valid_non_object_line_is_corruption(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text('{"a":1}\n123\n{"b":2}\n')
    with pytest.raises(jsonl.JsonlError):
        jsonl.load(p)


def test_jsonl_nan_is_refused_on_write(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        jsonl.append(tmp_path / "x.jsonl", {"v": float("nan")})
    assert (
        not (tmp_path / "x.jsonl").exists() or (tmp_path / "x.jsonl").read_text() == ""
    )


def test_jsonl_truncated_multibyte_tail(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    jsonl.append(p, {"a": "中"})
    with p.open("ab") as fh:
        fh.write('{"b": "中'.encode()[:-1])
    loaded = jsonl.load(p)
    assert len(loaded.records) == 1 and loaded.torn_tail
