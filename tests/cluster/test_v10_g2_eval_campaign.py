"""V1.0 G2 acceptance (brief §5 G2): the evaluation harness end to end on
REAL processes - a controller and three agents (cluster harness, subprocess
runtime) - driven by ``scripts/eval/run_campaign.py`` as an operator runs it
(a subprocess with the ``compute`` CLI's settings, on the origin laptop).

Campaign: 2 families (``cpu.primes`` = Mode 1 / Local, ``split.hash_files``
= a Mode 3 split job) x 3 arms (B0 Local only, B1 round_robin, B5 planner v2
calibrated - one controller profile, so no hook) x 3 repetitions = 18 runs,
smoke sizes (the harness is one machine; nothing here is evidence).

Asserted:

1. **resumable**: the first session stops after 5 runs (``--max-runs``, exit
   1), the second finishes (exit 0); the controller holds exactly one job per
   run (the idempotency keys - no duplicate after the resume);
2. **every run recorded and measured** from the controller's own record:
   status ``ok``, end-to-end > 0, a dispatched decision with the arm's
   policy; B0 ran on the origin laptop only; a Mode 1 / Local job has its
   ``run`` phase and overhead, a split job says ``split_job``;
3. **analysis byte-identical twice** (``analyze.py`` on the raw folder into
   two directories: every file equal), and the report written from it says
   it is not evidence (smoke, 3 repetitions) and prints the analysis's own
   verdicts.
"""

from __future__ import annotations

import filecmp
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("numpy", reason="the analysis needs the ml extra")

from mf_cluster import Fabric
from mf_split_cluster import split_fabric, sql

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(1800)]

EVAL: Final = Path(__file__).resolve().parents[2] / "scripts" / "eval"
ARMS: Final = ("B0", "B1", "B5")
RUNS: Final = 2 * 3 * 3


def _spec(origin: str) -> str:
    return (
        "name: g2-cluster\n"
        "seed: 27\n"
        "link: loopback\n"
        "repetitions: 3\n"
        "smoke: true\n"
        "run_timeout_s: 300\n"
        f"origin: {origin}\n"
        f"arms: [{', '.join(ARMS)}]\n"
        "families:\n"
        "  - family: cpu.primes\n"
        "    sizes: {s: {limit: 10000000, repeats: 1}}\n"
        "  - family: split.hash_files\n"
        "    sizes: {s: {files: 8, size_kb: 64, rounds: 2}}\n"
    )


def _env(fabric: Fabric, data_dir: Path, cwd: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__CA_FILE": str(fabric.ca_dir / "ca.crt"),
            "MESHFORGE_CLI__TOKEN": fabric.api_token(),
            "MESHFORGE_DATA_DIR": str(data_dir),
            "XDG_CONFIG_HOME": str(cwd / "cli-config"),
            "NO_COLOR": "1",
        }
    )
    return env


def _script(
    name: str, *args: str, env: dict[str, str] | None = None, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EVAL / name), *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=1500,
        check=False,
    )


