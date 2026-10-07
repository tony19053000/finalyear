"""V0.6 G4 tester round 1: the lab verdict (``lab/_sched/analysis.py``) fed
adversarial ledgers - what a 4-laptop run could really leave behind. Every
case states what the verdict must be from D117 and criterion 1, not from the
code."""

from __future__ import annotations

import random
from typing import Any

import pytest

from _sched.analysis import Planned, analyse, parse_ledger

JOB = "job_1"
N1, N2 = "node_1", "node_2"


def att(attempt_id: str, state: str, node: str = N1, ftype: str | None = None) -> Any:
    return {
        "attempt_id": attempt_id,
        "state": state,
        "node_id": node,
        "failure_type": ftype,
    }


def view(state: str, attempts: list[Any], ftype: str | None = None) -> Any:
    return {"state": state, "failure_type": ftype, "attempts": attempts}


def line(
    kind: str, attempt: str, job: str = JOB, at: float = 1.0, host: str = "h"
) -> str:
    return f"{at:.6f} {kind} {job} {attempt} {host}"


def run(
    views: dict[str, Any],
    lines: list[str],
    *,
    planned: list[Planned] | None = None,
    partitioned: tuple[str, ...] = (),
) -> Any:
    plan = planned or [Planned(JOB, 0, 5)]
    return analyse(plan, views, parse_ledger(lines), partitioned_nodes=partitioned)


def test_a_clean_job_passes() -> None:
    v = run(
        {JOB: view("succeeded", [att("a1", "done")])},
        [line("start", "a1"), line("end", "a1")],
    )
    assert v.criterion_1 and not v.committed_without_evidence


def test_a_job_still_running_or_unknown_is_lost() -> None:
    assert not run({JOB: view("running", [att("a1", "running")])}, []).criterion_1
    assert not run({JOB: None}, []).criterion_1
    assert not run({JOB: view("queued", [])}, []).criterion_1


def test_two_done_attempts_are_a_duplicate_commit() -> None:
    v = run(
        {JOB: view("succeeded", [att("a1", "done"), att("a2", "done", N2)])},
        [line("end", "a1"), line("end", "a2")],
    )
    assert v.duplicate_commits and not v.criterion_1


def test_zero_done_attempts_on_a_succeeded_job_is_a_duplicate_commit_defect() -> None:
    v = run({JOB: view("succeeded", [att("a1", "failed")])}, [line("end", "a1")])
    assert not v.criterion_1


def test_a_killed_run_that_never_ended_is_not_a_duplicate() -> None:
    v = run(
        {JOB: view("succeeded", [att("a1", "lost"), att("a2", "done", N2)])},
        [
            line("start", "a1"),
            line("start", "a2", host="h2"),
            line("end", "a2", host="h2"),
        ],
    )
    assert v.criterion_1 and not v.duplicate_executions


def test_a_lost_attempt_that_completed_on_a_killed_not_partitioned_node_is_a_duplicate() -> (
    None
):
    v = run(
        {JOB: view("succeeded", [att("a1", "lost"), att("a2", "done", N2)])},
        [line("end", "a1"), line("end", "a2")],
    )
    assert v.duplicate_executions and not v.criterion_1


def test_partition_overlap_is_allowed_and_counted_separately() -> None:
    v = run(
        {JOB: view("succeeded", [att("a1", "lost", N1), att("a2", "done", N2)])},
        [line("end", "a1"), line("end", "a2")],
        partitioned=(N1,),
    )
    assert v.criterion_1 and v.partition_overlaps == [f"{JOB}/a1"]


def test_partition_flag_for_the_wrong_node_does_not_excuse_the_duplicate() -> None:
    v = run(
        {JOB: view("succeeded", [att("a1", "lost", N1), att("a2", "done", N2)])},
        [line("end", "a1"), line("end", "a2")],
        partitioned=(N2,),
    )
    assert not v.criterion_1


def test_the_same_attempt_executed_twice_is_a_duplicate_execution() -> None:
    """At-least-once command delivery executed attempt a1 twice on the same
    node (two start/end pairs with the same attempt id): the very thing D113's
    dedupe exists to prevent. The verdict only looks for *other* attempts."""
    v = run(
        {JOB: view("succeeded", [att("a1", "done")])},
        [
            line("start", "a1"),
            line("end", "a1"),
            line("start", "a1"),
            line("end", "a1"),
        ],
    )
    assert v.duplicate_executions and not v.criterion_1


