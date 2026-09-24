"""Typed, prefixed, sortable identifiers for fabric entities.

Format: ``<prefix>_<ULID>``, e.g. ``job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3``.

Design choices:

* **ULID** (48-bit ms timestamp + 80 random bits, Crockford base32, 26 chars): IDs
  sort by creation time as plain strings, which keeps DB indexes append-mostly and
  makes logs readable. No coordination between processes is needed.
* **Prefix** makes an ID self-describing in logs and APIs and lets us reject a
  ``node_...`` where a ``job_...`` is expected. The ULID part is kept in canonical
  upper-case form so string equality is identity equality.
* **Monotonic within a process:** IDs from one process are strictly increasing, even
  within the same millisecond and even if the wall clock steps backwards (the
  generator never lets the timestamp go below the last one it issued). Across
  processes/machines, order is only as good as the clocks are.
* **Fork-safe:** a forked child resets the generator state (and its lock), so it
  does not continue the parent's same-millisecond counter and duplicate the IDs
  the parent issues next.
* **Why a custom generator** instead of ``ulid.ULID()``: python-ulid draws fresh
  randomness for every ID, so two IDs in the same millisecond sort randomly. We
  need strict in-process monotonicity (event/attempt ordering in logs and DB), so
  :class:`_MonotonicUlidGenerator` builds the bytes itself; python-ulid is used
  only for encoding/parsing.
* **IDs are not secrets.** They are timestamp-prefixed and, within a millisecond,
  sequential, so they are guessable. Never use an ID as an access token or
  capability; authorization is checked separately.
* **NewType** aliases (``JobId`` etc.) give static type safety at zero runtime cost.
* Content-addressed blobs are *not* ULIDs; they are named by SHA-256 (see
  :mod:`meshforge.common.hashing`).

New entity kinds are added by extending :class:`IdKind`; prefixes must stay stable
once data has been persisted.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from enum import StrEnum
from typing import NewType

from ulid import ULID

from meshforge.common.errors import InvalidIdError

__all__ = [
    "AttemptId",
    "ChunkId",
    "CommandId",
    "IdKind",
    "JobId",
    "NodeId",
    "ScoreRunId",
    "SessionId",
    "id_kind",
    "id_timestamp",
    "new_attempt_id",
    "new_chunk_id",
    "new_command_id",
    "new_id",
    "new_job_id",
    "new_node_id",
    "new_score_run_id",
    "new_session_id",
    "parse_id",
    "validate_attempt_id",
    "validate_chunk_id",
    "validate_command_id",
    "validate_job_id",
    "validate_node_id",
    "validate_score_run_id",
    "validate_session_id",
]


class IdKind(StrEnum):
    """Entity kinds; the value is the ID prefix (without the underscore)."""

    JOB = "job"
    ATTEMPT = "att"
    NODE = "node"
    SESSION = "sess"
    CHUNK = "chunk"
    COMMAND = "cmd"
    SCORE_RUN = "score"
    """One hardware-score run of an agent (roadmap 0.3.4, brief §5g)."""


JobId = NewType("JobId", str)
AttemptId = NewType("AttemptId", str)
NodeId = NewType("NodeId", str)
SessionId = NewType("SessionId", str)
ChunkId = NewType("ChunkId", str)
CommandId = NewType("CommandId", str)
ScoreRunId = NewType("ScoreRunId", str)

_SEPARATOR = "_"
_ULID_LEN = 26
_RANDOMNESS_BYTES = 10
_MAX_RANDOMNESS = (1 << 80) - 1


class _MonotonicUlidGenerator:
    """Thread-safe ULID generator that is strictly increasing within the process.

    Same millisecond → previous randomness + 1. Randomness overflow (practically
    impossible: 2^80 IDs/ms) or a clock stepping backwards → the timestamp is held at
    / advanced past the last issued one, so ordering is never violated.
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        """(Re)initialise state; also called in a child process after ``fork()``.

        The child gets a fresh lock (the parent's may have been held by another
        thread at fork time) and forgets the parent's last timestamp/randomness so
        its next ID draws new randomness instead of ``parent_last + 1``.
        """
        self._lock = threading.Lock()
        self._last_ms = -1
        self._last_rand = 0

    def generate(self) -> ULID:
        now_ms = time.time_ns() // 1_000_000
        with self._lock:
            if now_ms > self._last_ms:
                ms = now_ms
                rand = int.from_bytes(os.urandom(_RANDOMNESS_BYTES), "big")
            elif self._last_rand < _MAX_RANDOMNESS:
                ms = self._last_ms
                rand = self._last_rand + 1
            else:
                ms = self._last_ms + 1
                rand = int.from_bytes(os.urandom(_RANDOMNESS_BYTES), "big")
            self._last_ms, self._last_rand = ms, rand
        return ULID.from_bytes(
            ms.to_bytes(6, "big") + rand.to_bytes(_RANDOMNESS_BYTES, "big")
        )


