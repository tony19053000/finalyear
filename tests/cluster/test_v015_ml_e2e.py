"""V0.15 G6a (D377): the ML planner end to end on REAL processes - benchmark
campaigns -> ``compute planner ml train`` -> the planner serving the model
set -> its artefact killed -> ``compute planner eval`` with ML rows ->
retraining reproduced. The dry run of the 0.15.11 lab procedure.

A controller (with the ``ml`` extra: the retrain and fold children train
LightGBM / XGBoost / scikit-learn models) and three agents (cluster
harness, subprocess runtime; numpy jobs from the local wheelhouse).
Everything the operator does goes through the real ``compute`` CLI.

**Campaigns (step 1).** Three whole-job families with ``[local, mode1]``
(``cpu.primes``, ``cpu.zlib``, ``cpu.integrate``; 2 sizes x 3
configurations x 3 repetitions each) and one Mode 3 family with ``[local,
mode1, mode3]`` (``split.montecarlo_range``, 2 sizes x 4 configurations x 3
repetitions): 8 labelled groups, 78 runs. The training floors are lowered
for harness volume (``ml_min_rows`` 20, ``ml_min_families`` 3); everything
else is production defaults.

**What is asserted.**

2. ``compute planner ml train --wait``: a set is written, ``active`` or
   ``rejected`` **with a reason** from the gate's rule list (harness timings
   are noise: the verdict itself is not under test). At least one learned
   member (a model artefact) - else steps 3-4 test nothing.
6. (criterion 3, run right after training, before any new run ends) the
   dataset the controller trained on (``compute planner ml dataset --out``,
   same digest as the set's) trained twice by the real ``fit`` child gives
   the **same set version** as the registered set, ``set.json`` byte for
   byte; a second ``ml train`` is ``no_new_runs``.
3. A rejected set is promoted by SQL (the gate's verdict on harness noise is
   not what this test is about; the serving path is). New benchmark jobs are
   then planned with it: every stored candidate row names the set
   (``ml_model_version``), every learnable term of every feasible whole-job
   row carries ``detail.ml`` (used, or why not); decision latency p99 <
   100 ms (criterion 4, loopback); ``GET /v1/planner/decisions/{id}/replay``
   (and ``compute planner replay``) re-plans one with the set by version:
   ``reproduced``.
4. The set's artefact blobs are deleted and the controller restarted: the
   model source reports it unavailable, new jobs plan **without** ML (no
   ``ml_model_version``: the whole decision falls back to calibration) and
   still finish; a replay of a step-3 decision now says
   ``ml_model_unavailable`` - never a silent calibration re-plan.
5. ``compute planner eval`` over the four campaigns: the report's ``ml``
   block is ``evaluated`` with one fold entry per family, the
   ``cost_model:ml`` rows exist and both ML verdicts carry a status from the
   documented set (any, incl. ``not_separable`` / ``too_few_groups``; never
   a crash).
"""

from __future__ import annotations

import asyncio
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("lightgbm", reason="needs the controller's ml extra")
pytest.importorskip("xgboost", reason="needs the controller's ml extra")
pytest.importorskip("sklearn", reason="needs the controller's ml extra")

from mf_cluster import Fabric
from mf_planner_cluster import cli_json, create_campaign, start_campaign, wait_campaigns
from mf_split_cluster import split_fabric, sql

pytestmark = [pytest.mark.cluster, pytest.mark.slow, pytest.mark.timeout(2400)]

