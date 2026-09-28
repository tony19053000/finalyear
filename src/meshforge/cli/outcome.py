"""What ``compute run --follow`` exits with (V0.4 G5, D76) - pure, unit-tested.

* ``succeeded`` -> 0;
* ``failed`` with ``user_error`` -> the workload's exit code, clamped to 1-125
  (125 when it has none, e.g. killed by a signal) - so ``compute run -f`` can
  stand in for running the script locally in a shell pipeline;
* ``cancelled`` -> :attr:`ExitCode.JOB_CANCELLED` (128);
* any other failure -> :attr:`ExitCode.JOB_FAILED` (126). Codes 126+ can never be
  a clamped workload code, so a script can tell "my program failed" from "the
  fabric failed". Ctrl-C while following exits :attr:`ExitCode.INTERRUPTED` (130)
  and leaves the job running.
"""

from __future__ import annotations

from typing import Final

from meshforge.common.exit_codes import ExitCode

__all__ = ["MAX_WORKLOAD_EXIT", "job_exit_code"]

MAX_WORKLOAD_EXIT: Final = 125


def job_exit_code(state: str, failure_type: str | None, exit_code: int | None) -> int:
    """The process exit code for a job's final state (see module docstring)."""
    if state == "succeeded":
        return int(ExitCode.OK)
    if state == "cancelled":
        return int(ExitCode.JOB_CANCELLED)
    if state == "failed" and failure_type == "user_error":
        if exit_code is None or exit_code <= 0:
            return MAX_WORKLOAD_EXIT
        return min(exit_code, MAX_WORKLOAD_EXIT)
    return int(ExitCode.JOB_FAILED)
