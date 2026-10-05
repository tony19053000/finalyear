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
from typing import Any, Final

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_attempt_id, validate_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import (
    ProtoDecodeError,
    attempt_state_from_proto,
    failure_type_from_proto,
)
from meshforge.common.states import AttemptState
from meshforge.controller.jobs.execution_store import StatusReport, TransferRow
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
RUNTIMES: Final = frozenset({"docker", "subprocess", "venv"})
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
_ENV_HASH: Final = re.compile(r"[0-9a-f]{64}")
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


MAX_LOG_REPORT_BYTES: Final = 1 << 40
"""Bound on ``FinalLog.bytes`` (the agent's cap is far below)."""


def _final_logs(
    status: agent_pb2.AttemptStatus,
) -> dict[str, dict[str, Any]] | str:
    """D71: ``{"stdout": {"digest", "bytes", "truncated"}, ...}`` or why refused."""
    logs: dict[str, dict[str, Any]] = {}
    present = (
        ("stdout", status.HasField("stdout_log"), status.stdout_log),
        ("stderr", status.HasField("stderr_log"), status.stderr_log),
    )
    for stream, has, final in present:
        if not has:
            continue
        field_name = f"{stream}_log"
        if final.digest and not _DIGEST.fullmatch(final.digest):
            return f"AttemptStatus.{field_name}.digest is not sha256:<64 hex>"
        if final.bytes > MAX_LOG_REPORT_BYTES:
            return f"AttemptStatus.{field_name}.bytes is over {MAX_LOG_REPORT_BYTES}"
        logs[stream] = {
            "digest": final.digest or None,
            "bytes": int(final.bytes),
            "truncated": bool(final.truncated),
        }
    return logs


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
    logs = _final_logs(status)
    if isinstance(logs, str):
        return logs
    transfers = _transfers(status, state)
    if isinstance(transfers, str):
        return transfers
    if status.env_hash and not _ENV_HASH.fullmatch(status.env_hash):
        return "AttemptStatus.env_hash is not 64 lowercase hex characters"
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
        logs=logs,
        transfers=transfers,
        env_hash=status.env_hash or None,
    )


MAX_TRANSFERS: Final = 17
"""The project tree + at most 16 named inputs."""
MAX_TRANSFER_SOURCES: Final = 17
"""The controller + at most 16 listed peers."""
_DROPPED: Final = re.compile(r"[a-z_]{1,32}")
_NODE_ID: Final = re.compile(r"node_[0-9A-HJKMNP-TV-Z]{26}")
_INT32: Final = 2**31 - 1


def _ms(value: float) -> int | None:
    if not math.isfinite(value) or not 0 <= value <= MAX_PHASE_S:
        return None
    return round(value * 1000)


def _transfers(
    status: agent_pb2.AttemptStatus, state: AttemptState
) -> tuple[TransferRow, ...] | str:
    """``AttemptStatus.transfers`` (V0.5 G5, D93) -> transfer_event rows: per
    tree a ``cache`` row (hits) and one row per source; bounded, validated,
    rows of one (tree, source) merged. Only on a terminal report."""
    if not status.transfers:
        return ()
    if state not in (AttemptState.DONE, AttemptState.FAILED):
        return "AttemptStatus.transfers is set on a non-terminal report"
    if len(status.transfers) > MAX_TRANSFERS:
        return f"AttemptStatus.transfers has more than {MAX_TRANSFERS} entries"
    rows: dict[tuple[str, str], TransferRow] = {}

    def add(row: TransferRow) -> None:
        key = (row.tree_id, row.source)
        old = rows.get(key)
        if old is None:
            rows[key] = row
            return
        rows[key] = TransferRow(
            tree_id=row.tree_id,
            source=row.source,
            bytes=old.bytes + row.bytes,
            duration_ms=old.duration_ms + row.duration_ms,
            units=min(_INT32, old.units + row.units),
            failures=min(_INT32, old.failures + row.failures),
            resumed_chunks=min(_INT32, old.resumed_chunks + row.resumed_chunks),
            dropped=old.dropped or row.dropped,
        )

    for index, summary in enumerate(status.transfers):
        where = f"AttemptStatus.transfers[{index}]"
        if not _DIGEST.fullmatch(summary.tree_id):
            return f"{where}.tree_id is not sha256:<64 hex>"
        if len(summary.sources) > MAX_TRANSFER_SOURCES:
            return f"{where} has more than {MAX_TRANSFER_SOURCES} sources"
        duration = _ms(summary.duration_s)
        if duration is None:
            return f"{where}.duration_s is not a duration"
        counts = (summary.blobs_hit, summary.corrupt_refetches, summary.chunks_resumed)
        if any(c > _INT32 for c in counts) or any(
            s.units > _INT32 or s.failures > _INT32 for s in summary.sources
        ):
            return f"{where}: a count is over 2^31-1"
        add(
            TransferRow(
                tree_id=summary.tree_id,
                source="cache",
                bytes=int(summary.bytes_hit),
                duration_ms=duration,
                units=int(summary.blobs_hit),
                failures=int(summary.corrupt_refetches),
                resumed_chunks=int(summary.chunks_resumed),
            )
        )
        for source in summary.sources:
            name = source.node_id or "controller"
            if source.node_id and not _NODE_ID.fullmatch(source.node_id):
                return f"{where}: a source node_id is not a canonical node id"
            if source.dropped and not _DROPPED.fullmatch(source.dropped):
                return f"{where}: a source's dropped reason is malformed"
            spent = _ms(source.duration_s)
            if spent is None:
                return f"{where}: a source duration_s is not a duration"
            add(
                TransferRow(
                    tree_id=summary.tree_id,
                    source=name,
                    bytes=int(source.bytes),
                    duration_ms=spent,
                    units=int(source.units),
                    failures=int(source.failures),
                    dropped=source.dropped or None,
                )
            )
    return tuple(rows.values())