ML_SETTINGS: Final = {
    "ml_min_rows": 20,
    "ml_min_families": 3,
    "ml_eval_timeout_s": 900.0,
    "ml_retrain_timeout_s": 900.0,
}
WHOLE_STRATEGIES: Final = "[local, mode1]"
SPLIT_STRATEGIES: Final = "[local, mode1, mode3]"
FAMILIES: Final[dict[str, tuple[str, list[int], dict[str, Any], str]]] = {
    "cpu.primes": (
        "limit",
        [100_000_000, 110_000_000],
        {"repeats": 4},
        WHOLE_STRATEGIES,
    ),
    "cpu.zlib": ("size_mb", [64, 72], {"repeats": 2}, WHOLE_STRATEGIES),
    "cpu.integrate": ("points_m", [70, 80], {}, WHOLE_STRATEGIES),
    "split.montecarlo_range": ("items", [1200, 1400], {}, SPLIT_STRATEGIES),
}
"""family -> (size parameter, its values, fixed parameters, strategies)."""
REPETITIONS: Final = 3
"""The campaign minimum (labels need 3 repetitions)."""
GATE_REASONS: Final = (
    "too_few_held_out",
    "coverage",
    "family_tolerance",
    "worse_than_reference",
    "not_better",
    "regret_worse",
    "unloadable_artefact",
)
ML_VERDICT_STATUSES: Final = {
    "PASS",
    "FAIL",
    "not_separable",
    "too_few_groups",
    "ml_unavailable",
}
WHOLE_TERMS: Final = ("transfer", "env", "compute", "output")
CAMPAIGNS_WITHIN_S: Final = 1200.0
PROMOTED: Final = "promoted by the V0.15 G6a cluster test (harness data)"


def _spec(
    name: str,
    seed: int,
    family: str,
    key: str,
    values: list[int],
    fixed: Mapping[str, Any],
    strategies: str,
    origin: str,
    repetitions: int,
) -> str:
    params = {key: values, **fixed}
    return (
        f"name: {name}\n"
        f"seed: {seed}\n"
        "families:\n"
        f"  - family: {family}\n"
        f"    params: {json.dumps(params)}\n"
        f"strategies: {strategies}\n"
        "nodes: all\n"
        f"origin: {origin}\n"
        f"repetitions: {repetitions}\n"
        "budget: {max_runs: 200, max_hours: 8}\n"
    )


def _run_campaigns(
    fabric: Fabric,
    data: Path,
    work: Path,
    origin: str,
    tag: str,
    *,
    families: list[str],
    sizes: int | None = None,
) -> list[str]:
    """One campaign per family (``sizes`` = keep only that many parameter
    values), all started, waited for; their ids."""
    ids: list[str] = []
    for seed, family in enumerate(families, start=150):
        key, values, fixed, strategies = FAMILIES[family]
        spec = _spec(
            f"{tag}-{family}",
            seed,
            family,
            key,
            values[:sizes],
            fixed,
            strategies,
            origin,
            REPETITIONS,
        )
        cid, _ = create_campaign(fabric, data, spec, f"{tag}-{family}", cwd=work)
        ids.append(cid)
    for cid in ids:
        start_campaign(fabric, data, cid, cwd=work, per_node=2, cooldown_s=0.0)
    wait_campaigns(fabric, data, ids, cwd=work, within=CAMPAIGNS_WITHIN_S)
    return ids


async def _execute(pg_settings: Any, query: str, params: Mapping[str, Any]) -> int:
    from sqlalchemy import text

    from meshforge.controller.store.engine import create_engine

    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            done = await conn.execute(text(query), dict(params))
            return int(done.rowcount or 0)
    finally:
        await engine.dispose()


def _models(fabric: Fabric, data: Path, work: Path) -> dict[str, Any]:
    return dict(cli_json(fabric, data, "planner", "ml", "models", cwd=work))


def _wait_in_use(
    fabric: Fabric,
    data: Path,
    work: Path,
    check: Callable[[Mapping[str, Any]], bool],
    within: float = 60.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + within
    while True:
        doc = _models(fabric, data, work)
        if check(doc["in_use"]):
            return doc
        if time.monotonic() > deadline:
            pytest.fail(f"model source never reached the state: {doc['in_use']}")
        time.sleep(1.0)


def _decisions(pg_settings: Any, campaign_ids: list[str]) -> list[Any]:
    return sql(
        pg_settings,
        "SELECT pd.id, pd.job_id, pd.candidates, pd.decision_ms "
        "FROM plan_decision pd JOIN bench_run br ON br.job_id = pd.job_id "
        "WHERE br.campaign_id = ANY(:ids) AND pd.outcome = 'dispatched' "
        "ORDER BY pd.id",
        ids=list(campaign_ids),
    )


def _p99(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.99 * len(ordered)) - 1)]


