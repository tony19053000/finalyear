"""V0.6 G4 tester round 2 - the lab verdict (D132) at the command line.

Verdict ranking: FAIL (3) outranks INCONCLUSIVE (4) outranks PASS (0), and a
verdict never reads PASS without evidence:

* R2-1 (strict xfail): a run file with no jobs reads PASS (vacuous);
* R2-2 (strict xfail): the same ledger copied under two file names makes
  every attempt look executed twice (a FAIL for a duplicate that did not
  happen - safe direction, but the lab team is sent hunting a ghost).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from _sched.analysis import INCONCLUSIVE, PASS, Planned, analyse

LAB = Path(__file__).resolve().parents[3] / "lab"
pytestmark = pytest.mark.unit


def att(attempt_id: str, state: str, node: str = "n1") -> dict[str, Any]:
    return {"attempt_id": attempt_id, "state": state, "node_id": node}


def done_view(*attempts: dict[str, Any]) -> dict[str, Any]:
    return {"state": "succeeded", "failure_type": None, "attempts": list(attempts)}


def run_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    jobs: list[dict[str, Any]],
    views: dict[str, dict[str, Any]],
    ledgers: dict[str, str],
    *extra: str,
) -> tuple[int, dict[str, Any]]:
    sys.path.insert(0, str(LAB))
    import sched_check

    run = tmp_path / "run.json"
    run.write_text(json.dumps({"seed": 1, "jobs": jobs}))
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    for name, body in ledgers.items():
        (ledger / name).write_text(body)

    def handler(request: httpx.Request) -> httpx.Response:
        job_id = request.url.path.rsplit("/", 1)[1]
        return (
            httpx.Response(200, json=views[job_id])
            if job_id in views
            else httpx.Response(404)
        )

    monkeypatch.setattr(
        sched_check,
        "_client",
        lambda: httpx.Client(
            base_url="http://x", transport=httpx.MockTransport(handler)
        ),
    )
    out = tmp_path / "report.json"
    code = sched_check.main(
        ["check", str(run), "--ledger", str(ledger), "--out", str(out), *extra]
    )
    return code, json.loads(out.read_text())


def two_jobs() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    jobs = [{"job_id": "j1", "exit_code": 0}, {"job_id": "j2", "exit_code": 0}]
    views = {
        "j1": done_view(att("a1", "done")),
        "j2": done_view(att("b1", "done")),
    }
    return jobs, views


def test_a_duplicate_on_one_laptop_and_a_missing_ledger_on_another_is_fail_not_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, views = two_jobs()
    ledger = {"l2.txt": "1.0 end j1 a1 l2\n2.0 end j1 a1 l2\n"}  # j2: no evidence
    code, report = run_check(tmp_path, monkeypatch, jobs, views, ledger)
    assert code == 3
    assert report["criterion_1"] == "FAIL"
    assert report["committed_without_ledger_evidence"] == ["j2"]


def test_a_partition_overlap_plus_a_missing_ledger_is_inconclusive_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, views = two_jobs()
    views["j1"] = done_view(att("a0", "lost", "n9"), att("a1", "done"))
    ledger = {"l2.txt": "1.0 end j1 a0 l2\n2.0 end j1 a1 l3\n"}
    code, report = run_check(
        tmp_path, monkeypatch, jobs, views, ledger, "--partitioned", "n9"
    )
    assert code == 4
    assert report["overlaps_under_partition"] == ["j1/a0"]
    assert report["duplicate_executions_under_kills"] == []


def test_a_job_the_controller_forgot_is_lost_even_with_a_perfect_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, views = two_jobs()
    del views["j2"]  # the controller answers 404 (database wiped / wrong url)
    ledger = {"l2.txt": "1.0 end j1 a1 l2\n2.0 end j2 b1 l2\n"}
    code, report = run_check(tmp_path, monkeypatch, jobs, views, ledger)
    assert code == 3 and report["lost"] == ["j2: no answer from the controller"]


def test_a_ledger_dir_without_txt_files_is_a_usage_error_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sys.path.insert(0, str(LAB))
    import sched_check

    run = tmp_path / "run.json"
    run.write_text(json.dumps({"seed": 1, "jobs": [{"job_id": "j1", "exit_code": 0}]}))
    empty = tmp_path / "ledger"
    empty.mkdir()
    (empty / "l2.TXT").write_text("1.0 end j1 a1 l2\n")  # Windows-style upper case
    assert sched_check.main(["check", str(run), "--ledger", str(empty)]) == 2
    assert "no ledger files" in capsys.readouterr().out


def test_an_unreachable_controller_never_reads_as_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sys.path.insert(0, str(LAB))
    import sched_check

    run = tmp_path / "run.json"
    run.write_text(json.dumps({"seed": 1, "jobs": [{"job_id": "j1", "exit_code": 0}]}))
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "l2.txt").write_text("1.0 end j1 a1 l2\n")

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(
        sched_check,
        "_client",
        lambda: httpx.Client(
            base_url="http://x", transport=httpx.MockTransport(refuse)
        ),
    )
    assert sched_check.main(["check", str(run), "--ledger", str(ledger)]) in (3, 6)


@pytest.mark.xfail(strict=True, reason="R2-1: an empty run is a vacuous PASS")
def test_r2_1_a_run_with_no_jobs_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert analyse([], {}, []).criterion_1_text != PASS
    code, _report = run_check(
        tmp_path, monkeypatch, [], {}, {"l2.txt": "1.0 end j1 a1 l2\n"}
    )
    assert code != 0


@pytest.mark.xfail(
    strict=True, reason="R2-2: one ledger copied twice reads as a duplicate"
)
def test_r2_2_the_same_ledger_under_two_names_is_not_a_duplicate_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, views = two_jobs()
    body = "1.0 start j1 a1 l2\n2.0 end j1 a1 l2\n3.0 end j2 b1 l2\n"
    code, report = run_check(
        tmp_path, monkeypatch, jobs, views, {"l2.txt": body, "l2-copy.txt": body}
    )
    assert report["duplicate_executions_under_kills"] == []
    assert code == 0


def test_the_vacuous_verdict_is_not_inconclusive_by_accident() -> None:
    """Pin: today an empty plan with a ledger of nothing reads PASS; the xfail
    above is what flips when it is fixed - keep the constant it is compared to."""
    assert INCONCLUSIVE != PASS
    assert analyse([Planned("j", 0, 5)], {"j": None}, []).criterion_1_text == "FAIL"