def test_a_missing_ledger_cannot_make_the_run_pass() -> None:
    """No ledger evidence at all for a succeeded job: duplicates on that laptop
    would be invisible, so the verdict must not say PASS / exit 0."""
    v = run({JOB: view("succeeded", [att("a1", "done")])}, [])
    assert v.committed_without_evidence == [JOB]
    assert not v.criterion_1


def test_a_job_meant_to_succeed_that_exhausted_its_retries_is_not_a_pass() -> None:
    attempts = [att(f"a{i}", "lost") for i in range(5)]
    v = run({JOB: view("failed", attempts, "node_lost")}, [])
    assert not v.criterion_1


def test_a_failure_meant_to_fail_is_correct_and_a_failure_meant_to_succeed_is_lost() -> (
    None
):
    planned = [Planned("job_f", 3, 5), Planned("job_s", 0, 5)]
    views = {
        "job_f": view(
            "failed", [att("f1", "failed", ftype="user_error")], "user_error"
        ),
        "job_s": view(
            "failed", [att("s1", "failed", ftype="user_error")], "user_error"
        ),
    }
    lines = [line("end", "f1", "job_f"), line("end", "s1", "job_s")]
    v = analyse(planned, views, parse_ledger(lines))
    assert v.lost == ["job_s: meant to succeed, failed user_error"]


def test_a_retryable_failure_with_attempts_left_is_lost() -> None:
    v = run(
        {JOB: view("failed", [att("a1", "failed", ftype="node_lost")], "node_lost")}, []
    )
    assert not v.criterion_1


# ------------------------------------------------------- hostile ledger text


def test_garbled_and_truncated_lines_are_skipped_never_fatal() -> None:
    junk = [
        "",
        "   ",
        "1.0 end",
        "x end job att h",
        "1.0 end job att h extra",
        "\x00\x00\x00",
        "nan end job a1 h",
        "1.0 END job a1 h",
        "1" * 100_000,
        "\ud800 end job a1 h",
    ]
    parsed = parse_ledger([*junk, line("end", "a1")])
    assert any(x.attempt_id == "a1" and x.kind == "end" for x in parsed)


def test_a_torn_end_line_is_treated_as_no_evidence_not_a_pass_of_a_duplicate() -> None:
    v = run(
        {JOB: view("succeeded", [att("a1", "done")])},
        [line("start", "a1"), line("end", "a1")[:20]],
    )
    assert v.committed_without_evidence == [JOB]


def test_a_windows_ledger_with_crlf_parses() -> None:
    text = (line("start", "a1") + "\r\n" + line("end", "a1") + "\r\n").splitlines()
    assert [x.kind for x in parse_ledger(text)] == ["start", "end"]


@pytest.mark.parametrize("seed", range(20))
def test_the_verdict_ignores_clock_skew_and_line_order(seed: int) -> None:
    """Laptop clocks disagree by minutes and files are concatenated in any
    order: the verdict uses neither the timestamps nor the order."""
    rng = random.Random(seed)  # noqa: S311
    views = {
        JOB: view("succeeded", [att("a1", "lost", N1), att("a2", "done", N2)]),
        "job_2": view("succeeded", [att("b1", "done")]),
    }
    plan = [Planned(JOB, 0, 5), Planned("job_2", 0, 5)]

    def build(skew: float) -> list[str]:
        rows = [
            line("start", "a1", at=100 + skew),
            line("end", "a1", at=130 + skew),
            line("start", "a2", at=140),
            line("end", "a2", at=150),
            line("start", "b1", "job_2", at=10),
            line("end", "b1", "job_2", at=20),
        ]
        rng.shuffle(rows)
        return rows

    base = analyse(plan, views, parse_ledger(build(0)), partitioned_nodes=(N1,))
    skewed = analyse(
        plan,
        views,
        parse_ledger(build(rng.uniform(-900, 900))),
        partitioned_nodes=(N1,),
    )
    assert base.as_dict() == skewed.as_dict()
