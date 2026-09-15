"""Tester (task 0.1.5): codec coverage complementing
``tests/unit/test_proto_enum_parity.py`` -- specifically, that a real
``MeshForgeError`` (the actual exception type raised throughout the codebase,
not a bare ``FailureType`` member) maps onto a wire ``common_pb2.Failure`` and
back without losing information, for every failure type and for both the
default and an overridden ``retryable``.

Also locks down the exact cardinality of the parity set (22 non-zero
``FailureType`` values per the 0.1.5 handoff) so a silent addition/removal on
either side is caught even if the name-set comparison in
``test_proto_enum_parity.py`` were ever weakened.
"""

from __future__ import annotations

import pytest

from meshforge.common.errors import DEFAULT_RETRYABLE, FailureType, MeshForgeError
from meshforge.common.proto.meshforge.v1 import common_pb2
from meshforge.common.proto_codec import failure_type_from_proto, failure_type_to_proto

pytestmark = pytest.mark.unit


def _failure_from_error(error: MeshForgeError) -> common_pb2.Failure:
    """What controller/agent code will do wherever it wraps a MeshForgeError
    for the wire (no such helper exists yet in proto_codec -- V0.1 is a
    skeleton -- so this is the reference construction the tester expects any
    future ``failure_to_proto`` helper to match)."""
    return common_pb2.Failure(
        failure_type=failure_type_to_proto(error.failure_type),
        message=error.message,
        retryable=error.retryable,
    )


def test_failure_type_cardinality_is_22_non_unspecified_values() -> None:
    non_zero = [v for v in common_pb2.FailureType.DESCRIPTOR.values if v.number != 0]
    assert len(non_zero) == 22
    assert len(FailureType) == 22


@pytest.mark.parametrize("member", list(FailureType), ids=lambda m: m.name)
def test_meshforge_error_default_retryable_round_trips_through_wire(
    member: FailureType,
) -> None:
    error = MeshForgeError(f"boom: {member.value}", failure_type=member)
    assert error.retryable == DEFAULT_RETRYABLE[member]

    wire = _failure_from_error(error)
    parsed = common_pb2.Failure.FromString(wire.SerializeToString())

    assert failure_type_from_proto(parsed.failure_type) is member
    assert parsed.message == error.message
    assert parsed.retryable == error.retryable


@pytest.mark.parametrize("override", [True, False])
def test_meshforge_error_explicit_retryable_overrides_default(override: bool) -> None:
    # Pick a type whose default is the opposite of the override so the test
    # actually exercises the override path either way.
    member = next(m for m, default in DEFAULT_RETRYABLE.items() if default != override)
    error = MeshForgeError("x", failure_type=member, retryable=override)
    assert error.retryable is override

    wire = _failure_from_error(error)
    parsed = common_pb2.Failure.FromString(wire.SerializeToString())
    assert parsed.retryable is override


def test_meshforge_error_message_with_unicode_and_newlines_survives_wire() -> None:
    message = "path C:\\Users\\ünïcödé\\job\u3000\n\ttrailing\x00null?"
    error = MeshForgeError(message, failure_type=FailureType.ENV_ERROR)
    wire = _failure_from_error(error)
    parsed = common_pb2.Failure.FromString(wire.SerializeToString())
    assert parsed.message == message


def test_meshforge_error_default_failure_type_is_internal_error() -> None:
    error = MeshForgeError("unclassified")
    assert error.failure_type is FailureType.INTERNAL_ERROR
    wire = _failure_from_error(error)
    assert wire.failure_type == common_pb2.FAILURE_TYPE_INTERNAL_ERROR


def test_subclass_default_failure_type_maps_correctly() -> None:
    from meshforge.common.errors import ConfigError

    error = ConfigError("bad config")
    assert error.failure_type is FailureType.CONFIG_ERROR
    wire = _failure_from_error(error)
    parsed = common_pb2.Failure.FromString(wire.SerializeToString())
    assert failure_type_from_proto(parsed.failure_type) is FailureType.CONFIG_ERROR
    assert parsed.retryable is False
