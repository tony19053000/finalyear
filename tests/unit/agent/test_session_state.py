"""V0.3 G3 (0.3.8): the agent-side state table is exhaustive and exactly as documented."""

from __future__ import annotations

import itertools

import pytest

from meshforge.agent.session.state import (
    LIVE_STATES,
    TERMINAL_STATES,
    AgentEvent,
    AgentState,
    InvalidAgentTransitionError,
    next_state,
)
from meshforge.common.errors import FailureType

pytestmark = pytest.mark.unit

S, E = AgentState, AgentEvent

EXPECTED: dict[tuple[AgentState, AgentEvent], AgentState] = {
    (S.DISCOVERING, E.CONTROLLER_FOUND): S.CONNECTING,
    (S.DISCOVERING, E.DISCOVERY_FAILED): S.BACKOFF,
    (S.CONNECTING, E.STREAM_OPENED): S.HELLO_SENT,
    (S.CONNECTING, E.STREAM_ENDED): S.BACKOFF,
    (S.HELLO_SENT, E.HELLO_ACCEPTED): S.ONLINE,
    (S.HELLO_SENT, E.HELLO_RETRY): S.CONNECTING,
    (S.HELLO_SENT, E.HELLO_REJECTED): S.BACKOFF,
    (S.HELLO_SENT, E.STREAM_ENDED): S.BACKOFF,
    (S.ONLINE, E.STREAM_ENDED): S.BACKOFF,
    (S.BACKOFF, E.BACKOFF_ELAPSED): S.DISCOVERING,
    (S.STOPPING, E.STOPPED): S.STOPPED,
}
for _live in LIVE_STATES:
    EXPECTED[(_live, E.STOP_REQUESTED)] = S.STOPPING
    EXPECTED[(_live, E.FATAL)] = S.STOPPING


@pytest.mark.parametrize(("state", "event"), list(itertools.product(S, E)))
def test_full_table(state: AgentState, event: AgentEvent) -> None:
    """Every (state, event) pair: the documented target, or a typed error."""
    if (state, event) in EXPECTED:
        assert next_state(state, event) is EXPECTED[(state, event)]
    else:
        with pytest.raises(InvalidAgentTransitionError) as info:
            next_state(state, event)
        assert info.value.failure_type is FailureType.INTERNAL_ERROR
        assert info.value.state is state and info.value.event is event


def test_terminal_state_has_no_exits() -> None:
    for event in E:
        with pytest.raises(InvalidAgentTransitionError):
            next_state(S.STOPPED, event)
    assert {S.STOPPED} == TERMINAL_STATES


def test_live_states_partition() -> None:
    assert LIVE_STATES | {S.STOPPING} | TERMINAL_STATES == set(S)
    assert not (LIVE_STATES & TERMINAL_STATES)


def test_happy_path_and_reconnect_loop() -> None:
    path = [
        E.CONTROLLER_FOUND,
        E.STREAM_OPENED,
        E.HELLO_ACCEPTED,
        E.STREAM_ENDED,
        E.BACKOFF_ELAPSED,
        E.CONTROLLER_FOUND,
        E.STREAM_OPENED,
        E.HELLO_RETRY,  # STALE_EPOCH: bump and retry at once
        E.STREAM_OPENED,
        E.HELLO_ACCEPTED,
        E.STOP_REQUESTED,
        E.STOPPED,
    ]
    state = S.DISCOVERING
    seen = [state]
    for event in path:
        state = next_state(state, event)
        seen.append(state)
    assert seen == [
        S.DISCOVERING,
        S.CONNECTING,
        S.HELLO_SENT,
        S.ONLINE,
        S.BACKOFF,
        S.DISCOVERING,
        S.CONNECTING,
        S.HELLO_SENT,
        S.CONNECTING,
        S.HELLO_SENT,
        S.ONLINE,
        S.STOPPING,
        S.STOPPED,
    ]
