"""V0.9 G5c item 0.8 (D252.5): ``owner_preempted`` (FailureType 24) never
reaches a peer that cannot decode it.

* controller -> agent: no field anywhere under ``ControllerMessage`` is a
  ``FailureType`` (so a new controller never sends 24 to an old agent);
* agent -> controller: 24 is reported only for attempts flagged
  ``StartAttempt.benchmark`` - pinned behaviourally in
  ``tests/component/agent/test_v09_g5c_old_peer.py``; here: the flag's
  default is false, so an old controller's StartAttempt is a user attempt.
"""

from __future__ import annotations

import pytest
from google.protobuf.descriptor import Descriptor, FieldDescriptor

from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import failure_type_to_proto

pytestmark = pytest.mark.unit

FAILURE_ENUM = common_pb2.DESCRIPTOR.enum_types_by_name["FailureType"].full_name


def _failure_fields(desc: Descriptor, seen: set[str], path: str) -> list[str]:
    if desc.full_name in seen:
        return []
    seen.add(desc.full_name)
    found: list[str] = []
    for field in desc.fields:
        where = f"{path}.{field.name}"
        if (
            field.type == FieldDescriptor.TYPE_ENUM
            and field.enum_type.full_name == FAILURE_ENUM
        ):
            found.append(where)
        if field.type == FieldDescriptor.TYPE_MESSAGE:
            found += _failure_fields(field.message_type, seen, where)
    return found


def test_owner_preempted_is_number_24() -> None:
    assert failure_type_to_proto(FailureType.OWNER_PREEMPTED) == 24


def test_no_controller_message_carries_a_failure_type() -> None:
    found = _failure_fields(agent_pb2.ControllerMessage.DESCRIPTOR, set(), "msg")
    assert found == []


def test_the_agent_side_does_carry_one() -> None:
    # sanity: the walker finds the AttemptStatus failure (so the test above
    # is not vacuous)
    found = _failure_fields(agent_pb2.AgentMessage.DESCRIPTOR, set(), "msg")
    assert any("failure" in f for f in found), found


def test_an_old_controllers_start_attempt_is_not_a_benchmark() -> None:
    start = agent_pb2.StartAttempt(job_id="j", attempt_id="a", fencing_token=1)
    assert start.benchmark is False
    wire = start.SerializeToString()  # what a pre-G5 controller sends
    assert agent_pb2.StartAttempt.FromString(wire).benchmark is False
