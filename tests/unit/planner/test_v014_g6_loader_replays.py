"""V0.14 G6: which decision's replay a record is evaluated on (the loader's
pure half, ``attach_replays``; D259/D314).

* a whole job's record takes its own attempt's ``dispatched`` decision;
* a split job's record (the combine's row: no decision of its own) takes the
  job's **first** dispatched decision - the one that planned the lanes - never
  a later re-plan of what was left (a lane lost mid-run);
* the order of the rows is the decision id order, whatever batch they came
  from; a record without any decision keeps ``replay`` None (``no_replay``);
* a NULL replay stays None, with its decision id.
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.planner_eval.loader import attach_replays
from meshforge.controller.planner_eval.model import EvalRecord

pytestmark = pytest.mark.unit


def _record(attempt: str, job: str) -> EvalRecord:
    return EvalRecord(
        campaign_id="camp",
        group_id="g",
        config_id="c",
        repetition=0,
        attempt_id=attempt,
        job_id=job,
    )


def test_a_split_job_takes_its_first_dispatched_decision_not_a_replan() -> None:
    rows = [
        (10, "job_split", "att_lane1", {"replay": "first plan"}),
        (11, "job_whole", "att_whole", {"replay": "whole"}),
        (17, "job_split", "att_lane9", {"replay": "re-plan after a lost lane"}),
    ]
    split, whole = attach_replays(
        [_record("att_combine", "job_split"), _record("att_whole", "job_whole")],
        rows,
    )
    assert (split.decision_id, split.replay) == (10, {"replay": "first plan"})
    assert (whole.decision_id, whole.replay) == (11, {"replay": "whole"})


def test_the_records_own_attempt_wins_over_the_jobs_first_decision() -> None:
    rows = [
        (3, "job_a", "att_1", {"n": 1}),
        (4, "job_a", "att_2", {"n": 2}),  # a retry of the same job
    ]
    (record,) = attach_replays([_record("att_2", "job_a")], rows)
    assert (record.decision_id, record.replay) == (4, {"n": 2})


def test_no_decision_keeps_no_replay_and_a_null_replay_stays_null() -> None:
    rows = [(5, "job_b", "att_b", None)]
    none, null = attach_replays(
        [_record("att_x", "job_none"), _record("att_b", "job_b")], rows
    )
    assert (none.decision_id, none.replay) == (None, None)
    assert (null.decision_id, null.replay) == (5, None)
