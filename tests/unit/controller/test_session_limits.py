"""V0.3 G2 review round 2 - B3: bound what a peer can make the controller keep.

``message_id`` must be a ``cmd_<ULID>`` (else ``protocol_error``), and the Hello's
string fields are capped (else ``INVALID_ARGUMENT`` before any registry call).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

import grpc
import pytest
import structlog
from test_session_manager import FakeClock, FakeRegistry, QueueStream, drain, hello

from meshforge import __version__
from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import (
    new_attempt_id,
    new_command_id,
    new_node_id,
    new_session_id,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.session.errors import SessionProtocolError
from meshforge.controller.session.limits import (
    AGENT_VERSION_MAX_LENGTH,
    HOSTNAME_MAX_LENGTH,
    MESSAGE_ID_MAX_LENGTH,
    OS_MAX_LENGTH,
    PLATFORM_MAX_LENGTH,
    check_hello_strings,
    validate_message_id,
)
from meshforge.controller.session.manager import (
    Accepted,
    Rejected,
    SessionManager,
)
from meshforge.controller.store.models.node import Node, NodeSession

pytestmark = pytest.mark.unit


def make_manager(reg: FakeRegistry, **kw: Any) -> SessionManager:
    return SessionManager(
        cast(Registry, reg),
        LivenessConfig(),
        clock=FakeClock(),
        wall_clock=lambda: dt.datetime(2026, 9, 20, tzinfo=dt.UTC),
        **kw,
    )


# ---------------------------------------------------------------- message_id


def test_validate_message_id_accepts_only_canonical_command_ids() -> None:
    good = new_command_id()
    assert validate_message_id(good) == good
    assert len(good) == MESSAGE_ID_MAX_LENGTH
    for bad in (
        "",
        "hb-1",
        good.lower(),  # not canonical
        good[:-1],  # too short
        good + "A",  # too long
        new_node_id(),  # right shape, wrong kind
        new_session_id(),
        new_attempt_id(),
        "cmd_" + "!" * 26,  # bad alphabet
        "x" * (1024 * 1024),  # a megabyte
    ):
        with pytest.raises(InvalidIdError) as info:
            validate_message_id(bad)
        assert bad not in str(info.value) or bad == ""  # never echoed


@pytest.mark.asyncio
async def test_malformed_message_id_is_a_protocol_error_and_nothing_is_kept() -> None:
    reg = FakeRegistry()
    manager = make_manager(reg)
    result = await manager.admit(hello(new_node_id()), "p")
    assert isinstance(result, Accepted)
    live = result.live
    huge = "x" * (1024 * 1024)
    with pytest.raises(SessionProtocolError) as info:
        await manager.handle_message(
            live,
            agent_pb2.AgentMessage(
                message_id=huge,
                session_id=live.session_id,
                heartbeat=agent_pb2.Heartbeat(sequence=1),
            ),
        )
    assert "message_id" in str(info.value) and huge not in str(info.value)
    assert len(live.seen) == 0  # not remembered
    assert live.heartbeats == 0  # not dispatched
    assert drain(live) == []  # not acked


@pytest.mark.asyncio
async def test_reader_closes_the_session_protocol_error_on_a_bad_message_id() -> None:
    reg = FakeRegistry()
    manager = make_manager(reg)
    result = await manager.admit(hello(new_node_id()), "p")
    assert isinstance(result, Accepted)
    live = result.live
    stream = QueueStream()
    reader = manager.start_reader(live, stream)
    stream.feed(
        agent_pb2.AgentMessage(
            message_id="M" * 1000,
            session_id=live.session_id,
            heartbeat=agent_pb2.Heartbeat(sequence=1),
        )
    )
    with structlog.testing.capture_logs() as logs:
        await reader
    assert live.ended and live.end_reason is SessionEndReason.PROTOCOL_ERROR
    (entry,) = [e for e in logs if e["event"] == "session.protocol_error"]
    assert "M" * 1000 not in entry["error"]


@pytest.mark.asyncio
async def test_dedupe_window_memory_is_bounded_in_bytes() -> None:
    """200 x 1 MB ids used to be retained (201 MB); now no oversized id is ever
    remembered, and every remembered id is exactly ``MESSAGE_ID_MAX_LENGTH``."""
    manager = make_manager(FakeRegistry(), dedupe_window=200)
    result = await manager.admit(hello(new_node_id()), "p")
    assert isinstance(result, Accepted)
    live = result.live
    for i in range(200):
        with pytest.raises(SessionProtocolError):
            await manager.handle_message(
                live,
                agent_pb2.AgentMessage(
                    message_id=f"{i}-" + "x" * (1024 * 1024),
                    session_id=live.session_id,
                    heartbeat=agent_pb2.Heartbeat(sequence=i),
                ),
            )
    assert len(live.seen) == 0
    for i in range(300):
        await manager.handle_message(
            live,
            agent_pb2.AgentMessage(
                message_id=new_command_id(),
                session_id=live.session_id,
                heartbeat=agent_pb2.Heartbeat(sequence=i),
            ),
        )
    assert len(live.seen) == 200  # the window, not 300
    assert all(len(m) == MESSAGE_ID_MAX_LENGTH for m in live.seen._set)


# ------------------------------------------------------------- Hello strings


def test_limits_match_the_registry_columns() -> None:
    """The caps exist to stop ``StringDataRightTruncation``; they must never be
    wider than the column they protect."""
    node_cols = Node.__table__.columns
    assert node_cols["hostname"].type.length >= HOSTNAME_MAX_LENGTH
    assert node_cols["os"].type.length >= OS_MAX_LENGTH
    assert node_cols["platform"].type.length >= PLATFORM_MAX_LENGTH
    session_cols = NodeSession.__table__.columns
    assert session_cols["agent_version"].type.length >= AGENT_VERSION_MAX_LENGTH


def test_check_hello_strings_names_the_field_and_lengths_not_the_value() -> None:
    ok = {"hostname": "h", "os": "o", "platform": "p", "agent_version": "v"}
    assert check_hello_strings(**ok) is None
    at_limit = {
        "hostname": "h" * HOSTNAME_MAX_LENGTH,
        "os": "o" * OS_MAX_LENGTH,
        "platform": "p" * PLATFORM_MAX_LENGTH,
        "agent_version": "v" * AGENT_VERSION_MAX_LENGTH,
    }
    assert check_hello_strings(**at_limit) is None
    for field, limit in (
        ("hostname", HOSTNAME_MAX_LENGTH),
        ("os", OS_MAX_LENGTH),
        ("platform", PLATFORM_MAX_LENGTH),
        ("agent_version", AGENT_VERSION_MAX_LENGTH),
    ):
        over = dict(ok, **{field: "Z" * (limit + 1)})
        reason = check_hello_strings(**over)
        assert reason == (
            f"Hello.{field} is {limit + 1} characters; the maximum is {limit}"
        )
        assert "ZZZ" not in reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("hostname", HOSTNAME_MAX_LENGTH),
        ("os", OS_MAX_LENGTH),
        ("platform", PLATFORM_MAX_LENGTH),
        ("agent_version", AGENT_VERSION_MAX_LENGTH),
    ],
)
async def test_over_long_hello_string_is_invalid_argument_before_any_db_call(
    field: str, limit: int
) -> None:
    reg = FakeRegistry()
    manager = make_manager(reg)
    h = hello(new_node_id())
    setattr(h, field, "Z" * (limit + 1))
    result = await manager.admit(h, "p")
    assert isinstance(result, Rejected)
    assert result.abort_status is grpc.StatusCode.INVALID_ARGUMENT
    assert result.reason.startswith(f"Hello.{field} is {limit + 1} characters")
    assert reg.calls == [] and reg.nodes == {} and reg.sessions == {}
    assert len(manager) == 0

    # Exactly at the cap is fine. V0.17 G5 (Q37): an agent_version must also
    # read as this controller's <major>.<minor>, so pad a real one to the cap.
    h2 = hello(new_node_id())
    at_cap = "Z" * limit
    if field == "agent_version":
        prefix = ".".join(__version__.split(".")[:2]) + "."
        at_cap = prefix + "0" * (limit - len(prefix))
    setattr(h2, field, at_cap)
    assert isinstance(await manager.admit(h2, "p"), Accepted)