_generator = _MonotonicUlidGenerator()
if hasattr(os, "register_at_fork"):  # POSIX only; Windows has no fork()
    os.register_at_fork(after_in_child=_generator._reset)


def new_id(kind: IdKind) -> str:
    """Return a new ID string of the given kind, e.g. ``att_01J...``."""
    return f"{kind.value}{_SEPARATOR}{_generator.generate()}"


def new_job_id() -> JobId:
    return JobId(new_id(IdKind.JOB))


def new_attempt_id() -> AttemptId:
    return AttemptId(new_id(IdKind.ATTEMPT))


def new_node_id() -> NodeId:
    return NodeId(new_id(IdKind.NODE))


def new_session_id() -> SessionId:
    return SessionId(new_id(IdKind.SESSION))


def new_chunk_id() -> ChunkId:
    return ChunkId(new_id(IdKind.CHUNK))


def new_command_id() -> CommandId:
    return CommandId(new_id(IdKind.COMMAND))


def new_score_run_id() -> ScoreRunId:
    return ScoreRunId(new_id(IdKind.SCORE_RUN))


def parse_id(value: object, kind: IdKind | None = None) -> tuple[IdKind, ULID]:
    """Split and validate an ID. If ``kind`` is given the prefix must match it.

    Raises:
        InvalidIdError: wrong type, unknown/mismatched prefix, or malformed /
            non-canonical ULID part.
    """
    if not isinstance(value, str):
        raise InvalidIdError(f"ID must be a string, got {type(value).__name__}")
    prefix, sep, body = value.partition(_SEPARATOR)
    if not sep:
        raise InvalidIdError(f"ID {value!r} has no '<prefix>_' part")
    try:
        actual_kind = IdKind(prefix)
    except ValueError:
        raise InvalidIdError(f"ID {value!r} has unknown prefix {prefix!r}") from None
    if kind is not None and actual_kind is not kind:
        raise InvalidIdError(f"expected a {kind.value!r} ID, got {value!r}")
    if len(body) != _ULID_LEN:
        raise InvalidIdError(
            f"ID {value!r} has a malformed ULID part (length {len(body)})"
        )
    try:
        ulid = ULID.from_str(body)
    except ValueError as exc:
        raise InvalidIdError(f"ID {value!r} has a malformed ULID part: {exc}") from None
    if str(ulid) != body:
        raise InvalidIdError(f"ID {value!r} is not in canonical (upper-case) form")
    return actual_kind, ulid


def id_kind(value: str) -> IdKind:
    """Return the kind of a valid ID (raises :class:`InvalidIdError` otherwise)."""
    return parse_id(value)[0]


def id_timestamp(value: str) -> datetime:
    """Creation time (UTC, millisecond precision) embedded in an ID."""
    return parse_id(value)[1].datetime


def validate_job_id(value: object) -> JobId:
    parse_id(value, IdKind.JOB)
    return JobId(str(value))


def validate_attempt_id(value: object) -> AttemptId:
    parse_id(value, IdKind.ATTEMPT)
    return AttemptId(str(value))


def validate_node_id(value: object) -> NodeId:
    parse_id(value, IdKind.NODE)
    return NodeId(str(value))


def validate_session_id(value: object) -> SessionId:
    parse_id(value, IdKind.SESSION)
    return SessionId(str(value))


def validate_chunk_id(value: object) -> ChunkId:
    parse_id(value, IdKind.CHUNK)
    return ChunkId(str(value))


def validate_command_id(value: object) -> CommandId:
    parse_id(value, IdKind.COMMAND)
    return CommandId(str(value))


def validate_score_run_id(value: object) -> ScoreRunId:
    parse_id(value, IdKind.SCORE_RUN)
    return ScoreRunId(str(value))
