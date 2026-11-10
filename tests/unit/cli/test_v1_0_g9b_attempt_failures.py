"""V1.0 G9-B item 1: the CLI shows why an attempt ended.

``compute jobs JOB_ID`` adds a ``failed <type>: <detail>`` row under an attempt
that ended with a failure (a job that succeeded on retry has no job-level
``failure_detail``, so this is where the first attempt's failure shows), and
``compute run -f``'s retry line carries the ended attempt's detail. Server
text is untrusted: flattened, scrubbed, capped.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console

from meshforge.cli import job_commands
from meshforge.cli.render import attempt_failure_text, job_detail_table

pytestmark = [pytest.mark.unit]


def _render(payload: dict[str, object]) -> str:
    out = io.StringIO()
    Console(file=out, width=600, force_terminal=True, color_system=None).print(
        job_detail_table(payload)
    )
    return out.getvalue()


def _job(*attempts: dict[str, Any]) -> dict[str, object]:
    return {
        "job_id": "job_x",
        "name": "n",
        "state": "succeeded",
        "failure_type": None,
        "failure_detail": None,
        "attempts": list(attempts),
    }


FAILED = {
    "attempt_id": "att_1",
    "node_id": "node_a",
    "state": "failed",
    "fencing_token": 1,
    "failure_type": "transfer_error",
    "detail": "RemoteProtocolError: peer closed connection",
}
OK = {"attempt_id": "att_2", "node_id": "node_b", "state": "done", "fencing_token": 2}


def test_a_failed_attempt_of_a_succeeded_job_shows_type_and_detail() -> None:
    text = _render(_job(FAILED, OK))
    lines = [
        line for line in text.splitlines() if "failed" in line and "transfer" in line
    ]
    assert len(lines) == 1, text
    assert "transfer_error: RemoteProtocolError: peer closed connection" in lines[0]
    # the succeeded attempt has no failed row
    assert text.count("transfer_error") == 1


def test_type_without_detail_and_no_failure_at_all() -> None:
    assert attempt_failure_text({"failure_type": "node_lost", "detail": None}) == (
        "node_lost"
    )
    assert attempt_failure_text({"failure_type": "node_lost", "detail": "  "}) == (
        "node_lost"
    )
    assert attempt_failure_text({"failure_type": None, "detail": "x"}) == ""
    assert attempt_failure_text({}) == ""
    assert (
        attempt_failure_text({"failure_type": "node_lost", "detail": "node_revoked"})
        == "node_lost: node_revoked"
    )


def test_hostile_detail_cannot_drive_the_terminal_and_secrets_are_scrubbed() -> None:
    hostile = {
        **FAILED,
        "detail": "\x1b[2Jboom\r\nforged line token=hunter2hunter2hunter2",
    }
    text = _render(_job(hostile))
    assert "\x1b[2J" not in text
    assert "hunter2hunter2hunter2" not in text
    assert "forged line" in text  # flattened onto the same row, still readable


def test_a_long_detail_is_capped_not_unbounded() -> None:
    text = _render(_job({**FAILED, "detail": "x" * 5000}))
    assert "x" * 300 not in text
    assert "..." in text


def test_the_retry_line_carries_the_detail(capsys: pytest.CaptureFixture[str]) -> None:
    job_commands._print_retry(
        {
            "retry": True,
            "attempt_id": "att_2",
            "fencing_token": 2,
            "previous_attempt_id": "att_1",
            "previous_state": "failed",
            "previous_failure_type": "transfer_error",
            "previous_detail": "RemoteProtocolError: peer closed connection",
        }
    )
    err = capsys.readouterr().err
    assert (
        "the attempt ended (transfer_error: RemoteProtocolError: peer closed "
        "connection); the job was retried - following attempt 2"
    ) in err


@pytest.mark.parametrize("detail", [None, "", "   ", 7, ["x"]])
def test_the_retry_line_without_a_usable_detail_is_as_before(
    capsys: pytest.CaptureFixture[str], detail: object
) -> None:
    job_commands._print_retry(
        {
            "retry": True,
            "fencing_token": 3,
            "previous_failure_type": "node_lost",
            "previous_detail": detail,
        }
    )
    assert "the attempt ended (node_lost); the job was retried" in (
        capsys.readouterr().err
    )


def test_the_retry_detail_is_flattened_and_scrubbed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    job_commands._print_retry(
        {
            "retry": True,
            "fencing_token": 2,
            "previous_failure_type": "user_error",
            "previous_detail": "bad\x1b[31m\nline password=s3cr3tvalue99",
        }
    )
    err = capsys.readouterr().err
    assert "\x1b" not in err
    assert "s3cr3tvalue99" not in err
    assert err.count("\n") == 1