def _fit_child(dataset: Path, out: Path) -> subprocess.CompletedProcess[str]:
    """The retrain's own child entry, with the controller's training settings
    (``ml/folds.train_settings_argv`` for ML_SETTINGS + the defaults)."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "fit",
            "--dataset",
            str(dataset),
            "--out",
            str(out),
            "--min-rows",
            str(ML_SETTINGS["ml_min_rows"]),
            "--min-families",
            str(ML_SETTINGS["ml_min_families"]),
            "--min-effect",
            repr(0.02),
            "--max-interval-log",
            repr(0.6931471805599453),
            "--family-tolerance-log",
            repr(0.05),
        ],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def _replay(fabric: Fabric, decision_id: int) -> dict[str, Any]:
    resp = fabric.call("GET", f"/v1/planner/decisions/{decision_id}/replay")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _blob_files(fabric: Fabric, hexes: list[str]) -> list[Path]:
    root = fabric.workdir / "controller-data" / "controller" / "blobs"
    wanted = set(hexes)
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name in wanted)


# ------------------------------------------------------------------ checks


def _check_ml_decisions(rows: list[Any], version: str) -> list[float]:
    """Step 3: every row names the set; every feasible whole-job row's
    learnable terms carry ``detail.ml``. Returns the decision times."""
    assert rows, "no dispatched decision for the ML campaigns"
    times: list[float] = []
    learned_terms = 0
    for row in rows:
        assert row.decision_ms is not None, row.id
        times.append(float(row.decision_ms))
        for cand in row.candidates:
            assert cand.get("ml_model_version") == version, (row.id, cand)
            assert "ml_fallback" not in cand, (row.id, cand)  # no predictor error
            if not cand.get("feasible") or cand.get("mode") == "split":
                continue
            if "node_set" in cand:  # a split job's one-lane row (lane terms)
                continue
            terms = (cand.get("estimate") or {}).get("terms") or {}
            for name in WHOLE_TERMS:
                block = (terms.get(name) or {}).get("detail", {}).get("ml")
                assert isinstance(block, dict), (row.id, name, terms.get(name))
                assert isinstance(block.get("used"), bool), block
                assert block.get("used") or block.get("reason"), block
                learned_terms += 1
    assert learned_terms > 0, "no whole-job candidate row was checked"
    return times


def _check_eval(report: Mapping[str, Any], families: set[str]) -> None:
    ml = report["ml"]
    assert report["eval_version"] == 2, report["eval_version"]
    assert ml["status"] == "evaluated", (ml["status"], ml["reason"])
    assert {f["family"] for f in ml["folds"]} == families, ml["folds"]
    names = [v["name"] for v in ml["verdicts"]]
    assert names == ["ml_criterion_1", "ml_criterion_2"], names
    for verdict in ml["verdicts"]:
        assert verdict["status"] in ML_VERDICT_STATUSES, verdict
        assert verdict["detail"], verdict
    keys = {p["key"] for p in report["policies"]}
    assert "cost_model:ml:min_time" in keys, sorted(keys)
    assert ml["fallback"]["total"]["decisions"] >= 0, ml["fallback"]


# ------------------------------------------------------------------ the test


def test_ml_train_serve_fallback_evaluate_reproduce(
    make_fabric: Callable[..., Fabric],
    tmp_path: Path,
    pg_settings: Any,
    numpy_wheelhouse: Path,
) -> None:
    started = time.monotonic()
    fabric = split_fabric(
        make_fabric,
        controller_extra={"bench": {"tick_s": 0.5}, "planner": dict(ML_SETTINGS)},
        wheelhouse=numpy_wheelhouse,
    )
    origin = fabric.nodes[0]
    work = tmp_path / "cli"
    work.mkdir()
    data = origin.data_dir
    timings: dict[str, float] = {}

    # --- (1) the campaigns
    campaigns = _run_campaigns(
        fabric, data, work, origin.node_id, "g6a", families=list(FAMILIES)
    )
    timings["campaigns_s"] = time.monotonic() - started
    families = set(FAMILIES)

    # --- (2) train through the CLI
    mark = time.monotonic()
    state = cli_json(
        fabric, data, "planner", "ml", "train", "--wait", cwd=work, timeout_s=1200
    )
    timings["train_s"] = time.monotonic() - mark
    last = state["last"]
    assert last["error"] is None, last
    assert last["skipped"] is None, last
    version = last["created_set"]
    assert isinstance(version, str) and version.startswith("sha256:"), last
    detail = cli_json(
        fabric, data, "planner", "ml", "models", "--set", version, cwd=work
    )
    assert detail["status"] in ("active", "rejected"), detail["status"]
    if detail["status"] == "rejected":
        reason = detail["reason"] or ""
        assert any(rule in reason for rule in GATE_REASONS), reason
    learned = [m for m in detail["members"] if m["algorithm"] != "none"]
    assert learned, f"no learned member: {detail['members']}"

    # --- (6) criterion 3: the same dataset gives the same set, byte for byte
    dataset = work / "dataset.parquet"
    got = cli_json(
        fabric, data, "planner", "ml", "dataset", "--out", str(dataset), cwd=work
    )
    assert got["digest"] == detail["dataset_digest"], (got, detail["dataset_digest"])
    texts = []
    for name in ("fit1", "fit2"):
        done = _fit_child(dataset, work / name)
        assert done.returncode == 0, done.stderr[-3000:]
        texts.append((work / name / "set.json").read_bytes())
        doc = json.loads(texts[-1])
        assert doc["set_version"] == version, (doc["set_version"], version)
    assert texts[0] == texts[1]
    again = cli_json(
        fabric, data, "planner", "ml", "train", "--wait", cwd=work, timeout_s=600
    )
    assert again["last"]["skipped"] == "no_new_runs", again["last"]

    # --- (3) the planner serves the set
    if detail["status"] == "rejected":
        n = asyncio.run(
            _execute(
                pg_settings,
                "UPDATE ml_model_set SET status = 'active', reason = :r, "
                "status_changed_at = now() WHERE set_version = :v",
                {"v": version, "r": PROMOTED},
            )
        )
        assert n == 1
    _wait_in_use(fabric, data, work, lambda u: u["in_use_set"] == version)
    served = _run_campaigns(
        fabric,
        data,
        work,
        origin.node_id,
        "g6a-ml",
        families=["cpu.primes", "split.montecarlo_range"],
        sizes=1,
    )
    rows = _decisions(pg_settings, served)
    times = _check_ml_decisions(rows, version)
    assert _p99(times) < 100.0, sorted(times)[-5:]  # criterion 4 (loopback)
    probe = rows[0]
    replayed = _replay(fabric, int(probe.id))
    assert replayed["status"] == "reproduced", replayed
    assert replayed["ml_model_version"] == version, replayed
    cli = cli_json(fabric, data, "planner", "replay", str(probe.id), cwd=work)
    cli.pop("schema_version", None)
    assert cli == replayed, (cli, replayed)

    # --- (4) the artefact is gone: per-decision fallback, jobs still finish
    hexes = [m["version"].split(":", 1)[1] for m in learned]
    files = _blob_files(fabric, hexes)
    assert len(files) == len(set(hexes)), (files, hexes)
    for path in files:
        path.unlink()
    fabric.restart_controller()
    after = _wait_in_use(
        fabric, data, work, lambda u: u["in_use_set"] is None and bool(u["unavailable"])
    )
    assert after["serving_set"] == version, after
    gone = _replay(fabric, int(probe.id))
    assert gone["status"] == "ml_model_unavailable", gone
    assert gone["ml_model_version"] == version, gone
    fallback = _run_campaigns(
        fabric, data, work, origin.node_id, "g6a-gone", families=["cpu.primes"], sizes=1
    )
    for row in _decisions(pg_settings, fallback):
        for cand in row.candidates:
            assert "ml_model_version" not in cand, (row.id, cand)

    # --- (5) the evaluator's ML rows on the first campaigns
    mark = time.monotonic()
    report = cli_json(
        fabric,
        data,
        "planner",
        "eval",
        *(arg for cid in campaigns for arg in ("--campaign", cid)),
        cwd=work,
        timeout_s=1800,
    )["report"]
    timings["eval_s"] = time.monotonic() - mark
    _check_eval(report, families)
    timings["total_s"] = time.monotonic() - started
    print(  # noqa: T201 - for the hand-off
        "g6a ml e2e: "
        + json.dumps(
            {
                "set": version,
                "status": detail["status"],
                "reason": detail["reason"],
                "learned": [m["term"] + ":" + m["algorithm"] for m in learned],
                "verdicts": {v["name"]: v["status"] for v in report["ml"]["verdicts"]},
                "timings": {k: round(v, 1) for k, v in timings.items()},
            },
            sort_keys=True,
        )
    )
