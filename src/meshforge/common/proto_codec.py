"""Conversions between Python domain enums and their protobuf wire enums.

This is the **only** place enum mapping happens (task 0.1.5, reviewer B1). The
mapping is by *name*: Python ``FailureType.<NAME>`` <-> proto
``FAILURE_TYPE_<NAME>``. Wire numbers are never compared with Python values, so
the two sides can be ordered independently; ``tests/unit/test_proto_enum_parity.py``
guarantees every member exists on both sides.

Decoding is strict: ``*_UNSPECIFIED`` (proto3's "not set") and numbers unknown to
this build (a newer peer) raise :class:`ProtoDecodeError` instead of guessing.
"""

from __future__ import annotations

from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.proto.meshforge.v1 import common_pb2
from meshforge.common.states import AttemptState

__all__ = [
    "ProtoDecodeError",
    "attempt_state_from_proto",
    "attempt_state_to_proto",
    "failure_type_from_proto",
    "failure_type_to_proto",
]

_FAILURE_TYPE_PREFIX: Final = "FAILURE_TYPE_"
_ATTEMPT_STATE_PREFIX: Final = "ATTEMPT_STATE_"

type ProtoAttemptState = common_pb2.AttemptState.ValueType

type ProtoFailureType = common_pb2.FailureType.ValueType


class ProtoDecodeError(MeshForgeError, ValueError):
    """A protobuf value cannot be converted to its Python domain type."""

    default_failure_type = FailureType.INVALID_INPUT


def failure_type_to_proto(failure_type: FailureType) -> ProtoFailureType:
    """``FailureType.OOM`` -> ``common_pb2.FAILURE_TYPE_OOM``."""
    try:
        return common_pb2.FailureType.Value(_FAILURE_TYPE_PREFIX + failure_type.name)
    except ValueError as exc:
        # Unreachable in a consistent build: the parity test guarantees 1:1 names.
        raise MeshForgeError(
            f"FailureType.{failure_type.name} has no proto counterpart",
            details={"failure_type": failure_type.value},
        ) from exc


def failure_type_from_proto(value: int) -> FailureType:
    """``common_pb2.FAILURE_TYPE_OOM`` -> ``FailureType.OOM``.

    Raises :class:`ProtoDecodeError` for ``FAILURE_TYPE_UNSPECIFIED`` and for
    numbers not defined in this build's proto.
    """
    try:
        proto_name = common_pb2.FailureType.Name(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ProtoDecodeError(
            f"unknown FailureType wire value {value}", details={"value": value}
        ) from exc
    name = proto_name.removeprefix(_FAILURE_TYPE_PREFIX)
    if name == "UNSPECIFIED":
        raise ProtoDecodeError(
            "FailureType is FAILURE_TYPE_UNSPECIFIED (not set)",
            details={"value": value},
        )
    try:
        return FailureType[name]
    except KeyError as exc:
        raise ProtoDecodeError(
            f"proto {proto_name} has no Python FailureType counterpart",
            details={"value": value},
        ) from exc


def attempt_state_to_proto(state: AttemptState) -> ProtoAttemptState:
    """``AttemptState.RUNNING`` -> ``common_pb2.ATTEMPT_STATE_RUNNING``."""
    try:
        return common_pb2.AttemptState.Value(_ATTEMPT_STATE_PREFIX + state.name)
    except ValueError as exc:  # unreachable in a consistent build (parity test)
        raise MeshForgeError(
            f"AttemptState.{state.name} has no proto counterpart",
            details={"attempt_state": state.value},
        ) from exc


def attempt_state_from_proto(value: int) -> AttemptState:
    """Strict: ``ATTEMPT_STATE_UNSPECIFIED`` and unknown numbers raise."""
    try:
        proto_name = common_pb2.AttemptState.Name(value)  # type: ignore[arg-type]
    except ValueError as exc:
        raise ProtoDecodeError(
            f"unknown AttemptState wire value {value}", details={"value": value}
        ) from exc
    name = proto_name.removeprefix(_ATTEMPT_STATE_PREFIX)
    if name == "UNSPECIFIED":
        raise ProtoDecodeError(
            "AttemptState is ATTEMPT_STATE_UNSPECIFIED (not set)",
            details={"value": value},
        )
    try:
        return AttemptState[name]
    except KeyError as exc:
        raise ProtoDecodeError(
            f"proto {proto_name} has no Python AttemptState counterpart",
            details={"value": value},
        ) from exc
