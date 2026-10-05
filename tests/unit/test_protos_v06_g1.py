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


def test_refresh_upload_grant_is_controller_payload_19() -> None:
    """D114: the next free ControllerMessage payload number after
    refresh_fetch_token (18)."""
    field = agent_pb2.ControllerMessage.DESCRIPTOR.fields_by_name[
        "refresh_upload_grant"
    ]
    assert field.number == 19
    assert field.containing_oneof is not None
    assert field.containing_oneof.name == "payload"
    numbers = {f.name: f.number for f in agent_pb2.RefreshUploadGrant.DESCRIPTOR.fields}
    assert numbers == {
        "attempt_id": 1,
        "fencing_token": 2,
        "upload_token": 3,
        "blob_url": 4,
        "blob_port": 5,
    }
    msg = agent_pb2.ControllerMessage(
        message_id="m-1",
        refresh_upload_grant=agent_pb2.RefreshUploadGrant(
            attempt_id="att_1", fencing_token=3, upload_token="t", blob_port=8471
        ),
    )
    parsed = agent_pb2.ControllerMessage.FromString(msg.SerializeToString())
    assert parsed.WhichOneof("payload") == "refresh_upload_grant"
    assert parsed.refresh_upload_grant.fencing_token == 3
