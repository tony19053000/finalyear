"""Every exit code ``compute`` returns, and the one mapping to them (V0.8 G6, D178).

The table (docs/CLI.md "Exit codes" is the user-facing copy; a unit test keeps
the two in step):

====  =================  ===========================================================
code  name               meaning
====  =================  ===========================================================
0     OK                 done
1     FAILURE            the command ran and failed: the controller refused the
                         request for a reason not listed below, a local file could
                         not be read or written, a node failed to prepare...
2     USAGE_ERROR        bad arguments or options (Click's own code), or the
                         controller rejected a value we sent (``invalid_input``)
3     AUTH               not logged in, login expired/revoked, wrong password, not
                         allowed (401/403), the controller's certificate does not
                         match the fingerprint given
4     CONFIG_ERROR       the local configuration is unusable (config.toml, token
                         file, CA file, credentials folder)
6     UNAVAILABLE        the controller could not be reached, timed out, or cannot
                         answer right now (503) - retrying later may work (D18)
8     NOT_FOUND          the job, node, token, secret, input or invite does not exist
126   JOB_FAILED         ``run -f``: the fabric could not run or finish the job
128   JOB_CANCELLED      ``run -f``: the job was cancelled
130   INTERRUPTED        Ctrl-C (``run -f`` / ``logs -f`` detach; the job keeps running)
1-125 (workload)         ``run -f``: the workload's own exit code, clamped (D76)
====  =================  ===========================================================

``compute fabric join`` runs ``meshforge-agent join`` and returns *its* code,
which uses the same numbers (4 config, 5 already enrolled, 6 controller
unreachable).

**Why 6 and 8, not D178's 5 and 4.** Every MeshForge program shares one table
(:class:`meshforge.common.exit_codes.ExitCode`): 4 is already ``CONFIG_ERROR``,
5 ``CONFLICT`` and 6 ``UNAVAILABLE`` (D18) for the controller, the agent and
``compute`` since V0.3, scripts and docs/OPERATIONS.md depend on them, and
``compute fabric join`` forwards the agent's code - renumbering would make one
number mean two things. 3 (``NOT_IMPLEMENTED``, never returned by any program)
becomes ``AUTH``, matching D178; ``NOT_FOUND`` takes the next free number.
Likewise "1 job failed" is D76's richer ``run -f`` outcome (the workload's own
code, 126 for a fabric failure), which a script can already branch on. Every
value here that exists in ``ExitCode`` is the same number there (unit-tested).
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = ["MAX_WORKLOAD_EXIT", "CliExit", "exit_code_for", "job_exit_code"]


class CliExit(IntEnum):
    """The process exit codes of ``compute`` (module docstring)."""

    OK = 0
    FAILURE = 1
    USAGE_ERROR = 2
    AUTH = 3
    CONFIG_ERROR = 4
    CONFLICT = 5
    """Only from ``compute fabric join`` (the agent's "already enrolled")."""
    UNAVAILABLE = 6
    BUSY = 7
    """Not returned by ``compute`` itself; kept so the table is the shared one."""
    NOT_FOUND = 8
    JOB_FAILED = 126
    JOB_CANCELLED = 128
    INTERRUPTED = 130


MAX_WORKLOAD_EXIT: Final = 125

_BY_FAILURE: Final[dict[FailureType, CliExit]] = {
    FailureType.NETWORK_ERROR: CliExit.UNAVAILABLE,
    FailureType.TIMEOUT: CliExit.UNAVAILABLE,
    FailureType.UNAUTHORIZED: CliExit.AUTH,
    FailureType.NOT_FOUND: CliExit.NOT_FOUND,
    FailureType.INVALID_INPUT: CliExit.USAGE_ERROR,
    FailureType.CONFIG_ERROR: CliExit.CONFIG_ERROR,
}
"""Typed client failure -> exit code; any other failure type is ``FAILURE``.

``INTERNAL_ERROR`` (an answer this build cannot read: a proxy's error page, an
unknown code) stays ``FAILURE`` and not ``UNAVAILABLE`` on purpose: a polling
script must stop rather than hammer an endpoint that answers something unusable."""


def exit_code_for(error: MeshForgeError) -> CliExit:
    """The exit code for a typed failure (a :class:`~.client.ControllerError`)."""
    return _BY_FAILURE.get(error.failure_type, CliExit.FAILURE)


def job_exit_code(state: str, failure_type: str | None, exit_code: int | None) -> int:
    """What ``compute run --follow`` exits with for a job's final state (D76).

    * ``succeeded`` -> 0;
    * ``failed`` with ``user_error`` -> the workload's exit code, clamped to
      1-125 (125 when it has none, e.g. killed by a signal) - so ``compute run
      -f`` can stand in for running the script locally in a shell pipeline;
    * ``cancelled`` -> 128;
    * any other failure -> 126. Codes 126+ can never be a clamped workload
      code, so a script can tell "my program failed" from "the fabric failed".
    """
    if state == "succeeded":
        return int(CliExit.OK)
    if state == "cancelled":
        return int(CliExit.JOB_CANCELLED)
    if state == "failed" and failure_type == "user_error":
        if exit_code is None or exit_code <= 0:
            return MAX_WORKLOAD_EXIT
        return min(exit_code, MAX_WORKLOAD_EXIT)
    return int(CliExit.JOB_FAILED)
