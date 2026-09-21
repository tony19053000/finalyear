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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import IdKind, parse_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = [
    "AGENT_VERSION_MAX_LENGTH",
    "CAPABILITY_MAX_LENGTH",
    "CPU_MODEL_MAX_LENGTH",
    "GPU_COMPUTE_CAPABILITY_MAX_LENGTH",
    "GPU_DRIVER_MAX_LENGTH",
    "GPU_MODEL_MAX_LENGTH",
    "HARDWARE_SUMMARY_MAX_BYTES",
    "HOSTNAME_MAX_LENGTH",
    "MAX_CAPABILITIES",
    "MAX_DISKS",
    "MAX_GPUS",
    "MESSAGE_ID_MAX_LENGTH",
    "MOUNT_MAX_LENGTH",
    "OS_MAX_LENGTH",
    "PLATFORM_MAX_LENGTH",
    "Clamped",
    "check_hardware",
    "check_hello_strings",
    "check_resource_update",
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


def check_hello_strings(
    *, hostname: str, os: str, platform: str, agent_version: str
) -> str | None:
    """Return a reason if a Hello string field is over its cap, else ``None``.

    Pure: the caller turns a reason into an ``INVALID_ARGUMENT`` rejection. The
    reason names the field and both lengths but never echoes the value (it may
    be megabytes long, and it is peer-controlled).
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

CPU_MODEL_MAX_LENGTH: Final = 128
"""``HardwareSummary.cpu_model``."""

GPU_MODEL_MAX_LENGTH: Final = 128
"""``HardwareSummary.Gpu.model``."""

GPU_DRIVER_MAX_LENGTH: Final = 64
"""``HardwareSummary.Gpu.driver_version``."""

GPU_COMPUTE_CAPABILITY_MAX_LENGTH: Final = 16
"""``HardwareSummary.Gpu.compute_capability`` (``"8.6"``)."""

MAX_GPUS: Final = 16
"""Devices per node, in ``HardwareSummary.gpus`` and ``ResourceUpdate.gpus``."""

MAX_CAPABILITIES: Final = 32
"""Tags in ``HardwareSummary.capabilities``."""

CAPABILITY_MAX_LENGTH: Final = 64
"""One capability tag."""

MAX_DISKS: Final = 16
"""Entries in ``ResourceUpdate.disks``."""

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
