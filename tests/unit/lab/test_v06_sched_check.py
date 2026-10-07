"""V0.6 lab kit (roadmap 0.6.13): the ledger workload and the verdict.

* the workload writes ``start``/``end`` with the agent's ids and its output;
* :func:`analyse` counts lost jobs, duplicate commits, duplicate executions
  under kills and (separately, allowed) overlaps under partition, and retries
  after a failure (not duplicates);
* ``sched_check.py check`` end to end against a mocked controller answers.
"""

from __future__ import annotations

import itertools
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


_CLOCK = itertools.count(1)


def end(job: str, attempt: str) -> LedgerLine:
    """A ledger `end` line; each call is a separate event (its own time stamp,
    as the real workload writes them), so two calls are two executions."""
    return LedgerLine(next(_CLOCK) / 1000, "end", job, attempt, "h")


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
    # D132: j3 used every attempt but was meant to succeed - lost as well
    assert len(verdict.lost) == 3 and not verdict.criterion_1
    assert [x.split(":")[0] for x in verdict.lost] == ["j1", "j2", "j3"]
    assert "2 of 2 attempts" in verdict.lost[2]


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
    assert verdict.committed_without_evidence == ["j1"]
    # D132: no evidence is never a pass - and not a failure either
    assert not verdict.criterion_1 and not verdict.failed
    assert verdict.criterion_1_text == "INCONCLUSIVE"
    assert verdict.as_dict()["criterion_1"] == "INCONCLUSIVE"


def test_a_failure_outranks_missing_evidence() -> None:
    planned = [Planned("j1", 0, 5), Planned("j2", 0, 5)]
    views = {
        "j1": view("succeeded", att("a1", "done")),
        "j2": view("running", att("b1", "running")),
    }
    verdict = analyse(planned, views, [])
    assert verdict.committed_without_evidence == ["j1"]
    assert verdict.criterion_1_text == "FAIL"


def test_one_attempt_id_with_two_end_lines_is_a_duplicate_execution() -> None:
    """D132 / tester T-11: also for a lost attempt on a partitioned node (the
    partition explains an overlap with *another* attempt, not a second run of
    the same one) and for a job that exits non-zero on purpose."""
    planned = [Planned("j1", 0, 5), Planned("j2", 3, 5), Planned("j3", 0, 5)]
    views = {
        "j1": view("succeeded", att("a1", "lost", node="wifi_off"), att("a2", "done")),
        "j2": view(
            "failed", att("b1", "failed", ftype="user_error"), ftype="user_error"
        ),
        "j3": view("succeeded", att("c1", "done")),
    }
    ledger = [
        end("j1", "a1"),
        end("j1", "a1"),
        end("j1", "a2"),
        end("j2", "b1"),
        end("j2", "b1"),
        end("j2", "b1"),
        end("j3", "c1"),
    ]
    verdict = analyse(planned, views, ledger, partitioned_nodes=["wifi_off"])
    assert verdict.duplicate_executions == [
        "j1/a1 (the same attempt ran 2 times)",
        "j2/b1 (the same attempt ran 3 times)",
    ]
    assert verdict.partition_overlaps == ["j1/a1"]  # counted once, not per line
    assert verdict.criterion_1_text == "FAIL"


def test_a_job_meant_to_succeed_that_was_cancelled_is_lost() -> None:
    planned = [Planned("j1", 0, 5), Planned("j2", 3, 5)]
    views = {"j1": view("cancelled"), "j2": view("cancelled")}
    verdict = analyse(planned, views, [])
    assert verdict.lost == ["j1: meant to succeed, cancelled"]


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


def _check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_text: str,
    job_view: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    sys.path.insert(0, str(LAB))
    import sched_check

    run = tmp_path / "run.json"
    run.write_text(json.dumps({"seed": 1, "jobs": [{"job_id": "j1", "exit_code": 0}]}))
    ledger = tmp_path / "ledger"
    ledger.mkdir()
    (ledger / "l2.txt").write_text(ledger_text)
    monkeypatch.setattr(
        sched_check,
        "_client",
        lambda: httpx.Client(
            base_url="http://x",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=job_view)
            ),
        ),
    )
    out = tmp_path / "report.json"
    code = sched_check.main(
        ["check", str(run), "--ledger", str(ledger), "--out", str(out)]
    )
    return code, json.loads(out.read_text())


def test_check_without_ledger_evidence_is_inconclusive_and_exits_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D132 / tester T-12: the job succeeded but its laptop's ledger is empty
    (forgotten on the USB stick). Never PASS, never exit 0 - and not FAIL (3):
    nothing is known to be wrong, the evidence is missing."""
    code, report = _check(
        tmp_path, monkeypatch, "", view("succeeded", att("a1", "done"))
    )
    assert code == 4
    assert report["criterion_1"] == "INCONCLUSIVE"
    assert report["committed_without_ledger_evidence"] == ["j1"]
    captured = capsys.readouterr()
    assert "INCONCLUSIVE" in captured.err and "ledger" in captured.err
    json.loads(captured.out)  # stdout stays the report alone


def test_check_exits_3_on_a_duplicate_execution_of_one_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    twice = (
        "1.0 start j1 a1 l2\n2.0 end j1 a1 l2\n3.0 start j1 a1 l2\n4.0 end j1 a1 l2\n"
    )
    code, report = _check(
        tmp_path, monkeypatch, twice, view("succeeded", att("a1", "done"))
    )
    assert code == 3 and report["criterion_1"] == "FAIL"
    assert report["duplicate_executions_under_kills"]


# ------------------------------------------- the checklist itself (T-14)

CHECKLIST = LAB / "checklists" / "V0.6-scheduling.md"


def test_checklist_part_d_builds_tiny_job_history_on_l2_only() -> None:
    """Tester T-14: a pinned run of the 300 MB project on the strongest laptop
    would leave the tree in its cache, so the expected ``local`` result could
    not occur. Steps 1-2 of part D must pin on L2 only."""
    text = CHECKLIST.read_text(encoding="utf-8")
    part_d = text[text.index("## D.") : text.index("## E.")]
    tiny = part_d[: part_d.index("3. **Inverse")]
    assert "--node <L2-ID>" in tiny
    assert "--node <STRONG-ID>" not in tiny
    assert "Do not run `c4-tiny` on any other laptop" in tiny


def test_checklist_states_the_real_config_path_per_os() -> None:
    """The checklist used to say ``meshforge-agent run --help`` prints the
    config path (it does not). It now states the path per OS - the ones
    :func:`platform_dirs` computes - and a command that prints it."""
    from pathlib import PurePosixPath, PureWindowsPath

    from meshforge.common.config import CONFIG_FILE_NAME, platform_dirs

    text = CHECKLIST.read_text(encoding="utf-8")
    assert "run --help` prints where it is" not in text
    linux = platform_dirs("linux", {}, "/home/u").config_dir / CONFIG_FILE_NAME
    assert linux == PurePosixPath("/home/u/.config/meshforge/config.toml")
    assert "`~/.config/meshforge/config.toml`" in text
    win = platform_dirs(
        "win32", {"APPDATA": "C:\\Users\\u\\AppData\\Roaming"}, "C:\\Users\\u"
    ).config_dir
    assert win / CONFIG_FILE_NAME == PureWindowsPath(
        "C:\\Users\\u\\AppData\\Roaming\\MeshForge\\config.toml"
    )
    assert "`%APPDATA%\\MeshForge\\config.toml`" in text
    assert "default_config_file" in text  # the command that prints it
