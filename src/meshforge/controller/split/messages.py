"""Validate a lane's chunk messages before anything is looked up (V0.10 G4,
D263). Pure.

They come from the network: every field is bounded first and a message that
breaks a bound is **refused, never cleaned** (the rule of
:mod:`meshforge.controller.jobs.reports`): the caller drops it (a
``ChunkResult`` is acked so it is not replayed for ever), logs the reason -
which never echoes the value - and counts it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Final

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_attempt_id, validate_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.proto_codec import ProtoDecodeError, failure_type_from_proto
from meshforge.controller.jobs.reports import FAILURE_MESSAGE_MAX_LENGTH
from meshforge.controller.session.limits import first_non_printable

__all__ = [
    "MAX_HELD_LEASES",
    "MAX_WANT",
    "ChunkRequestIn",
    "ChunkResultIn",
    "LaneRef",
    "LeaseRenewIn",
    "parse_chunk_request",
    "parse_chunk_result",
    "parse_lease_renew",
]

MAX_WANT: Final = 2
"""D263: prefetch window 1 - one chunk running, one staging."""
MAX_HELD_LEASES: Final = 64
_MAX_ID: Final = 2**63 - 1
_MAX_PHASE_S: Final = 30 * 86_400.0
_DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
_OUTCOMES: Final = {
    agent_pb2.ChunkResult.OUTCOME_DONE: "done",
    agent_pb2.ChunkResult.OUTCOME_FAILED: "failed",
    agent_pb2.ChunkResult.OUTCOME_RELEASED: "released",
}


@dataclass(frozen=True, slots=True)
class LaneRef:
    job_id: str
    attempt_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class ChunkRequestIn:
    lane: LaneRef
    want: int
    held: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class ChunkResultIn:
    lane: LaneRef
    chunk_index: int
    item_id: int
    lease_seq: int
    outcome: str
    """``done`` | ``failed`` | ``released``."""
    result_digest: str | None
    failure: tuple[FailureType, str, bool] | None
    exit_code: int | None
    phases: dict[str, float]


@dataclass(frozen=True, slots=True)
class LeaseRenewIn:
    lane: LaneRef
    leases: tuple[tuple[int, int], ...]


def _lane(where: str, job_id: str, attempt_id: str, token: int) -> LaneRef | str:
    try:
        attempt = str(validate_attempt_id(attempt_id))
        job = str(validate_job_id(job_id))
    except InvalidIdError:
        return f"{where}.attempt_id/job_id is not a canonical id"
    if not 1 <= token <= _MAX_ID:
        return f"{where}.fencing_token is outside [1, 2^63-1]"
    return LaneRef(job, attempt, int(token))


def _leases(
    where: str, refs: list[agent_pb2.ChunkLeaseRef]
) -> tuple[tuple[int, int], ...] | str:
    if len(refs) > MAX_HELD_LEASES:
        return f"{where} lists {len(refs)} leases; the maximum is {MAX_HELD_LEASES}"
    out: list[tuple[int, int]] = []
    for ref in refs:
        if not (1 <= ref.item_id <= _MAX_ID and 1 <= ref.lease_seq <= _MAX_ID):
            return f"{where} has a lease outside [1, 2^63-1]"
        out.append((int(ref.item_id), int(ref.lease_seq)))
    return tuple(out)


def parse_chunk_request(msg: agent_pb2.ChunkRequest) -> ChunkRequestIn | str:
    lane = _lane("ChunkRequest", msg.job_id, msg.attempt_id, msg.fencing_token)
    if isinstance(lane, str):
        return lane
    if not 0 <= msg.want <= MAX_WANT:
        return f"ChunkRequest.want is outside [0, {MAX_WANT}]"
    held = _leases("ChunkRequest.held", list(msg.held))
    if isinstance(held, str):
        return held
    return ChunkRequestIn(lane, int(msg.want), held)


def parse_lease_renew(msg: agent_pb2.LeaseRenew) -> LeaseRenewIn | str:
    lane = _lane("LeaseRenew", msg.job_id, msg.attempt_id, msg.fencing_token)
    if isinstance(lane, str):
        return lane
    leases = _leases("LeaseRenew.leases", list(msg.leases))
    if isinstance(leases, str):
        return leases
    return LeaseRenewIn(lane, leases)


def parse_chunk_result(msg: agent_pb2.ChunkResult) -> ChunkResultIn | str:
    lane = _lane("ChunkResult", msg.job_id, msg.attempt_id, msg.fencing_token)
    if isinstance(lane, str):
        return lane
    if not (1 <= msg.item_id <= _MAX_ID and 1 <= msg.lease_seq <= _MAX_ID):
        return "ChunkResult.item_id/lease_seq is outside [1, 2^63-1]"
    outcome = _OUTCOMES.get(msg.outcome)
    if outcome is None:
        return f"ChunkResult.outcome is unspecified or unknown ({int(msg.outcome)})"
    digest: str | None = None
    if outcome == "done":
        if not _DIGEST.fullmatch(msg.result_digest):
            return "ChunkResult.result_digest is not sha256:<64 hex>"
        digest = msg.result_digest
    elif msg.result_digest:
        return "ChunkResult.result_digest is set but the outcome is not done"
    failure: tuple[FailureType, str, bool] | None = None
    if msg.HasField("failure"):
        if outcome != "failed":
            return "ChunkResult.failure is set but the outcome is not failed"
        try:
            kind = failure_type_from_proto(msg.failure.failure_type)
        except ProtoDecodeError:
            return "ChunkResult.failure.failure_type is unspecified or unknown"
        text = msg.failure.message
        if len(text) > FAILURE_MESSAGE_MAX_LENGTH:
            return "ChunkResult.failure.message is too long"
        if first_non_printable(text) is not None:
            return "ChunkResult.failure.message has a non-printable character"
        failure = (kind, text, bool(msg.failure.retryable))
    elif outcome == "failed":
        return "ChunkResult is failed without a failure"
    phases: dict[str, float] = {}
    if msg.HasField("phases"):
        for name in ("stage_s", "env_s", "run_s", "upload_s"):
            if msg.phases.HasField(name):
                value = getattr(msg.phases, name)
                if not (math.isfinite(value) and 0 <= value <= _MAX_PHASE_S):
                    return f"ChunkResult.phases.{name} is not a finite duration"
                phases[name] = float(value)
    return ChunkResultIn(
        lane=lane,
        chunk_index=int(msg.chunk_index),
        item_id=int(msg.item_id),
        lease_seq=int(msg.lease_seq),
        outcome=outcome,
        result_digest=digest,
        failure=failure,
        exit_code=msg.exit_code if msg.HasField("exit_code") else None,
        phases=phases,
    )
