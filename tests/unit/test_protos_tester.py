"""Independent tester round for roadmap 0.3.1 (agent.proto V0.3 contract).

Spot-checks the implementer's own claims (session_id = field 3 on both
envelopes, HelloAck reachable, wire round-trip, no V0.1 field renumbered)
directly against the compiled descriptors, without reading
``tests/unit/test_protos.py`` first. Never edits ``proto/`` or generated code.
"""

from __future__ import annotations

import pytest

from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2

pytestmark = pytest.mark.unit


def test_hello_ack_is_reachable_as_the_first_controller_message() -> None:
    ack = agent_pb2.ControllerMessage(
        message_id="01J00000000000000000000000",
        session_id="",  # empty is explicitly allowed only on HelloAck
        hello_ack=agent_pb2.HelloAck(
            accepted=True,
            session_id="sess_01J00000000000000000000000",
            heartbeat_interval_ms=2000,
            lease_ms=6000,
        ),
    )
    wire = ack.SerializeToString()
    parsed = agent_pb2.ControllerMessage.FromString(wire)
    assert parsed.WhichOneof("payload") == "hello_ack"
    assert parsed.hello_ack.accepted is True
    assert parsed.hello_ack.session_id == "sess_01J00000000000000000000000"


def test_hello_ack_reject_round_trip_carries_reject_code_and_reason() -> None:
    ack = agent_pb2.HelloAck(
        accepted=False,
        reject_code=agent_pb2.HelloAck.REJECT_CODE_STALE_EPOCH,
        reject_reason="epoch 3 <= live session epoch 5",
    )
    parsed = agent_pb2.HelloAck.FromString(ack.SerializeToString())
    assert parsed.accepted is False
    assert parsed.reject_code == agent_pb2.HelloAck.REJECT_CODE_STALE_EPOCH
    assert parsed.session_id == ""  # unset on rejection


def test_v01_field_numbers_on_hello_are_unchanged() -> None:
    """A hand-picked sample of V0.1 fields (roadmap 0.1.5) that V0.3 must not
    have renumbered - checked directly against the descriptor, independent of
    the implementer's own parity test."""
    fields = {f.name: f.number for f in agent_pb2.Hello.DESCRIPTOR.fields}
    assert fields["node_id"] == 1
    assert fields["protocol_version"] == 2
    assert fields["agent_version"] == 3
    assert fields["hostname"] == 4


def test_envelope_session_id_field_3_round_trips_on_both_directions() -> None:
    agent_msg = agent_pb2.AgentMessage(
        message_id="01J00000000000000000000001",
        session_id="sess_01J00000000000000000000000",
        heartbeat=agent_pb2.Heartbeat(sequence=7),
    )
    ctrl_msg = agent_pb2.ControllerMessage(
        message_id="01J00000000000000000000002",
        session_id="sess_01J00000000000000000000000",
        ack=agent_pb2.Ack(acked_message_id="01J00000000000000000000001"),
    )
    assert agent_msg.DESCRIPTOR.fields_by_name["session_id"].number == 3
    assert ctrl_msg.DESCRIPTOR.fields_by_name["session_id"].number == 3
    assert (
        agent_pb2.AgentMessage.FromString(agent_msg.SerializeToString()).session_id
        == "sess_01J00000000000000000000000"
    )
    assert (
        agent_pb2.ControllerMessage.FromString(ctrl_msg.SerializeToString()).session_id
        == "sess_01J00000000000000000000000"
    )


def test_message_carrying_wrong_session_id_is_still_parseable_not_dropped_at_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proto layer has no notion of "which session is live" - dropping a
    stale-session message is a caller responsibility (ADR-0002 Updates §2), so
    the wire format itself must not reject or mangle a message whose
    session_id is simply wrong; that decision is out of scope for the .proto.
    """
    msg = agent_pb2.AgentMessage(
        message_id="01J00000000000000000000003",
        session_id="sess_totally-wrong-session",
        heartbeat=agent_pb2.Heartbeat(sequence=1),
    )
    parsed = agent_pb2.AgentMessage.FromString(msg.SerializeToString())
    assert parsed.session_id == "sess_totally-wrong-session"


def test_hello_session_epoch_is_uint64_and_rejects_negative_python_int() -> None:
    with pytest.raises(ValueError):
        agent_pb2.Hello(session_epoch=-1)
    # Max uint64 must round-trip cleanly (the identity layer can in principle
    # produce values this large; see the identity tester's epoch-bound test).
    hello = agent_pb2.Hello(session_epoch=2**64 - 1)
    assert agent_pb2.Hello.FromString(hello.SerializeToString()).session_epoch == (
        2**64 - 1
    )


def test_agent_control_service_session_is_bidirectional_streaming() -> None:
    service = agent_pb2.DESCRIPTOR.services_by_name["AgentControl"]
    method = service.methods_by_name["Session"]
    assert method.client_streaming is True
    assert method.server_streaming is True
    assert method.input_type is agent_pb2.AgentMessage.DESCRIPTOR
    assert method.output_type is agent_pb2.ControllerMessage.DESCRIPTOR


def test_common_proto_package_matches_agent_proto_package() -> None:
    assert (
        common_pb2.DESCRIPTOR.package == agent_pb2.DESCRIPTOR.package == "meshforge.v1"
    )
