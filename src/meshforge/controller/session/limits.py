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

from typing import Final

from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import IdKind, parse_id

__all__ = [
    "AGENT_VERSION_MAX_LENGTH",
    "HOSTNAME_MAX_LENGTH",
    "MESSAGE_ID_MAX_LENGTH",
    "OS_MAX_LENGTH",
    "PLATFORM_MAX_LENGTH",
    "check_hello_strings",
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
