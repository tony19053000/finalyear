"""Task 0.1.5 (reviewer B1): Python domain enums mirror their proto enums 1:1.

Mapping is by name (``FailureType.<NAME>`` <-> ``FAILURE_TYPE_<NAME>``) and lives
only in :mod:`meshforge.common.proto_codec`.
"""

from __future__ import annotations

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import common_pb2
from meshforge.common.proto_codec import (
    ProtoDecodeError,
    attempt_state_from_proto,
    attempt_state_to_proto,
    failure_type_from_proto,
    failure_type_to_proto,
)
from meshforge.common.states import AttemptState

pytestmark = pytest.mark.unit

_PREFIX = "FAILURE_TYPE_"


def _proto_failure_names() -> set[str]:
    return {
        v.name.removeprefix(_PREFIX)
        for v in common_pb2.FailureType.DESCRIPTOR.values
        if v.number != 0
    }


def test_every_python_failure_type_exists_in_proto() -> None:
    missing = {m.name for m in FailureType} - _proto_failure_names()
    assert not missing, (
        f"add FAILURE_TYPE_<NAME> to common.proto for: {sorted(missing)}"
    )


def test_every_proto_failure_type_exists_in_python() -> None:
    missing = _proto_failure_names() - {m.name for m in FailureType}
    assert not missing, f"add to meshforge.common.errors.FailureType: {sorted(missing)}"


def test_proto_zero_value_is_unspecified() -> None:
    assert common_pb2.FailureType.Name(0) == "FAILURE_TYPE_UNSPECIFIED"  # type: ignore[arg-type]


@pytest.mark.parametrize("member", list(FailureType), ids=lambda m: m.name)
def test_failure_type_round_trip(member: FailureType) -> None:
    wire = failure_type_to_proto(member)
    assert wire != 0
    assert common_pb2.FailureType.Name(wire) == _PREFIX + member.name
    assert failure_type_from_proto(wire) is member


def test_from_proto_rejects_unspecified() -> None:
    with pytest.raises(ProtoDecodeError, match="UNSPECIFIED"):
        failure_type_from_proto(common_pb2.FAILURE_TYPE_UNSPECIFIED)


@pytest.mark.parametrize("value", [9999, -1])
def test_from_proto_rejects_unknown_numbers(value: int) -> None:
    with pytest.raises(ProtoDecodeError, match="unknown") as info:
        failure_type_from_proto(value)
    assert info.value.failure_type is FailureType.INVALID_INPUT
    assert isinstance(info.value, ValueError)


def test_decoded_wire_message_maps_back() -> None:
    failure = common_pb2.Failure(
        failure_type=failure_type_to_proto(FailureType.VRAM_OOM)
    )
    parsed = common_pb2.Failure.FromString(failure.SerializeToString())
    assert failure_type_from_proto(parsed.failure_type) is FailureType.VRAM_OOM


_STATE_PREFIX = "ATTEMPT_STATE_"


def _proto_state_names() -> set[str]:
    return {
        v.name.removeprefix(_STATE_PREFIX)
        for v in common_pb2.AttemptState.DESCRIPTOR.values
        if v.number != 0
    }


def test_attempt_state_parity_both_directions() -> None:
    python = {m.name for m in AttemptState}
    assert python == _proto_state_names()
    assert common_pb2.AttemptState.Name(0) == "ATTEMPT_STATE_UNSPECIFIED"  # type: ignore[arg-type]


@pytest.mark.parametrize("member", list(AttemptState), ids=lambda m: m.name)
def test_attempt_state_round_trip(member: AttemptState) -> None:
    wire = attempt_state_to_proto(member)
    assert wire != 0
    assert attempt_state_from_proto(wire) is member


@pytest.mark.parametrize("value", [0, 9999, -1])
def test_attempt_state_from_proto_is_strict(value: int) -> None:
    with pytest.raises(ProtoDecodeError):
        attempt_state_from_proto(value)
