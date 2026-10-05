"""V0.6 G1: the field / payload numbers this group took are pinned (additive
only; renumbering would silently break agents of another version)."""

from __future__ import annotations

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


def test_attempt_status_env_hash_is_field_16() -> None:
    field = agent_pb2.AttemptStatus.DESCRIPTOR.fields_by_name["env_hash"]
    assert field.number == 16
    assert field.type == field.TYPE_STRING
    wire = agent_pb2.AttemptStatus(env_hash="ab" * 32).SerializeToString()
    assert agent_pb2.AttemptStatus.FromString(wire).env_hash == "ab" * 32
