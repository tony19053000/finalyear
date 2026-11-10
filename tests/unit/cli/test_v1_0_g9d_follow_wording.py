"""V1.0 G9-D: ``compute run -f`` of a split job - a lane that ended ``done``
followed by the combine is the next step, not a retry (L1 C10.10-c printed
"the attempt ended (done); the job was retried")."""

from __future__ import annotations

import pytest

from meshforge.cli import job_commands

pytestmark = pytest.mark.unit

FRAME = {
    "retry": True,
    "attempt_id": "att_2",
    "fencing_token": 2,
    "previous_attempt_id": "att_1",
    "previous_state": "done",
    "previous_failure_type": None,
    "previous_detail": None,
    "role": "combine",
    "previous_role": "lane",
}


def test_lane_then_combine_reads_as_the_next_step(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_commands._print_retry(FRAME)
    err = capsys.readouterr().err
    assert "the lane finished; following the combine step (attempt 2)" in err
    assert "retried" not in err


def test_a_controller_without_roles_still_avoids_the_retry_wording(
    capsys: pytest.CaptureFixture[str],
) -> None:
    old = {k: v for k, v in FRAME.items() if k not in ("role", "previous_role")}
    job_commands._print_retry(old)
    err = capsys.readouterr().err
    assert "the attempt finished; following the attempt (attempt 2)" in err
    assert "retried" not in err


def test_unknown_roles_are_never_echoed(capsys: pytest.CaptureFixture[str]) -> None:
    job_commands._print_retry({**FRAME, "role": "\x1b[2Jevil", "previous_role": 3})
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "the attempt finished; following the attempt (attempt 2)" in err


def test_a_failed_attempt_still_reads_as_a_retry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_commands._print_retry(
        {**FRAME, "previous_state": "failed", "previous_failure_type": "user_error"}
    )
    assert "the job was retried - following attempt 2" in capsys.readouterr().err
