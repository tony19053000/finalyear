"""V1.0 G1 (roadmap 1.0.2, D410): every soak pass rule on synthetic runs.

Each test starts from a healthy 1 h world (``soak_world.world``) and bends
one thing - a leak, a lost job, an unexplained restart - so the rule that must
catch it is the only one that changes.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
import soak_world as w

from _soak import report, rules
from _soak.inputs import Note

pytestmark = pytest.mark.unit
MIB = w.MIB


def _by_id(inputs: Any) -> dict[str, dict[str, Any]]:
    out = rules.evaluate(inputs, generated_at=w.T0, meta={})
    return {r["id"]: r for r in out["rules"]}


# ------------------------------------------------------------- memory (3)
def test_flat_series_passes_memory() -> None:
    rule = rules.rule_memory(w.world())
    assert rule["verdict"] == rules.PASS
    assert all(r["growth_pct"] == 0.0 for r in rule["details"])


def test_synthetic_leak_is_detected() -> None:
    samples = w.healthy_samples()
    # 20 MB/h: a slow leak, 100 -> 120 MiB over the hour
    samples["L2"] = w.sampler(
        "L2",
        {"agent": ("agent", "L2")},
        rss={"agent": lambda t: (100 + 20 * t / 3600) * MIB},
    )
    rule = rules.rule_memory(w.world(samples=samples))
    assert rule["verdict"] == rules.FAIL
    leaky = next(r for r in rule["details"] if r["series"] == "L2/agent")
    assert leaky["verdict"] == rules.FAIL
    assert leaky["slope_mb_h"] == pytest.approx(20 * 1.048576, rel=0.05)
    others = [r for r in rule["details"] if r["series"] != "L2/agent"]
    assert all(r["verdict"] == rules.PASS for r in others)


def test_step_growth_over_ten_percent_fails_even_with_small_slope() -> None:
    samples = w.healthy_samples()
    # flat, then +12 % only in the last quiet window: the fit stays small-ish,
    # the quiet-to-quiet comparison catches it
    samples["L1"] = w.sampler(
        "L1",
        {"controller": ("controller", "L1"), "postgres": ("postgres", "L1")},
        rss={"controller": lambda t: (112 if t >= 3240 else 100) * MIB},
    )
    row = next(
        r for r in rules.rule_memory(w.world(samples=samples))["details"]
        if r["series"] == "L1/controller"
    )  # fmt: skip
    assert row["growth_pct"] == pytest.approx(12.0)
    assert row["verdict"] == rules.FAIL


def test_growth_just_under_bound_with_flat_slope_passes() -> None:
    samples = w.healthy_samples()
    samples["L2"] = w.sampler(
        "L2",
        {"agent": ("agent", "L2")},
        rss={"agent": lambda t: (101 if t >= 1800 else 100) * MIB},
    )
    row = rules.rule_memory(w.world(samples=samples))["details"]
    agent = next(r for r in row if r["series"] == "L2/agent")
    assert agent["growth_pct"] == pytest.approx(1.0)
    assert agent["slope_mb_h"] < rules.RSS_SLOPE_MB_H
    assert agent["verdict"] == rules.PASS


def test_missing_quiet_window_samples_is_inconclusive_not_pass() -> None:
    samples = w.healthy_samples()
    samples["L3"] = [
        r for r in samples["L3"] if r.get("type") != "tick" or r["at"] > w.at(400)
    ]
    rule = rules.rule_memory(w.world(samples=samples))
    assert rule["verdict"] == rules.INCONCLUSIVE


# ----------------------------------------------------------- handles (4)
def test_one_extra_thread_is_tolerated() -> None:
    samples = w.healthy_samples()
    samples["L2"] = w.sampler(
        "L2",
        {"agent": ("agent", "L2")},
        threads={"agent": lambda t: 13 if t > 1800 else 12},
    )
    assert rules.rule_handles(w.world(samples=samples))["verdict"] == rules.PASS


def test_fd_leak_fails_and_windows_uses_handles() -> None:
    samples = w.healthy_samples()
    samples["L3"] = w.sampler(
        "L3",
        {"agent": ("agent", "L3")},
        os_name="nt",
        fds={"agent": lambda t: 400 + t / 10},
    )
    rule = rules.rule_handles(w.world(samples=samples))
    assert rule["verdict"] == rules.FAIL
    bad = [r for r in rule["details"] if r["verdict"] == rules.FAIL]
    assert [(r["series"], r["metric"]) for r in bad] == [("L3/agent", "handles")]


def test_unmeasured_counter_is_inconclusive() -> None:
    samples = w.healthy_samples()
    for r in samples["L1"]:
        if r.get("type") == "tick":
            r["procs"]["postgres"]["fds"] = None  # another user's process, no root
    rule = rules.rule_handles(w.world(samples=samples))
    assert rule["verdict"] == rules.INCONCLUSIVE
    row = next(
        r
        for r in rule["details"]
        if r["series"] == "L1/postgres" and r["metric"] == "fds"
    )
    assert "not measured" in row["note"]


# --------------------------------------------------------- lost jobs (1)
def test_all_jobs_succeeded_passes() -> None:
    rule = rules.rule_lost_jobs(w.world(), rules.Attributor(w.world()))
    assert rule["verdict"] == rules.PASS
    assert rule["details"]["states"] == {"succeeded": 5}


def _one_job(spec: Any, view: Any, **kw: Any) -> dict[str, Any]:
    world = w.world(jobs=[(spec, view)], **kw)
    return rules.rule_lost_jobs(world, rules.Attributor(world))


def test_one_lost_job_fails() -> None:
    jobs = [(w.spec(i), w.view(f"job{i}")) for i in range(4)]
    jobs.append((w.spec(4), w.view("job4", state="running")))
    world = w.world(jobs=jobs)
    rule = rules.rule_lost_jobs(world, rules.Attributor(world))
    assert rule["verdict"] == rules.FAIL
    assert [(x["job_id"], x["reason"]) for x in rule["details"]["lost"]] == [
        ("job4", "not_terminal")
    ]


def test_job_missing_from_controller_is_lost() -> None:
    world = w.world(jobs=[(w.spec(0), None)])
    world.final["jobs"] = {"job0": None}  # type: ignore[index]
    rule = rules.rule_lost_jobs(world, rules.Attributor(world))
    assert rule["details"]["lost"][0]["reason"] == "not_in_controller"


def test_retryable_failure_with_attempts_left_is_lost_even_after_chaos() -> None:
    rule = _one_job(
        w.spec(0),
        w.view("job0", state="failed", failure="node_lost", end_s=1000),
        actions=[w.action("A1", "kill_agent", "L2", 990)],
    )
    assert rule["details"]["lost"][0]["reason"] == "retry_not_done"


def _exhausted(
    job_id: str, failure: str, end_s: float, node: str = "node_L2"
) -> dict[str, Any]:
    attempts = [
        {"attempt_id": f"{job_id}-{i}", "node_id": node, "state": "lost", "role": "run",
         "ended_at": w.at(end_s - 50 + i)}
        for i in range(5)
    ]  # fmt: skip
    return w.view(
        job_id, state="failed", failure=failure, end_s=end_s, attempts=attempts
    )


def test_failure_explained_by_chaos_on_its_node_passes() -> None:
    rule = _one_job(
        w.spec(0),
        _exhausted("job0", "node_lost", 1000),
        actions=[w.action("A1", "kill_agent", "L2", 960)],
    )
    assert rule["verdict"] == rules.PASS
    assert rule["details"]["failed_explained"][0]["by"] == "A1"


def test_failure_after_chaos_on_another_laptop_is_unexplained() -> None:
    rule = _one_job(
        w.spec(0),
        _exhausted("job0", "node_lost", 3000),
        actions=[w.action("A1", "kill_agent", "L3", 960)],
    )
    assert rule["verdict"] == rules.FAIL
    assert rule["details"]["lost"][0]["reason"] == "unexplained_failure"


def test_controller_laptop_chaos_explains_failures_everywhere() -> None:
    rule = _one_job(
        w.spec(0),
        _exhausted("job0", "node_lost", 1000),
        actions=[w.action("A1", "kill_controller", "L1", 990)],
    )
    assert rule["verdict"] == rules.PASS


def test_meant_to_fail_job_failing_is_fine_but_succeeding_is_not() -> None:
    ok = _one_job(
        w.spec(0, exit_code=3), w.view("job0", state="failed", failure="user_error")
    )
    assert ok["verdict"] == rules.PASS
    bad = _one_job(w.spec(0, exit_code=3), w.view("job0"))
    assert bad["details"]["lost"][0]["reason"] == "wrong_outcome"


def test_failed_without_cause_is_lost() -> None:
    rule = _one_job(w.spec(0), w.view("job0", state="failed", failure=None))
    assert rule["details"]["lost"][0]["reason"] == "failed_without_cause"


def test_cancel_must_be_planned_or_chaos() -> None:
    lost = _one_job(w.spec(0), w.view("job0", state="cancelled"))
    assert lost["details"]["lost"][0]["reason"] == "unexplained_cancel"
    chaos = _one_job(
        w.spec(0),
        w.view("job0", state="cancelled"),
        actions=[w.action("A1", "cancel_mode3", "L1", 10, job_id="job0")],
    )
    assert chaos["verdict"] == rules.PASS


def test_planned_cancel_accepts_any_terminal_state() -> None:
    world = w.world(jobs=[(w.spec(0, cancel=5.0), w.view("job0"))])
    world.ledger.entries[0].cancel_status = 409  # it had already ended
    assert rules.rule_lost_jobs(world, rules.Attributor(world))["verdict"] == rules.PASS


def test_job_ending_ten_minutes_after_its_deadline_is_late() -> None:
    # deadline = 600 s timeout x 5 attempts + 25 s backoff = 3025 s
    rule = _one_job(w.spec(0), w.view("job0", submitted_s=0, end_s=3025 + 601))
    assert rule["verdict"] == rules.FAIL
    assert rule["details"]["late"][0]["over_deadline_s"] == pytest.approx(601, abs=1)


def test_two_committed_attempts_fail_and_missing_output_fails() -> None:
    attempts = [
        {"attempt_id": "a1", "node_id": "node_L2", "state": "done", "role": "run"},
        {"attempt_id": "a2", "node_id": "node_L3", "state": "done", "role": "run"},
    ]
    twice = _one_job(w.spec(0), w.view("job0", attempts=attempts))
    assert twice["details"]["lost"][0]["reason"] == "duplicate_or_missing_commit"
    no_output = w.view("job0")
    no_output["output_digest"] = None
    assert _one_job(w.spec(0), no_output)["details"]["lost"][0]["reason"] == "no_output"


def test_bad_output_content_fails() -> None:
    world = w.world(jobs=[(w.spec(0), w.view("job0"))])
    world.final["outputs"] = {"job0": {"checked": True, "ok": False}}  # type: ignore[index]
    rule = rules.rule_lost_jobs(world, rules.Attributor(world))
    assert rule["details"]["lost"][0]["reason"] == "bad_output"


def test_no_collection_is_inconclusive_and_duplicate_submission_fails() -> None:
    assert (
        _one_job(w.spec(0), w.view("job0"), collected=False)["verdict"]
        == rules.INCONCLUSIVE
    )
    world = w.world(jobs=[(w.spec(0), w.view("job0"))])
    world.ledger.problems.append("seq 0: two job ids")
    assert rules.rule_lost_jobs(world, rules.Attributor(world))["verdict"] == rules.FAIL


def test_unresolved_submission_is_inconclusive() -> None:
    world = w.world()
    world.ledger.entry(99).intent_at = w.at(10)
    assert (
        rules.rule_lost_jobs(world, rules.Attributor(world))["verdict"]
        == rules.INCONCLUSIVE
    )


# ------------------------------------------------------------ crashes (2)
def _restart(label: str, target: str, role: str, host: str, t: float) -> dict[str, Any]:
    return {"type": "event", "at": w.at(t), "label": label, "kind": "process_restarted",
            "target": target, "role": role, "host": host, "pid": 200, "old_pid": 100}  # fmt: skip


def _with_events(
    label: str, events: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    samples = w.healthy_samples()
    samples[label] = samples[label] + events
    return samples


def test_restart_caused_by_chaos_is_not_a_crash() -> None:
    world = w.world(
        samples=_with_events("L2", [_restart("L2", "agent", "agent", "L2", 1020)]),
        actions=[w.action("A1", "kill_agent", "L2", 1000)],
    )
    rule = rules.rule_crashes(world, rules.Attributor(world))
    assert rule["verdict"] == rules.PASS
    assert rule["details"]["explained"][0]["by"] == "A1"


def test_unexplained_restart_is_a_crash() -> None:
    world = w.world(
        samples=_with_events("L2", [_restart("L2", "agent", "agent", "L2", 2500)]),
        actions=[w.action("A1", "kill_agent", "L2", 1000)],
    )
    assert rules.rule_crashes(world, rules.Attributor(world))["verdict"] == rules.FAIL


def test_restart_of_a_role_the_action_does_not_restart_is_a_crash() -> None:
    # killing the agent on L1's neighbour does not explain the controller restarting
    world = w.world(
        samples=_with_events(
            "L1", [_restart("L1", "controller", "controller", "L1", 1020)]
        ),
        actions=[w.action("A1", "net_down_short", "L1", 1000, 1030)],
    )
    assert rules.rule_crashes(world, rules.Attributor(world))["verdict"] == rules.FAIL


def test_postgres_stop_explains_controller_restart_and_reboot_explains_all() -> None:
    events = [
        _restart("L1", "postgres", "postgres", "L1", 1010),
        _restart("L1", "controller", "controller", "L1", 1015),
        _restart("L1", "controller", "controller", "L1", 2400),
    ]
    world = w.world(
        samples=_with_events("L1", events),
        actions=[
            w.action("A1", "restart_postgres", "L1", 1000),
            w.action("A2", "reboot", "L1", 2000, 2240),
        ],
    )
    rule = rules.rule_crashes(world, rules.Attributor(world))
    assert rule["verdict"] == rules.PASS
    assert [e["by"] for e in rule["details"]["explained"]] == ["A1", "A1", "A2"]


def test_process_that_never_came_back_fails_and_a_note_explains() -> None:
    gone = {"type": "event", "at": w.at(3000), "label": "L3", "kind": "process_missing",
            "target": "agent", "role": "agent", "host": "L3"}  # fmt: skip
    world = w.world(samples=_with_events("L3", [gone]))
    rule = rules.rule_crashes(world, rules.Attributor(world))
    assert rule["verdict"] == rules.FAIL
    assert rule["details"]["never_returned"][0]["target"] == "agent"
    note = Note(
        w.T0 + timedelta(seconds=2900), w.T0 + timedelta(seconds=3500), "L3", "sleep"
    )
    later = _restart("L3", "agent", "agent", "L3", 3400)
    world = w.world(samples=_with_events("L3", [gone, later]), notes=[note])
    rule = rules.rule_crashes(world, rules.Attributor(world))
    assert rule["verdict"] == rules.PASS
    assert rule["details"]["explained"][0]["by"] == "note:sleep"


def test_failed_chaos_action_explains_nothing() -> None:
    world = w.world(
        samples=_with_events("L2", [_restart("L2", "agent", "agent", "L2", 1020)]),
        actions=[w.action("A1", "kill_agent", "L2", 1000, outcome="failed")],
    )
    assert rules.rule_crashes(world, rules.Attributor(world))["verdict"] == rules.FAIL


# ---------------------------------------------------------- heartbeat (7)
def _fabric(
    transitions: list[dict[str, Any]], outages: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {"nodes": {"node_L2": {}, "node_L3": {}}, "transitions": transitions,
            "outages": outages or [], "elapsed_s": 4320.0}  # fmt: skip


def _offline(node: str, t: float) -> dict[str, Any]:
    return {"at": w.at(t), "node_id": node, "hostname": "x", "field": "state",
            "old": "online", "new": "offline", "during_gap": False}  # fmt: skip


def test_offline_explained_by_chaos_passes_and_unexplained_fails() -> None:
    explained = w.world(
        fabric=_fabric([_offline("node_L2", 1040)]),
        actions=[w.action("A1", "net_down_short", "L2", 1000, 1030)],
    )
    assert (
        rules.rule_heartbeat(explained, rules.Attributor(explained))["verdict"]
        == rules.PASS
    )
    wide = w.world(
        fabric=_fabric([_offline("node_L3", 1040)]),
        actions=[w.action("A1", "kill_controller", "L1", 1000)],
    )
    assert rules.rule_heartbeat(wide, rules.Attributor(wide))["verdict"] == rules.PASS
    alone = w.world(fabric=_fabric([_offline("node_L3", 2000)]))
    rule = rules.rule_heartbeat(alone, rules.Attributor(alone))
    assert rule["verdict"] == rules.FAIL
    assert rule["details"]["unexplained"][0]["host"] == "L3"


def test_unexplained_controller_outage_fails_and_missing_record_is_inconclusive() -> (
    None
):
    world = w.world(fabric=_fabric([], [{"from": w.at(2000), "to": w.at(2030)}]))
    assert rules.rule_heartbeat(world, rules.Attributor(world))["verdict"] == rules.FAIL
    world.fabric = None
    assert (
        rules.rule_heartbeat(world, rules.Attributor(world))["verdict"]
        == rules.INCONCLUSIVE
    )


# ---------------------------------------------------------------- DB (5)
def _db(jobs_per_s: float, bytes_per_job: float, *, extra: Any = None) -> Any:
    def point(t: float) -> dict[str, Any] | None:
        if int(t) % 300:
            return None
        load_t = min(t, 3600.0)
        jobs = int(load_t * jobs_per_s)
        tables = {"job": {"bytes": 0, "live": jobs, "dead": 0},
                  "resource_snapshot": {"bytes": 0, "live": int(t), "dead": 0},
                  "node_session": {"bytes": 0, "live": 3, "dead": 0}}  # fmt: skip
        if extra:
            extra(t, tables)
        return {"ok": True, "db_bytes": int(10 * MIB + jobs * bytes_per_job(t)), "jobs": jobs,
                "tables": tables, "wal_bytes": 48 * MIB, "max_wal_bytes": 1024 * MIB}  # fmt: skip

    return point


def _db_world(point: Any) -> Any:
    samples = w.healthy_samples()
    samples["L1"] = w.sampler(
        "L1",
        {"controller": ("controller", "L1"), "postgres": ("postgres", "L1")},
        db=point,
    )
    # the last job ended at 61 min: the idle tail is 62-72 min
    jobs = [(w.spec(0), w.view("job0", end_s=3660))]
    return w.world(samples=samples, jobs=jobs)


def test_db_growth_proportional_to_jobs_passes() -> None:
    rule = rules.rule_db(_db_world(_db(0.1, lambda _t: 40_000)))
    assert rule["verdict"] == rules.PASS, rule["details"]
    assert (
        rule["details"]["idle_tail"]["time_driven_rows_per_h"]["resource_snapshot"] > 0
    )


def test_db_bytes_per_job_doubling_fails() -> None:
    rule = rules.rule_db(_db_world(_db(0.1, lambda t: 400_000 if t > 1800 else 40_000)))
    assert rule["details"]["bytes_per_job"]["verdict"] == rules.FAIL
    assert rule["verdict"] == rules.FAIL


def test_table_growing_without_jobs_fails() -> None:
    def sessions(t: float, tables: dict[str, Any]) -> None:
        tables["node_session"]["live"] = 3 + int(t // 300)

    rule = rules.rule_db(_db_world(_db(0.1, lambda _t: 40_000, extra=sessions)))
    assert rule["details"]["idle_tail"]["grew_without_jobs"] == {
        "node_session": 1
    }  # samples at 65 and 70 min
    assert rule["verdict"] == rules.FAIL


def test_wal_over_bound_fails_and_few_samples_inconclusive() -> None:
    def wal(t: float) -> dict[str, Any] | None:
        point = _db(0.1, lambda _t: 40_000)(t)
        if point is not None:
            point["wal_bytes"] = 2048 * MIB
        return point

    assert rules.rule_db(_db_world(wal))["details"]["wal"]["verdict"] == rules.FAIL
    one = rules.rule_db(
        _db_world(lambda t: _db(0.1, lambda _t: 1)(t) if t == 0 else None)
    )
    assert one["verdict"] == rules.INCONCLUSIVE


# --------------------------------------------------------------- disk (6)
def test_cache_over_cap_and_growing_logs_fail() -> None:
    samples = w.healthy_samples()
    samples["L2"] = w.sampler(
        "L2",
        {"agent": ("agent", "L2")},
        dirs={
            "cache": (lambda t: 900 * MIB if t > 2000 else 100 * MIB, 512 * MIB),
            "logs": (lambda t: t * 100_000.0, None),
        },
    )
    rule = rules.rule_disk(w.world(samples=samples))
    verdicts = {r["series"]: r["verdict"] for r in rule["details"]}
    assert verdicts == {"L2/cache": rules.FAIL, "L2/logs": rules.FAIL}
    samples["L2"] = w.sampler(
        "L2",
        {"agent": ("agent", "L2")},
        dirs={
            "cache": (lambda _t: 100 * MIB, 512 * MIB),
            "logs": (lambda _t: 5 * MIB, None),
        },
    )
    assert rules.rule_disk(w.world(samples=samples))["verdict"] == rules.PASS


# ------------------------------------------------ chaos, coverage, verdict
def test_too_few_chaos_actions_fail_and_failed_ones_do_not_count() -> None:
    world = w.world(
        actions=[w.action("A1", "kill_agent", "L2", 1000, outcome="failed")]
    )
    rule = rules.rule_chaos(world)
    assert rule["verdict"] == rules.FAIL
    assert rule["details"]["needed"] == 2  # 48 min chaos window, one per 45 min
    world = w.world(
        actions=[w.action(f"A{i}", "kill_agent", "L2", 1000 + i) for i in range(2)]
    )
    assert rules.rule_chaos(world)["verdict"] == rules.PASS


def test_sampler_gap_makes_coverage_inconclusive() -> None:
    samples = w.healthy_samples()
    samples["L2"] = [
        r
        for r in samples["L2"]
        if r.get("type") != "tick" or not w.at(600) < r["at"] < w.at(2000)
    ]
    rule = rules.rule_coverage(w.world(samples=samples))
    assert rule["verdict"] == rules.INCONCLUSIVE


def test_overall_verdict_and_report() -> None:
    world = w.world(
        actions=[
            w.action(f"A{i}", "kill_job_tree", "L2", 1000 + 600 * i) for i in range(3)
        ]
    )
    verdict = rules.evaluate(world, generated_at=w.T0, meta={"git": {"commit": "abc"}})
    by_rule = {r["id"]: r["verdict"] for r in verdict["rules"]}
    # no DB samples in the healthy world: DB is INCONCLUSIVE, so is the run
    assert by_rule["db_growth"] == rules.INCONCLUSIVE
    assert verdict["verdict"] == rules.INCONCLUSIVE
    assert verdict["schema"] == rules.VERDICT_SCHEMA
    assert verdict["criterion"] == "V1.0-c2"
    text = report.render(verdict, json_name="verdict.json")
    assert "**INCONCLUSIVE**" in text
    assert "kill_job_tree" in text
    assert _by_id(world)["lost_jobs"]["verdict"] == rules.PASS