def _runs(raw: Path) -> list[dict[str, Any]]:
    lines = (raw / "runs.jsonl").read_text("utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_campaign_runs_resumes_measures_and_analyses_reproducibly(
    make_fabric: Callable[..., Fabric],
    tmp_path: Path,
    pg_settings: Any,
) -> None:
    fabric = split_fabric(make_fabric, agents=3)
    origin = fabric.nodes[0]
    work = tmp_path / "op"
    work.mkdir()
    spec = work / "campaign.yaml"
    spec.write_text(_spec(origin.node_id), "utf-8")
    raw = work / "raw"
    env = _env(fabric, origin.data_dir, work)

    # --- 1. resumable: stop after 5 runs, then finish
    plan = _script(
        "run_campaign.py", "--spec", str(spec), "--raw", str(raw), "--plan", cwd=work
    )
    assert plan.returncode == 0, plan.stderr
    assert len(plan.stdout.splitlines()) == RUNS
    first = _script(
        "run_campaign.py",
        "--spec",
        str(spec),
        "--raw",
        str(raw),
        "--poll-s",
        "0.3",
        "--max-runs",
        "5",
        env=env,
        cwd=work,
    )
    assert first.returncode == 1, (first.stdout, first.stderr[-3000:])
    assert len(_runs(raw)) == 5
    second = _script(
        "run_campaign.py",
        "--spec",
        str(spec),
        "--raw",
        str(raw),
        "--poll-s",
        "0.3",
        env=env,
        cwd=work,
    )
    assert second.returncode == 0, (second.stdout, second.stderr[-3000:])
    runs = _runs(raw)
    assert len(runs) == RUNS
    assert len({r["run_id"] for r in runs}) == RUNS
    job_ids = [j["job_id"] for r in runs for j in r["jobs"]]
    assert len(set(job_ids)) == RUNS
    count = sql(pg_settings, "SELECT count(*) AS n FROM job")[0].n
    assert count == RUNS, "a resumed run submitted a second job"
    sessions = (raw / "sessions.jsonl").read_text("utf-8").splitlines()
    assert len(sessions) == 2
    session = json.loads(sessions[0])
    assert session["build"]["source"] == "checkout"  # a dev run, never evidence
    assert {n["node_id"] for n in session["environment"]["nodes"]} == {
        n.node_id for n in fabric.nodes
    }

    # --- 2. every run recorded and measured from the controller's record
    assert {r["status"] for r in runs} == {"ok"}, [
        (r["run_id"], r["status"], r["error"]) for r in runs if r["status"] != "ok"
    ]
    policies = {"B1": "round_robin", "B5": "cost_model"}
    for run in runs:
        (job,) = run["jobs"]
        assert job["state"] == "succeeded"
        assert job["end_to_end_ms"] > 0 and job["queue_ms"] is not None
        assert job["mode"] is not None, run["run_id"]
        if run["arm"] == "B0":
            assert job["nodes"] == [origin.node_id] and job["mode"] == "local"
        else:
            assert job["policy"] == policies[run["arm"]]
        if run["family"].startswith("split."):
            # lanes + combine, also when planned onto one lane
            assert job["overhead_basis"] == "split_job"
        else:
            assert job["run_ms"] is not None and job["run_ms"] > 0, run["run_id"]
            assert job["overhead_ms"] == job["end_to_end_ms"] - job["run_ms"]

    # --- 3. analysis byte-identical twice; the report is honest about it
    for out in ("a1", "a2"):
        done = _script(
            "analyze.py", "--raw", str(raw), "--out", str(work / out), cwd=work
        )
        assert done.returncode == 0, done.stderr
    cmp = filecmp.dircmp(work / "a1", work / "a2")
    assert not cmp.diff_files and not cmp.left_only and not cmp.right_only
    for name in ("analysis.json", "SHA256SUMS"):
        assert (work / "a1" / name).read_bytes() == (work / "a2" / name).read_bytes()
    for name in ("groups.csv", "comparisons.csv", "arms.csv"):
        assert (work / "a1" / "tables" / name).read_bytes() == (
            work / "a2" / "tables" / name
        ).read_bytes()
    analysis = json.loads((work / "a1" / "analysis.json").read_text("utf-8"))
    assert analysis["evidence"] is False
    assert len(analysis["groups"]) == 2
    for group in analysis["groups"]:
        assert {a: group["arms"][a]["runs"] for a in ARMS} == dict.fromkeys(ARMS, 3)
        assert group["oracle"] in ARMS
    # two groups < D357's five: no comparison can claim anything
    assert {c["reason"] for c in analysis["comparisons"]} == {"too_few_groups"}
    report = work / "report.md"
    done = _script(
        "make_report.py",
        "--analysis",
        str(work / "a1" / "analysis.json"),
        "--out",
        str(report),
        "--no-figures",
        cwd=work,
    )
    assert done.returncode == 0, done.stderr
    text = report.read_text("utf-8")
    assert "**Not evidence.**" in text and "smoke run" in text
    assert "**not_separable**" in text
    assert "**better**" not in text and "**worse**" not in text
