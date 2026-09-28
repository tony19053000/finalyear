"""Bounds on what a peer can make the controller keep (V0.3 G2 review, B3).

Everything an agent sends that the controller stores - in memory (the dedupe
window) or in the registry (``node`` columns) - is bounded *before* it is
accepted, so a hostile or buggy peer cannot grow our memory with a 1 MB
``message_id`` or surface a ``StringDataRightTruncation`` from PostgreSQL as an
``INTERNAL`` error. A Hello over these limits is ``INVALID_ARGUMENT`` and nothing
is persisted; a message with a malformed ``message_id`` is a
:class:`~meshforge.controller.session.errors.SessionProtocolError`.

``message_id`` shape: ``cmd_<ULID>`` (30 characters), the same id the controller
puts on its own envelopes (:func:`meshforge.common.ids.new_command_id`) and the
``(ULID)`` the envelope comment in ``agent.proto`` promises. The prefix is checked
too so a ``node_``/``job_`` id pasted into the wrong field is caught at the edge.

The string caps match the registry columns (``store/models/node.py``) so the
limit and the schema cannot drift apart silently; ``HOSTNAME_MAX_LENGTH`` is the
DNS maximum for a fully qualified name.

**Rule (V0.3 G6, D22): agent-supplied strings are validated at ingestion;
renderers stay defensive anyway.** Every Hello string the controller stores - the
four top-level ones and every string anywhere inside ``Hello.hardware`` - must be
printable (``str.isprintable()``: no C0/C1 control characters, no DEL, no bidi
overrides or other format characters, no line breaks or tabs). Otherwise the
Hello is ``INVALID_ARGUMENT`` and nothing is persisted, exactly like an
over-long field. The reason names the field path and the first offending code
point as ``U+XXXX`` - never the value, which is peer-controlled and may be an
escape sequence aimed at an operator's terminal. Letters of every script stay
legal (Polish, Japanese, ... hostnames). The value is rejected rather than
cleaned so what is stored is always exactly what the agent sent; the CLI's
``_cell()`` gate stays, because it will not stay the only sink (V0.8 dashboard,
log lines).
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Final

from google.protobuf.descriptor import FieldDescriptor
from google.protobuf.message import Message

from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import IdKind, parse_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.caps import MAX_DISKS, MAX_GPUS
from meshforge.controller.scores import GPU_METRICS, metric_name, status_name

__all__ = [
    "AGENT_VERSION_MAX_LENGTH",
    "CAPABILITY_MAX_LENGTH",
    "CPU_MODEL_MAX_LENGTH",
    "GOODBYE_DETAIL_MAX_LENGTH",
    "GPU_COMPUTE_CAPABILITY_MAX_LENGTH",
    "GPU_DRIVER_MAX_LENGTH",
    "GPU_MODEL_MAX_LENGTH",
    "HARDWARE_SCORES_MAX_BYTES",
    "HARDWARE_SUMMARY_MAX_BYTES",
    "HOSTNAME_MAX_LENGTH",
    "MAX_CAPABILITIES",
    "MAX_DISKS",
    "MAX_GPUS",
    "MAX_SCORE_METRICS",
    "MESSAGE_ID_MAX_LENGTH",
    "MOUNT_MAX_LENGTH",
    "OS_MAX_LENGTH",
    "PLATFORM_MAX_LENGTH",
    "SCORE_BACKEND_MAX_LENGTH",
    "SCORE_DETAIL_MAX_LENGTH",
    "SCORE_GPU_NOT_DECLARED",
    "Clamped",
    "check_goodbye",
    "check_hardware",
    "check_hardware_scores",
    "check_hello_strings",
    "check_resource_update",
    "first_non_printable",
    "validate_message_id",
]

HOSTNAME_MAX_LENGTH: Final = 253
"""``Hello.hostname`` - RFC 1035 maximum for a fully qualified name (column: 255)."""

OS_MAX_LENGTH: Final = 128
"""``Hello.os`` - matches ``node.os`` ``String(128)``."""

PLATFORM_MAX_LENGTH: Final = 64
"""``Hello.platform`` - matches ``node.platform`` ``String(64)``."""

AGENT_VERSION_MAX_LENGTH: Final = 64
"""``Hello.agent_version`` - matches ``node_session.agent_version`` ``String(64)``."""

MESSAGE_ID_MAX_LENGTH: Final = 30
"""``len("cmd_") + 26`` - every valid ``message_id`` is exactly this long."""

_HELLO_STRING_LIMITS: Final[tuple[tuple[str, int], ...]] = (
    ("hostname", HOSTNAME_MAX_LENGTH),
    ("os", OS_MAX_LENGTH),
    ("platform", PLATFORM_MAX_LENGTH),
    ("agent_version", AGENT_VERSION_MAX_LENGTH),
)


def first_non_printable(value: str) -> tuple[int, str] | None:
    """``(index, "U+XXXX")`` of the first non-printable character, else ``None``.

    Printable is :meth:`str.isprintable` (Unicode categories other than
    ``Cc``/``Cf``/``Cs``/``Co``/``Cn``/``Zl``/``Zp``/``Zs``, plus the ASCII
    space). Never returns the value itself.
    """
    if value.isprintable():  # C-speed fast path for the common case
        return None
    for index, char in enumerate(value):
        if not char.isprintable():
            return index, f"U+{ord(char):04X}"
    return None  # pragma: no cover - isprintable() said otherwise


def _non_printable_reason(path: str, value: str) -> str | None:
    found = first_non_printable(value)
    if found is None:
        return None
    index, code_point = found
    return f"{path} contains the non-printable character {code_point} at index {index}"


def check_hello_strings(
    *, hostname: str, os: str, platform: str, agent_version: str
) -> str | None:
    """Return a reason if a Hello string field is over its cap or not printable.

    Pure: the caller turns a reason into an ``INVALID_ARGUMENT`` rejection. The
    reason names the field (and both lengths, or the first offending code point,
    D22) but never echoes the value (it may be megabytes long, or an escape
    sequence, and it is peer-controlled). Lengths are checked first for every
    field, so a huge value is refused before it is scanned.
    """
    values = {
        "hostname": hostname,
        "os": os,
        "platform": platform,
        "agent_version": agent_version,
    }
    for field, limit in _HELLO_STRING_LIMITS:
        length = len(values[field])
        if length > limit:
            return f"Hello.{field} is {length} characters; the maximum is {limit}"
    for field, _limit in _HELLO_STRING_LIMITS:
        reason = _non_printable_reason(f"Hello.{field}", values[field])
        if reason is not None:
            return reason
    return None


def validate_message_id(value: str) -> str:
    """Accept exactly a ``cmd_<ULID>`` envelope id; raise :class:`InvalidIdError`.

    The length check runs first so a multi-megabyte string is refused without
    being parsed, and the error message never includes the value.
    """
    if len(value) != MESSAGE_ID_MAX_LENGTH:
        raise InvalidIdError(
            f"message_id must be a cmd_<ULID> ({MESSAGE_ID_MAX_LENGTH} characters), "
            f"got {len(value)} characters"
        )
    try:
        parse_id(value, IdKind.COMMAND)
    except InvalidIdError:
        # Re-raise without the value: parse_id's message repr()s the input.
        raise InvalidIdError("message_id is not a canonical cmd_<ULID>") from None
    return value


GOODBYE_DETAIL_MAX_LENGTH: Final = 256
"""``Goodbye.detail`` (V0.4 D40) - a log-only reason string."""


def check_goodbye(goodbye: agent_pb2.Goodbye) -> str | None:
    """Return a reason if ``Goodbye.detail`` is over its cap or not printable.

    Same rule as the Hello strings (D22): refused, never cleaned, and the reason
    never echoes the value. The caller treats a bad Goodbye as a protocol error -
    the session then ends ``protocol_error`` (abrupt: SUSPECT, OFFLINE at the lease
    edge), which is the safe direction for a peer that broke the contract.
    """
    length = len(goodbye.detail)
    if length > GOODBYE_DETAIL_MAX_LENGTH:
        return (
            f"Goodbye.detail is {length} characters; "
            f"the maximum is {GOODBYE_DETAIL_MAX_LENGTH}"
        )
    return _non_printable_reason("Goodbye.detail", goodbye.detail)


# ---------------------------------------------------------------------------
# Hardware and resource bounds (V0.3 G4, brief §5d D12)
# ---------------------------------------------------------------------------
#
# The agent clips to exactly these numbers before it sends
# (``meshforge.agent.hardware.limits``); the controller re-checks because a peer is
# a peer. The two copies are duplicated on purpose - the controller never imports
# the agent - and ``test_the_agent_and_controller_caps_are_the_same_numbers`` in
# ``tests/unit/controller/test_resource_limits.py`` pins that they agree. The
# policies are asymmetric:
#
# * ``Hello.hardware`` is the enrolment contract, so over-limit is INVALID_ARGUMENT
#   and nothing is persisted (:func:`check_hardware`).
# * ``ResourceUpdate`` is a periodic report, so over-limit is *clamped* and counted
#   once per session (:func:`check_resource_update`). A bad report must never close
#   a live session.
#
# ``MAX_GPUS`` / ``MAX_DISKS`` themselves live in :mod:`meshforge.controller.caps`
# (the downsampler bounds its JSONB keys with the same numbers and must import them
# at module level) and are re-exported here, where the admission checks read them.

CPU_MODEL_MAX_LENGTH: Final = 128
"""``HardwareSummary.cpu_model``."""

GPU_MODEL_MAX_LENGTH: Final = 128
"""``HardwareSummary.Gpu.model``."""

GPU_DRIVER_MAX_LENGTH: Final = 64
"""``HardwareSummary.Gpu.driver_version``."""

GPU_COMPUTE_CAPABILITY_MAX_LENGTH: Final = 16
"""``HardwareSummary.Gpu.compute_capability`` (``"8.6"``)."""

MAX_CAPABILITIES: Final = 32
"""Tags in ``HardwareSummary.capabilities``."""

CAPABILITY_MAX_LENGTH: Final = 64
"""One capability tag."""

MOUNT_MAX_LENGTH: Final = 128
"""``ResourceUpdate.DiskUsage.mount``."""

HARDWARE_SUMMARY_MAX_BYTES: Final = 16 * 1024
"""Backstop on the serialized ``HardwareSummary``: the per-field caps bound it far
below this, so reaching it means a field grew without its own cap. Closes the G2
review follow-up "hardware JSON bounded only by the 4 MiB gRPC limit"."""

PERCENT_MAX: Final = 100.0


def check_hardware(hardware: agent_pb2.HardwareSummary) -> str | None:
    """Return a reason if ``Hello.hardware`` breaks a cap, else ``None``.

    Pure, like :func:`check_hello_strings`: the caller turns a reason into an
    ``INVALID_ARGUMENT`` rejection. Reasons name the field and both sizes and never
    echo a peer-controlled value.
    """
    if len(hardware.cpu_model) > CPU_MODEL_MAX_LENGTH:
        return (
            f"Hello.hardware.cpu_model is {len(hardware.cpu_model)} characters; "
            f"the maximum is {CPU_MODEL_MAX_LENGTH}"
        )
    if len(hardware.gpus) > MAX_GPUS:
        return (
            f"Hello.hardware.gpus has {len(hardware.gpus)} entries; "
            f"the maximum is {MAX_GPUS}"
        )
    for position, gpu in enumerate(hardware.gpus):
        for field, value, limit in (
            ("model", gpu.model, GPU_MODEL_MAX_LENGTH),
            ("driver_version", gpu.driver_version, GPU_DRIVER_MAX_LENGTH),
            (
                "compute_capability",
                gpu.compute_capability,
                GPU_COMPUTE_CAPABILITY_MAX_LENGTH,
            ),
        ):
            if len(value) > limit:
                return (
                    f"Hello.hardware.gpus[{position}].{field} is {len(value)} "
                    f"characters; the maximum is {limit}"
                )
    if len(hardware.capabilities) > MAX_CAPABILITIES:
        return (
            f"Hello.hardware.capabilities has {len(hardware.capabilities)} tags; "
            f"the maximum is {MAX_CAPABILITIES}"
        )
    for position, tag in enumerate(hardware.capabilities):
        if len(tag) > CAPABILITY_MAX_LENGTH:
            return (
                f"Hello.hardware.capabilities[{position}] is {len(tag)} characters; "
                f"the maximum is {CAPABILITY_MAX_LENGTH}"
            )
    size = hardware.ByteSize()
    if size > HARDWARE_SUMMARY_MAX_BYTES:
        return (
            f"Hello.hardware is {size} bytes; the maximum is "
            f"{HARDWARE_SUMMARY_MAX_BYTES}"
        )
    # D22, after the size caps so only bounded strings are scanned.
    return _non_printable_in_message(hardware, "Hello.hardware")


def _non_printable_in_message(message: Message, path: str) -> str | None:
    """The first non-printable string anywhere in ``message`` (D22), as a reason.

    Walks the descriptor, not a hand-written field list, so a string field added
    to ``HardwareSummary`` later is covered without touching this module.
    """
    for field, value in message.ListFields():
        here = f"{path}.{field.name}"
        repeated = field.is_repeated
        items = list(value) if repeated else [value]
        for position, item in enumerate(items):
            where = f"{here}[{position}]" if repeated else here
            if field.type == FieldDescriptor.TYPE_STRING:
                reason = _non_printable_reason(where, item)
            elif field.type == FieldDescriptor.TYPE_MESSAGE:
                reason = _non_printable_in_message(item, where)
            else:
                reason = None
            if reason is not None:
                return reason
    return None


@dataclass(frozen=True, slots=True)
class Clamped:
    """Result of :func:`check_resource_update`: what to record, and what was dropped."""

    update: agent_pb2.ResourceUpdate
    """The original message when nothing was over a cap, else a corrected copy."""
    dropped: tuple[str, ...] = ()
    """Short field names/reasons, for one log line and one counter per session."""

    @property
    def clamped(self) -> bool:
        return bool(self.dropped)


def _out_of_range(value: float) -> bool:
    return not (0.0 <= value <= PERCENT_MAX) or value != value


def _clamp_percent(value: float) -> float:
    if value != value:  # NaN
        return 0.0
    return min(max(value, 0.0), PERCENT_MAX)


def check_resource_update(update: agent_pb2.ResourceUpdate) -> Clamped:
    """Bound a ``ResourceUpdate`` (never reject it). See the D12 note above.

    Percentages are clamped into ``[0, 100]`` (NaN becomes 0), ``gpus``/``disks``
    are truncated to their caps and mount strings are clipped. The returned
    ``dropped`` tuple is what the session manager counts and logs once.
    """
    dropped: list[str] = []
    if _out_of_range(update.cpu_percent):
        dropped.append("cpu_percent out of [0, 100]")
    if len(update.gpus) > MAX_GPUS:
        dropped.append(f"gpus: {len(update.gpus)} entries, cap {MAX_GPUS}")
    if len(update.disks) > MAX_DISKS:
        dropped.append(f"disks: {len(update.disks)} entries, cap {MAX_DISKS}")
    for gpu in update.gpus[:MAX_GPUS]:
        if _out_of_range(gpu.util_percent):
            dropped.append("gpus[].util_percent out of [0, 100]")
            break
    for disk in update.disks[:MAX_DISKS]:
        if len(disk.mount) > MOUNT_MAX_LENGTH:
            dropped.append(f"disks[].mount over {MOUNT_MAX_LENGTH} characters")
            break
    if not dropped:
        return Clamped(update)

    fixed = agent_pb2.ResourceUpdate(
        cpu_percent=_clamp_percent(update.cpu_percent),
        ram_used_bytes=update.ram_used_bytes,
        ram_total_bytes=update.ram_total_bytes,
        sequence=update.sequence,
    )
    fixed.observed_at.CopyFrom(update.observed_at)
    for gpu in update.gpus[:MAX_GPUS]:
        fixed.gpus.append(
            agent_pb2.ResourceUpdate.GpuUsage(
                index=gpu.index,
                util_percent=_clamp_percent(gpu.util_percent),
                vram_used_bytes=gpu.vram_used_bytes,
                vram_total_bytes=gpu.vram_total_bytes,
            )
        )
    for disk in update.disks[:MAX_DISKS]:
        fixed.disks.append(
            agent_pb2.ResourceUpdate.DiskUsage(
                mount=disk.mount[:MOUNT_MAX_LENGTH],
                free_bytes=disk.free_bytes,
                total_bytes=disk.total_bytes,
            )
        )
    return Clamped(fixed, tuple(dropped))


# ---------------------------------------------------------------------------
# Hardware scores (V0.3 G7, brief §5g D38)
# ---------------------------------------------------------------------------
#
# Policy: a score set that breaks any rule below is **dropped** - logged
# ``session.hardware_scores_rejected`` with the reason and counted - and the
# stream stays up. Scores are an optional feature of a node, so a malformed one
# must not take the node offline (unlike ``Hello.hardware``, the enrolment
# contract). A score is a feature the planner will train on (V0.15): a wrong
# number stored quietly is worse than none, so the rules are strict - finite
# values only, "unknown" only as *absent* (never a 0.0), known enum values only,
# bounded counts and printable bounded strings (D22). The agent's own model
# (``agent/benchmarks/model.py``) enforces the same shape before it sends; this is
# the controller's copy, because a peer is a peer.

MAX_SCORE_METRICS: Final = 4 + 3 * MAX_GPUS
"""Four host metrics plus three per GPU (the agent's ``_MAX_METRICS``)."""

HARDWARE_SCORES_MAX_BYTES: Final = 64 * 1024
"""Backstop on the serialized message; the per-field caps bound it far below."""

SCORE_BACKEND_MAX_LENGTH: Final = 64
"""``Score.backend`` / ``Score.backend_version`` (the agent model's cap)."""

SCORE_DETAIL_MAX_LENGTH: Final = 300
"""``Score.detail`` (the agent model's cap)."""

SCORE_MIN_REPS: Final = 5
SCORE_MAX_REPS: Final = 10_000
SCORE_MAX_THREADS: Final = 4096
SCORE_MAX_GPU_INDEX: Final = 1023
"""NVML indices are small; this only bounds a hostile value."""

SCORE_GPU_NOT_DECLARED: Final = "gpu_index_not_declared"
"""Reason code (D39): a GPU metric names a GPU the session's Hello did not declare."""

_SCORE_RUN_ID_LENGTH: Final = len("score_") + 26
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_REASON = re.compile(r"[a-z0-9_]{1,64}")


def _finite(value: float) -> bool:
    return math.isfinite(value)


def _check_score(position: int, score: agent_pb2.Score) -> str | None:
    where = f"HardwareScores.scores[{position}]"
    name = metric_name(score.metric)
    if name is None:
        return f"{where}.metric is unspecified or unknown ({int(score.metric)})"
    status = status_name(score.status)
    if status is None:
        return f"{where}.status is unspecified or unknown ({int(score.status)})"
    is_gpu = name in GPU_METRICS
    if score.HasField("gpu_index") != is_gpu:
        return f"{where}.gpu_index must be set for GPU metrics only ({name})"
    if is_gpu and score.gpu_index > SCORE_MAX_GPU_INDEX:
        return f"{where}.gpu_index {score.gpu_index} exceeds {SCORE_MAX_GPU_INDEX}"
    if score.n > SCORE_MAX_REPS:
        return f"{where}.n {score.n} exceeds {SCORE_MAX_REPS}"
    if score.HasField("threads_effective") and not (
        1 <= score.threads_effective <= SCORE_MAX_THREADS
    ):
        return f"{where}.threads_effective is outside [1, {SCORE_MAX_THREADS}]"
    for field in ("backend", "backend_version"):
        value = getattr(score, field)
        if len(value) > SCORE_BACKEND_MAX_LENGTH:
            return (
                f"{where}.{field} is {len(value)} characters; "
                f"the maximum is {SCORE_BACKEND_MAX_LENGTH}"
            )
    if len(score.detail) > SCORE_DETAIL_MAX_LENGTH:
        return (
            f"{where}.detail is {len(score.detail)} characters; "
            f"the maximum is {SCORE_DETAIL_MAX_LENGTH}"
        )
    if status == "ok":
        if not score.HasField("mean") or not (_finite(score.mean) and score.mean > 0):
            return f"{where}.mean must be a finite number > 0 for an ok metric"
        if not score.HasField("stdev") or not (
            _finite(score.stdev) and score.stdev >= 0
        ):
            return f"{where}.stdev must be a finite number >= 0 for an ok metric"
        if score.n < SCORE_MIN_REPS:
            return f"{where}.n must be >= {SCORE_MIN_REPS} for an ok metric"
        if score.reason:
            return f"{where}.reason must be empty for an ok metric"
    else:
        if score.HasField("mean") or score.HasField("stdev"):
            # "Unknown" is absence, never a number standing in for it (G4 rule).
            return f"{where} is {status} but carries a mean/stdev"
        if not _REASON.fullmatch(score.reason):
            return f"{where}.reason must be a snake_case code for a {status} metric"
    return None


def _check_conditions(scores: agent_pb2.HardwareScores) -> str | None:
    if not scores.HasField("conditions"):
        return "HardwareScores.conditions is missing"
    c = scores.conditions
    if not (_finite(c.budget_s) and c.budget_s > 0):
        return "HardwareScores.conditions.budget_s must be a finite number > 0"
    if not (_finite(c.duration_s) and c.duration_s >= 0):
        return "HardwareScores.conditions.duration_s must be a finite number >= 0"
    if c.HasField("cpu_load_percent_before") and not (
        _finite(c.cpu_load_percent_before)
        and 0.0 <= c.cpu_load_percent_before <= PERCENT_MAX
    ):
        return "HardwareScores.conditions.cpu_load_percent_before is outside [0, 100]"
    return None


def check_hardware_scores(
    scores: agent_pb2.HardwareScores, declared_gpus: Collection[int]
) -> str | None:
    """Return a reason if a ``HardwareScores`` breaks a D38/D39 rule, else ``None``.

    ``declared_gpus`` is the set of ``Hello.hardware.gpus[].index`` of the **same
    session** (for a Hello, that Hello's own hardware). A GPU metric whose
    ``gpu_index`` is not in it rejects the whole set with a reason starting
    ``gpu_index_not_declared`` (D39): a node with no declared GPU may send no GPU
    metric at all - otherwise REST would serve a TFLOP/s number for a GPU the
    node itself says it does not have. Host metrics need no GPU.

    Pure, like :func:`check_hardware`; the caller drops the set and logs the
    reason (never a stream kill, see the section note). Sizes and counts are
    checked before any string is scanned; reasons never echo a peer value.
    """
    size = scores.ByteSize()
    if size > HARDWARE_SCORES_MAX_BYTES:
        return (
            f"HardwareScores is {size} bytes; the maximum is "
            f"{HARDWARE_SCORES_MAX_BYTES}"
        )
    if len(scores.scores) > MAX_SCORE_METRICS:
        return (
            f"HardwareScores.scores has {len(scores.scores)} entries; "
            f"the maximum is {MAX_SCORE_METRICS}"
        )
    if len(scores.run_id) != _SCORE_RUN_ID_LENGTH:
        return "HardwareScores.run_id is not a score_<ULID>"
    try:
        parse_id(scores.run_id, IdKind.SCORE_RUN)
    except InvalidIdError:
        return "HardwareScores.run_id is not a score_<ULID>"
    if not _FINGERPRINT.fullmatch(scores.fingerprint):
        return "HardwareScores.fingerprint is not 64 lowercase hex characters"
    if scores.score_version < 1:
        return "HardwareScores.score_version must be >= 1"
    if not scores.HasField("measured_at"):
        return "HardwareScores.measured_at is missing"
    try:
        scores.measured_at.ToDatetime()
    except (ValueError, OverflowError, OSError):
        return "HardwareScores.measured_at is not a valid timestamp"
    reason = _check_conditions(scores)
    if reason is not None:
        return reason
    seen: set[tuple[str | None, int | None]] = set()
    gpu_indices: set[int] = set()
    for position, score in enumerate(scores.scores):
        reason = _check_score(position, score)
        if reason is not None:
            return reason
        index = score.gpu_index if score.HasField("gpu_index") else None
        key = (metric_name(score.metric), index)
        if key in seen:
            return f"HardwareScores.scores[{position}] repeats a metric"
        seen.add(key)
        if index is not None:
            if index not in declared_gpus:
                return (
                    f"{SCORE_GPU_NOT_DECLARED}: HardwareScores.scores[{position}]"
                    f".gpu_index {index} is not in the session's Hello.hardware.gpus"
                )
            gpu_indices.add(index)
    if len(gpu_indices) > MAX_GPUS:
        return (
            f"HardwareScores covers {len(gpu_indices)} GPUs; the maximum is {MAX_GPUS}"
        )
    # D22, after every size cap so only bounded strings are scanned.
    return _non_printable_in_message(scores, "HardwareScores")
