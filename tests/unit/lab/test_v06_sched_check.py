"""V0.6 lab kit (roadmap 0.6.13): the ledger workload and the verdict.

* the workload writes ``start``/``end`` with the agent's ids and its output;
* :func:`analyse` counts lost jobs, duplicate commits, duplicate executions
  under kills and (separately, allowed) overlaps under partition, and retries
  after a failure (not duplicates);
* ``sched_check.py check`` end to end against a mocked controller answers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from _sched.analysis import LedgerLine, Planned, analyse, parse_ledger

LAB = Path(__file__).resolve().parents[3] / "lab"
WORKLOAD = LAB / "workloads" / "v06-ledger" / "main.py"


def test_the_workload_leaves_start_end_and_its_output(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger"
    work = tmp_path / "job"
    work.mkdir()
    env = {
        **os.environ,
        "MF_LAB_LEDGER": str(ledger),
        "MESHFORGE_JOB_ID": "job_1",
        "MESHFORGE_ATTEMPT_ID": "att_1",
    }
    out = subprocess.run(
        [sys.executable, str(WORKLOAD), "0", "0.05", "3"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 3
    (file,) = ledger.glob("*.txt")
    lines = parse_ledger(file.read_text().splitlines())
    assert [(x.kind, x.job_id, x.attempt_id) for x in lines] == [
        ("start", "job_1", "att_1"),
        ("end", "job_1", "att_1"),
    ]
    assert (work / "out" / "result.txt").read_text().startswith("job_1 att_1 ")


def end(job: str, attempt: str) -> LedgerLine:
    return LedgerLine(1.0, "end", job, attempt, "h")


def view(
    state: str, *attempts: dict[str, Any], ftype: str | None = None
) -> dict[str, Any]:
    return {"state": state, "failure_type": ftype, "attempts": list(attempts)}


def att(
    aid: str, state: str, node: str = "n1", ftype: str | None = None
) -> dict[str, Any]:
    return {"attempt_id": aid, "state": state, "node_id": node, "failure_type": ftype}


def test_a_clean_run_passes() -> None:
    planned = [Planned("j1", 0, 5), Planned("j2", 3, 5)]
    views = {
        "j1": view("succeeded", att("a1", "lost"), att("a2", "done")),
        "j2": view(
            "failed", att("b1", "failed", ftype="user_error"), ftype="user_error"
        ),
    }
    verdict = analyse(planned, views, [end("j1", "a2"), end("j2", "b1")])
    assert verdict.criterion_1, verdict.as_dict()
    assert verdict.attempts_histogram == {2: 1, 1: 1}


def test_not_terminal_and_retryable_failure_with_attempts_left_are_lost() -> None:
    planned = [Planned("j1", 0, 5), Planned("j2", 0, 5), Planned("j3", 0, 2)]
    views = {
        "j1": view("running", att("a1", "running")),
        "j2": view("failed", att("b1", "lost"), ftype="node_lost"),
        "j3": view("failed", att("c1", "lost"), att("c2", "lost"), ftype="node_lost"),
    }
    verdict = analyse(planned, views, [])
    assert len(verdict.lost) == 2 and not verdict.criterion_1
    assert any("j2" in x for x in verdict.lost) and any("j1" in x for x in verdict.lost)


def test_two_done_attempts_is_a_duplicate_commit() -> None:
    planned = [Planned("j1", 0, 5)]
    views = {"j1": view("succeeded", att("a1", "done"), att("a2", "done"))}
    verdict = analyse(planned, views, [end("j1", "a1"), end("j1", "a2")])
    assert verdict.duplicate_commits and not verdict.criterion_1


def test_an_extra_end_on_a_partitioned_node_is_an_allowed_overlap() -> None:
    planned = [Planned("j1", 0, 5)]
    views = {
        "j1": view("succeeded", att("a1", "lost", node="wifi_off"), att("a2", "done"))
    }
    ledger = [end("j1", "a1"), end("j1", "a2")]
    verdict = analyse(planned, views, ledger, partitioned_nodes=["wifi_off"])
    assert verdict.criterion_1
    assert verdict.partition_overlaps == ["j1/a1"]
    # the same on a node that was only killed: a duplicate execution
    killed = analyse(planned, views, ledger, partitioned_nodes=[])
    assert killed.duplicate_executions and not killed.criterion_1


def test_a_retry_after_a_reported_failure_is_not_a_duplicate() -> None:
    planned = [Planned("j1", 0, 5)]
    views = {
        "j1": view(
            "succeeded", att("a1", "failed", ftype="output_error"), att("a2", "done")
        )
    }
    verdict = analyse(planned, views, [end("j1", "a1"), end("j1", "a2")])
    assert verdict.criterion_1 and verdict.retried_after_failure


def test_committed_without_ledger_evidence_is_reported() -> None:
    verdict = analyse(
        [Planned("j1", 0, 5)], {"j1": view("succeeded", att("a1", "done"))}, []
    )
    assert verdict.committed_without_evidence == ["j1"] and verdict.criterion_1


def test_torn_ledger_lines_are_skipped() -> None:
    lines = parse_ledger(["1.0 end j a h", "garbage", "x end j a h", "2.0 start j a"])
    assert len(lines) == 1


def test_check_command_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.path.insert(0, str(LAB))
    import sched_check

    run = tmp_path / "run.json"
    run.write_text(json.dumps({"seed": 1, "jobs": [{"job_id": "j1", "exit_code": 0}]}))
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "l2.txt").write_text("1.0 start j1 a1 l2\n2.0 end j1 a1 l2\n")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/j1"
        return httpx.Response(200, json=view("succeeded", att("a1", "done")))

    monkeypatch.setattr(
        sched_check,
        "_client",
        lambda: httpx.Client(
            base_url="http://x", transport=httpx.MockTransport(handler)
        ),
    )
    out = tmp_path / "report.json"
    code = sched_check.main(
        ["check", str(run), "--ledger", str(ledger), "--out", str(out)]
    )
    assert code == 0
    report = json.loads(out.read_text())
    assert report["criterion_1"] == "PASS" and report["ledger_files"] == ["l2.txt"]
    assert "PASS" in capsys.readouterr().out
