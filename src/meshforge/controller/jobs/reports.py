"""Validate an agent's ``AttemptStatus`` before anything is stored (V0.4 G4).

The report comes from the network, so every field is bounded first (checklist
item 9) and a report that breaks a bound is **refused, never cleaned** (the D22
rule of the Hello strings): the caller drops it, logs the reason (which never
echoes the value) and counts it. :func:`parse_status` returns the typed
:class:`~.execution_store.StatusReport` or the reason string.
"""

from __future__ import annotations

import math
import re
from typing import Final

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_attempt_id, validate_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    ProtoDecodeError,
    attempt_state_from_proto,
    failure_type_from_proto,
)
from meshforge.common.states import AttemptState
from meshforge.controller.jobs.execution_store import StatusReport
from meshforge.controller.jobs.progress import (
    AGENT_REPORTABLE,
    MAX_PHASE_S,
    AgentPhases,
)
from meshforge.controller.session.limits import first_non_printable

__all__ = [
    "FAILURE_MESSAGE_MAX_LENGTH",
    "MAX_MISSING_OUTPUTS",
    "MISSING_OUTPUT_MAX_LENGTH",
    "RUNTIMES",
    "parse_status",
]

FAILURE_MESSAGE_MAX_LENGTH: Final = 1024
"""``Failure.message`` - matches ``attempt.detail`` ``String(1024)``."""
MAX_MISSING_OUTPUTS: Final = 64
"""Same as the manifest's ``outputs`` cap: one entry per declared output."""
MISSING_OUTPUT_MAX_LENGTH: Final = 512
MAX_FENCING_TOKEN: Final = 2**63 - 1
"""``attempt.fencing_token`` is a BIGINT."""
RUNTIMES: Final = frozenset({"docker", "subprocess"})
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
_MAX_SIGNAL: Final = 64


def _printable(where: str, value: str, limit: int) -> str | None:
    if len(value) > limit:
        return f"{where} is {len(value)} characters; the maximum is {limit}"
    bad = first_non_printable(value)
    if bad is not None:
        return f"{where} has a non-printable character ({bad[1]}) at {bad[0]}"
    return None


def _phases(status: agent_pb2.AttemptStatus) -> AgentPhases | str | None:
    if not status.HasField("phases"):
        return None
    p = status.phases
    values: dict[str, float | None] = {}
    for name in ("stage_s", "env_s", "run_s", "collect_s"):
        if not p.HasField(name):
            values[name] = None
            continue
        value = getattr(p, name)
        if not (math.isfinite(value) and 0 <= value <= MAX_PHASE_S):
            return f"AttemptStatus.phases.{name} must be a finite number in [0, {MAX_PHASE_S:g}]"
        values[name] = value
    return AgentPhases(**values)


def parse_status(status: agent_pb2.AttemptStatus) -> StatusReport | str:
    """The validated report, or why it is refused (see module docstring)."""
    try:
        attempt_id = str(validate_attempt_id(status.attempt_id))
        validate_job_id(status.job_id)
    except InvalidIdError:
        return "AttemptStatus.attempt_id/job_id is not a canonical ULID id"
    if not 1 <= status.fencing_token <= MAX_FENCING_TOKEN:
        return "AttemptStatus.fencing_token is outside [1, 2^63-1]"
    try:
        state = attempt_state_from_proto(status.state)
    except ProtoDecodeError:
        return f"AttemptStatus.state is unspecified or unknown ({int(status.state)})"
    if state not in AGENT_REPORTABLE:
        return f"AttemptStatus.state {state.value} is not an agent state"
    failure_type: FailureType | None = None
    detail: str | None = None
    if status.HasField("failure"):
        if state is not AttemptState.FAILED:
            return "AttemptStatus.failure is set but the state is not FAILED"
        try:
            failure_type = failure_type_from_proto(status.failure.failure_type)
        except ProtoDecodeError:
            return "AttemptStatus.failure.failure_type is unspecified or unknown"
        reason = _printable(
            "AttemptStatus.failure.message",
            status.failure.message,
            FAILURE_MESSAGE_MAX_LENGTH,
        )
        if reason is not None:
            return reason
        detail = status.failure.message or None
    elif state is AttemptState.FAILED:
        return "AttemptStatus is FAILED without a failure"
    if status.output_digest and not _DIGEST.fullmatch(status.output_digest):
        return "AttemptStatus.output_digest is not sha256:<64 hex>"
    if len(status.missing_outputs) > MAX_MISSING_OUTPUTS:
        return (
            f"AttemptStatus.missing_outputs has {len(status.missing_outputs)} "
            f"entries; the maximum is {MAX_MISSING_OUTPUTS}"
        )
    for index, item in enumerate(status.missing_outputs):
        reason = _printable(
            f"AttemptStatus.missing_outputs[{index}]", item, MISSING_OUTPUT_MAX_LENGTH
        )
        if reason is not None:
            return reason
    if status.runtime and status.runtime not in RUNTIMES:
        return "AttemptStatus.runtime is not a known runtime"
    if status.HasField("signal") and not 1 <= status.signal <= _MAX_SIGNAL:
        return f"AttemptStatus.signal is outside [1, {_MAX_SIGNAL}]"
    phases = _phases(status)
    if isinstance(phases, str):
        return phases
    return StatusReport(
        attempt_id=attempt_id,
        fencing_token=int(status.fencing_token),
        state=state,
        failure_type=failure_type,
        detail=detail,
        exit_code=status.exit_code if status.HasField("exit_code") else None,
        runtime=status.runtime or None,
        phases=phases,
        output_digest=status.output_digest or None,
        missing_outputs=tuple(status.missing_outputs),
    )
