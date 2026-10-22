"""V0.14 G6 (D325-D327): the planner chain end to end on REAL processes -
benchmark campaigns -> labels -> calibration refresh (D319 gate) -> evaluator
report -> ``compute planner eval`` / ``reports`` / ``explain``.

A controller and three agents (cluster harness, subprocess runtime; split
lanes in the job's own ``numpy`` environment from the local wheelhouse).
Everything the operator does goes through the real ``compute`` CLI, except
the two ``POST /v1/planner/eval-reports`` the determinism check needs.

**Campaigns (D325).**

* (a) ``cpu.primes``, a 10-value ``limit`` sweep (``repeats`` 4: the smallest
  points inside the 5 s - 3 min runtime window every benchmark run must fit,
  D225 - the brief's "~0.3-2 s" is below that floor), strategies ``[local,
  mode1]``, origin = agent 1, 3 repetitions: 10 groups x 3 configurations x 3
  = 90 runs;
* (b) ``split.image_classify``, 3 image counts, ``[local, mode1, mode3]``, 3
  repetitions: 3 groups x 4 configurations (local, 2 x mode1 = "don't split"
  one lane, mode3 over the 3 nodes) x 3 = 36 runs - Mode 3 records through
  the evaluator's DB loader.

**What is asserted (D326 - never a PASS on criteria 1-3: harness timings
on one machine are noise).**

1. the report is stored and served (``GET`` == ``POST`` answer), not leaked;
2. **independent recomputation** from the report's own ``groups[]``
   (``mf_planner_cluster``, no evaluator code): criterion 1's counted groups,
   share and verdict; every paired criterion-2 comparison's common groups and
   mean difference, and the verdict restated from its CIs;
3. no silent drop: every run is a record of its group, records = used +
   skipped (by reason) + records of groups not evaluated, every Mode 3 group
   is evaluated with its records replayed (or not evaluated with a stated
   reason), and its replays' votes add up;
4. the latency block: its decisions = every ``dispatched`` decision row of
   the fabric (only campaign jobs ran), none without a time, p99 < the budget
   (a loose guard), sized by the 3-node snapshots; criterion 3 says
   ``too_few_decisions`` (the criterion wants 4-node snapshots);
5. ``compute planner calibrate`` writes a row whose ``fit_inputs`` holds
   ``regret_check`` in {passed, rejected, skipped} and ``regret_groups`` no
   more than the labelled groups (skipped exactly below 10);
6. a second ``POST`` with the same inputs gives the same report, byte for
   byte (canonical JSON);
7. ``compute planner eval --json`` is the API's report; ``compute planner
   reports`` lists every stored report.

**Criterion 4 on a real Mode 3 decision (D327).** For a finished ``mode3``
job: ``compute explain --json`` == ``GET /v1/jobs/{id}/decisions``; its leaves
are the G5 contract fixture's field set (nothing unknown); ``decision_ms`` is
set; the chosen set's terms carry no ``actual_s`` (D320: Mode 3 sets, by
design) and its margin sentence names the set; for a finished ``mode1`` job
the chosen node's ``compute`` term does carry ``actual_s``.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric
from mf_explain_contract import FIXTURE
from mf_planner_cluster import (
    CRITERION_KEY,
    RULES,
    answer_leaves,
    cli_json,
    create_campaign,
    criterion1_from_groups,
    criterion2_status,
    paired_from_groups,
    start_campaign,
    wait_campaigns,
)
from mf_split_cluster import split_fabric, sql

pytestmark = [pytest.mark.cluster, pytest.mark.slow, pytest.mark.timeout(3000)]

PRIMES: Final = "cpu.primes"
IMAGES: Final = "split.image_classify"
LIMITS: Final = [100_000_000 + 5_000_000 * i for i in range(10)]
IMAGE_COUNTS: Final = [2000, 2500, 3000]
PRIMES_RUNS: Final = 10 * 3 * 3
IMAGE_RUNS: Final = 3 * 4 * 3
CAMPAIGNS_WITHIN_S: Final = 2400.0


def _primes_spec(origin: str) -> str:
    return (
        "name: g6-primes\n"
        "seed: 61\n"
        "families:\n"
        f"  - family: {PRIMES}\n"
        f"    params: {{limit: {json.dumps(LIMITS)}, repeats: 4}}\n"
        "strategies: [local, mode1]\n"
        "nodes: all\n"
        f"origin: {origin}\n"
        "repetitions: 3\n"
        "budget: {max_runs: 200, max_hours: 8}\n"
    )


def _images_spec(origin: str) -> str:
    return (
        "name: g6-images\n"
        "seed: 62\n"
        "families:\n"
        f"  - family: {IMAGES}\n"
        f"    params: {{images: {json.dumps(IMAGE_COUNTS)}}}\n"
        "strategies: [local, mode1, mode3]\n"
        "nodes: all\n"
        f"origin: {origin}\n"
        "repetitions: 3\n"
        "budget: {max_runs: 100, max_hours: 8}\n"
    )


def _post_eval(fabric: Fabric, campaign_ids: list[str]) -> dict[str, Any]:
    resp = fabric.call(
        "POST",
        "/v1/planner/eval-reports",
        json={"campaign_ids": campaign_ids},
        timeout=600.0,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _canonical(report: Any) -> str:
    return json.dumps(report, sort_keys=True, separators=(",", ":"))


# ------------------------------------------------------------------ checks


def _check_criterion_1(report: dict[str, Any]) -> None:
    (verdict,) = [v for v in report["verdicts"] if v["criterion"] == 1]
    counted, share = criterion1_from_groups(report)
    (row,) = [p for p in report["policies"] if p["key"] == CRITERION_KEY]
    assert row["counted_groups"] == counted == verdict["counted_groups"], verdict
    assert row["within_threshold_pct"] == share, (row, share)
    settings = report["settings"]
    if counted < settings["min_groups"] or share is None:
        assert verdict["status"] == "too_few_groups", verdict
    else:
        want = "PASS" if share >= settings["within_target_pct"] else "FAIL"
        assert (verdict["status"], verdict["value"]) == (want, share), verdict


def _check_criterion_2(report: dict[str, Any]) -> None:
    (verdict,) = [v for v in report["verdicts"] if v["criterion"] == 2]
    comparisons = {c["key"]: c for c in verdict["comparisons"]}
    assert set(comparisons) == set(RULES), comparisons
    for rule, (common, mean) in paired_from_groups(report).items():
        c = comparisons[rule]
        assert c["common_groups"] == common, (rule, c, common)
        if mean is None:
            assert c["mean_diff_pct"] is None, c
        else:
            # the evaluator's mean of the same differences (6 decimals)
            assert c["mean_diff_pct"] == pytest.approx(mean, abs=2e-6), (rule, c)
        if c["ci_low"] is not None:
            assert c["ci_low"] <= c["mean_diff_pct"] + 1e-6, c
            assert c["mean_diff_pct"] <= c["ci_high"] + 1e-6, c
        unmeasured = sum(
            1
            for g in report["groups"]
            for p in g["picks"]
            if p["key"] == rule and p["status"] == "unmeasured"
        )
        assert c["unmeasured_groups"] == unmeasured, (rule, c)
    want = criterion2_status(
        list(comparisons.values()), int(report["settings"]["min_groups"])
    )
    assert verdict["status"] == want, verdict
    if want != "too_few_groups":
        lows = [float(c["ci_low"]) for c in comparisons.values()]
        assert verdict["value"] == pytest.approx(min(lows), abs=1e-6), verdict


def _check_no_silent_drop(
    report: dict[str, Any], runs_per_group: dict[tuple[str, str], int]
) -> None:
    counts = report["counts"]
    groups = report["groups"]
    # every done run is a record of its group, and only those
    assert {
        (g["campaign_id"], g["group_id"]): g["records"] for g in groups
    } == runs_per_group
    assert counts["records"] == sum(runs_per_group.values())
    not_evaluated = sum(g["records"] for g in groups if g["status"] == "excluded")
    skipped = sum(counts["records_skipped"].values())
    assert counts["records"] == counts["records_used"] + skipped + not_evaluated, counts
    assert counts["records_used"] == sum(g["records_used"] for g in groups)
    for g in groups:
        if g["status"] == "excluded":
            assert g["excluded_reason"], g  # a stated reason, never silent
            continue
        if g["status"] == "no_replay":
            assert g["records_used"] == 0, g
            continue
        assert g["status"] == "evaluated", g
        for pick in g["picks"]:
            if pick["key"] == "oracle":
                continue
            # every usable record voted once under every policy
            assert sum(pick["votes"].values()) == g["records_used"], (g, pick)


def _check_mode3(report: dict[str, Any]) -> None:
    mode3 = [
        g
        for g in report["groups"]
        if any(c["strategy"] == "mode3" for c in g["configs"])
    ]
    assert len(mode3) == len(IMAGE_COUNTS), [g["group_id"] for g in mode3]
    sets_seen = 0
    for g in mode3:
        (config,) = [c for c in g["configs"] if c["strategy"] == "mode3"]
        assert len(config["node_set"]) == 3, config  # the 3 nodes, sorted
        assert config["node_set"] == sorted(config["node_set"]), config
        if g["status"] == "excluded":
            assert g["excluded_reason"], g
            continue
        # a Mode 3 group is planned from its records' replays (the loader's
        # first-dispatched-decision path for split jobs, D259/D314)
        assert g["status"] == "evaluated", g
        assert g["records_used"] == g["records"], g
        for pick in g["picks"]:
            if pick["status"] == "counted" and pick["config_id"] == config["config_id"]:
                sets_seen += 1
            for voted in pick["votes"]:
                assert voted == "unmeasured" or voted in {
                    c["config_id"] for c in g["configs"]
                }, (g["group_id"], pick)
    evaluated = [g for g in mode3 if g["status"] == "evaluated"]
    if evaluated:
        # the oracle or a policy picks the Mode 3 set somewhere, or every
        # policy's pick is accounted for by another configuration (no drop)
        assert sets_seen > 0 or all(
            p["status"] in ("counted", "unmeasured")
            for g in evaluated
            for p in g["picks"]
        ), mode3


def _check_latency(report: dict[str, Any], pg_settings: Any) -> None:
    latency = report["latency"]
    (rows,) = sql(
        pg_settings,
        "SELECT count(*) AS n, count(decision_ms) AS timed FROM plan_decision "
        "WHERE outcome = 'dispatched'",
    )
    assert latency["decisions"] + latency["without_latency"] == rows.n, latency
    assert latency["without_latency"] == 0 and rows.timed == rows.n, latency
    assert latency["p99_ms"] is not None and latency["p99_ms"] < latency["budget_ms"]
    assert latency["p50_ms"] <= latency["p99_ms"] <= latency["max_ms"], latency
    assert list(latency["by_node_count"]) == ["3"], latency
    assert latency["by_node_count"]["3"]["decisions"] == latency["decisions"]
    (c3,) = [v for v in report["verdicts"] if v["criterion"] == 3]
    assert c3["status"] == "too_few_decisions" and c3["counted_groups"] == 0, c3


def _check_calibration(
    created: dict[str, Any], pg_settings: Any, labelled: int
) -> None:
    assert created is not None, "calibrate wrote no row"
    (row,) = sql(
        pg_settings,
        "SELECT status, reason, fit_inputs FROM planner_calibration WHERE version = :v",
        v=created["version"],
    )
    inputs = row.fit_inputs
    if row.status == "rejected" and "regret_check" not in inputs:
        # the D306 error check rejected it before the regret check ran
        assert row.reason and "holdout_error" in row.reason, row
        return
    assert inputs["regret_check"] in ("passed", "rejected", "skipped"), inputs
    groups = int(inputs["regret_groups"])
    assert 0 <= groups <= labelled, (groups, labelled)
    if inputs["regret_check"] == "skipped":
        assert groups < 10, inputs
        assert row.reason and "regret check skipped" in row.reason, row
    else:
        assert groups >= 10, inputs
        assert inputs["holdout_regret_new"] is not None, inputs


def _check_explain(
    fabric: Fabric, data_dir: Path, work: Path, mode3_job: str, mode1_job: str
) -> None:
    contract = json.loads(FIXTURE.read_text(encoding="utf-8"))
    known = {f["field"] for f in contract["fields"]} | {
        f["field"] for f in contract["not_shown"]
    }
    for job_id in (mode3_job, mode1_job):
        api = fabric.call("GET", f"/v1/jobs/{job_id}/decisions").json()
        cli = cli_json(fabric, data_dir, "explain", job_id, cwd=work)
        cli.pop("schema_version", None)
        assert cli == api, job_id  # criterion 4: identical fields
        unknown = answer_leaves(api) - known
        assert not unknown, unknown  # nothing outside the contract's field set
        for decision in api["decisions"]:
            assert decision["decision_ms"] is not None, decision
    # --- the Mode 3 job: the chosen set, no per-term actual (D320)
    api = fabric.call("GET", f"/v1/jobs/{mode3_job}/decisions").json()
    first = next(d for d in api["decisions"] if d["outcome"] == "dispatched")
    (chosen,) = [
        c
        for c in first["candidates"]
        if c["rank"] == 1 and c["node_id"] == first["chosen_node_id"]
    ]
    assert chosen["mode"] == "split" and len(chosen["node_set"]) == 3, chosen
    assert all(t["actual_s"] is None for t in chosen["estimate"]["terms"].values())
    sentence = first["margin_sentence"]
    assert isinstance(sentence, str), first
    assert all(node in sentence for node in chosen["node_set"]), sentence
    # --- a Mode 1 job: the chosen node's compute term has its actual
    api = fabric.call("GET", f"/v1/jobs/{mode1_job}/decisions").json()
    first = next(d for d in api["decisions"] if d["outcome"] == "dispatched")
    (chosen,) = [
        c
        for c in first["candidates"]
        if c["rank"] == 1 and c["node_id"] == first["chosen_node_id"]
    ]
    assert chosen["estimate"]["terms"]["compute"]["actual_s"] is not None, chosen


def _summary(report: dict[str, Any], calibrated: dict[str, Any], took: float) -> str:
    """One diagnostic block (printed with ``-s``): what the real data gave."""
    lines = [f"g6 harness: campaigns done in {took:.0f} s"]
    lines.append(f"  counts {json.dumps(report['counts'], sort_keys=True)}")
    lines.extend(
        f"  criterion {v['criterion']}: {v['status']} - {v['detail']}"
        for v in report["verdicts"]
    )
    lat = report["latency"]
    lines.append(
        f"  latency n={lat['decisions']} p50={lat['p50_ms']} p99={lat['p99_ms']} "
        f"max={lat['max_ms']} over={lat['over_budget']}"
    )
    created = calibrated.get("created") or {}
    lines.append(
        f"  calibration v{created.get('version')} {created.get('status')} "
        f"reason={created.get('reason')!r}"
    )
    for g in report["groups"]:
        crit = next((p for p in g["picks"] if p["key"] == CRITERION_KEY), None)
        strategies = {c["config_id"]: c["strategy"] for c in g["configs"]}
        picked = (
            None
            if crit is None
            else (
                crit["status"],
                strategies.get(crit["config_id"] or ""),
                crit["regret_pct"],
            )
        )
        lines.append(
            f"  {g['family']} {g['group_id'][:12]} {g['label']} {g['status']} "
            f"best={strategies.get(g['best_config_id'] or '')} crit={picked}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------ the test


def test_campaigns_to_calibration_to_evaluation_to_explain(
    make_fabric: Callable[..., Fabric],
    tmp_path: Path,
    pg_settings: Any,
    numpy_wheelhouse: Path,
) -> None:
    fabric = split_fabric(
        make_fabric,
        controller_extra={"bench": {"tick_s": 0.5}},
        wheelhouse=numpy_wheelhouse,
    )
    origin = fabric.nodes[0]
    work = tmp_path / "cli"
    work.mkdir()
    data = origin.data_dir

    # --- D325: two campaigns through the real CLI, run by the controller
    primes, n_primes = create_campaign(
        fabric, data, _primes_spec(origin.node_id), "primes", cwd=work
    )
    images, n_images = create_campaign(
        fabric, data, _images_spec(origin.node_id), "images", cwd=work
    )
    assert (n_primes, n_images) == (PRIMES_RUNS, IMAGE_RUNS)
    started = time.monotonic()
    for cid in (primes, images):
        start_campaign(fabric, data, cid, cwd=work, per_node=2, cooldown_s=0.0)
    wait_campaigns(fabric, data, [primes, images], cwd=work, within=CAMPAIGNS_WITHIN_S)
    took = time.monotonic() - started
    campaigns = sorted([primes, images])

    runs = sql(
        pg_settings,
        "SELECT campaign_id, group_id, strategy, family, state, job_id "
        "FROM bench_run WHERE campaign_id IN (:a, :b)",
        a=primes,
        b=images,
    )
    assert Counter(r.state for r in runs) == {"done": PRIMES_RUNS + IMAGE_RUNS}
    runs_per_group = Counter((r.campaign_id, r.group_id) for r in runs)
    assert len(runs_per_group) == 13

    # --- D326 (5): the calibration refresh after the campaigns
    calibrated = cli_json(fabric, data, "planner", "calibrate", cwd=work)
    assert calibrated["skipped"] is None, calibrated

    # --- D326 (1, 6): two evaluations of the same inputs
    first = _post_eval(fabric, campaigns)
    second = _post_eval(fabric, campaigns)
    report = first["report"]
    assert first["leaked"] is False and report["leaked"] is False
    assert first["campaign_ids"] == campaigns == report["campaign_ids"]
    assert _canonical(second["report"]) == _canonical(report)  # byte-identical
    served = fabric.call("GET", f"/v1/planner/eval-reports/{first['id']}").json()
    assert served == first

    labelled = sum(1 for g in report["groups"] if g["label"] in ("best", "tie"))
    _check_calibration(calibrated["created"], pg_settings, labelled)
    _check_criterion_1(report)  # (2)
    _check_criterion_2(report)
    _check_no_silent_drop(report, dict(runs_per_group))  # (3)
    _check_mode3(report)
    _check_latency(report, pg_settings)  # (4)

    # --- D326 (7): the CLI's evaluation is the API's; reports lists them all
    cli = cli_json(
        fabric,
        data,
        "planner",
        "eval",
        "--campaign",
        primes,
        "--campaign",
        images,
        cwd=work,
        timeout_s=600,
    )
    assert _canonical(cli["report"]) == _canonical(report)
    listed = cli_json(fabric, data, "planner", "reports", cwd=work)
    assert {r["id"] for r in listed["reports"]} == {
        first["id"],
        second["id"],
        cli["id"],
    }
    for summary in listed["reports"]:
        assert [v["criterion"] for v in summary["verdicts"]] == [1, 2, 3]

    # --- D327: criterion 4 on a real Mode 3 decision (and a Mode 1 one)
    mode3_job = next(r.job_id for r in runs if r.strategy == "mode3")
    mode1_job = next(
        r.job_id for r in runs if r.strategy == "mode1" and r.family == PRIMES
    )
    _check_explain(fabric, data, work, mode3_job, mode1_job)
    print(_summary(report, calibrated, took))  # noqa: T201 - for the hand-off
