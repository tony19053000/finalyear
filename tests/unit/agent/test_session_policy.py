"""V0.3 G3 (0.3.8, ADR-0002 Updates §3): backoff table with a fake clock and an
injected random source; stream-end and HelloAck-reject action tables. No sleeps."""

from __future__ import annotations

import grpc
import pytest

from meshforge.agent.session.policy import (
    MAX_STALE_EPOCH_RETRIES,
    Backoff,
    BackoffPolicy,
    HelloRejectAction,
    StreamEndAction,
    hello_reject_action,
    stream_end_action,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit

RejectCode = agent_pb2.HelloAck.RejectCode


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


# ----------------------------------------------------------------------- backoff


def test_policy_ceilings_default() -> None:
    p = BackoffPolicy()
    assert [p.ceiling(n) for n in range(8)] == [
        0.5,
        1.0,
        2.0,
        4.0,
        8.0,
        15.0,
        15.0,
        15.0,
    ]
    assert p.ceiling(1000) == 15.0  # no overflow, still the cap


def test_policy_validation() -> None:
    with pytest.raises(ValueError, match="base_s"):
        BackoffPolicy(base_s=0)
    with pytest.raises(ValueError, match="cap_s"):
        BackoffPolicy(base_s=2, cap_s=1)
    with pytest.raises(ValueError, match="reset_after_s"):
        BackoffPolicy(reset_after_s=-1)
    with pytest.raises(ValueError, match="attempt"):
        BackoffPolicy().ceiling(-1)


def test_full_jitter_uses_the_whole_range() -> None:
    """rand()=1.0 (max) gives the ceiling; rand()=0 gives 0; rand()=0.5 the middle."""
    clock = FakeClock()
    values = iter([1.0, 0.0, 0.5, 1.0, 1.0, 1.0, 1.0])
    b = Backoff(BackoffPolicy(), clock=clock, rand=lambda: next(values))
    assert [b.next_delay() for _ in range(7)] == [0.5, 0.0, 1.0, 4.0, 8.0, 15.0, 15.0]
    assert b.attempt == 7


def test_cap_never_exceeded_with_any_random() -> None:
    clock = FakeClock()
    b = Backoff(
        BackoffPolicy(base_s=0.5, cap_s=15.0), clock=clock, rand=lambda: 0.999999
    )
    for _ in range(40):
        assert b.next_delay() <= 15.0


def test_reset_after_long_lived_stream() -> None:
    clock = FakeClock()
    b = Backoff(BackoffPolicy(reset_after_s=30.0), clock=clock, rand=lambda: 1.0)
    for _ in range(4):
        b.next_delay()
    assert b.attempt == 4
    b.stream_started()
    clock.advance(30.0)  # exactly the threshold counts
    assert b.stream_ended() == 30.0
    assert b.attempt == 0
    assert b.next_delay() == 0.5


def test_short_lived_stream_does_not_reset() -> None:
    clock = FakeClock()
    b = Backoff(BackoffPolicy(reset_after_s=30.0), clock=clock, rand=lambda: 1.0)
    for _ in range(3):
        b.next_delay()
    b.stream_started()
    clock.advance(29.9)
    assert b.stream_ended() == pytest.approx(29.9)
    assert b.attempt == 3
    assert b.next_delay() == 4.0  # 0.5 * 2**3


def test_stream_ended_without_start_is_zero_and_keeps_attempt() -> None:
    b = Backoff(BackoffPolicy(), clock=FakeClock(), rand=lambda: 1.0)
    b.next_delay()
    assert b.stream_ended() == 0.0
    assert b.attempt == 1


def test_clock_going_backwards_is_clamped() -> None:
    clock = FakeClock()
    b = Backoff(BackoffPolicy(), clock=clock, rand=lambda: 1.0)
    b.stream_started()
    clock.advance(-5.0)
    assert b.stream_ended() == 0.0


def test_explicit_reset() -> None:
    b = Backoff(BackoffPolicy(), clock=FakeClock(), rand=lambda: 1.0)
    b.next_delay()
    b.next_delay()
    b.reset()
    assert b.attempt == 0 and b.next_delay() == 0.5


# ------------------------------------------------------------- stream end table


@pytest.mark.parametrize(
    ("code", "details", "action"),
    [
        (grpc.StatusCode.OK, "", StreamEndAction.RECONNECT),
        (grpc.StatusCode.ABORTED, "superseded", StreamEndAction.STOP_SUPERSEDED),
        (grpc.StatusCode.ABORTED, " superseded ", StreamEndAction.STOP_SUPERSEDED),
        (grpc.StatusCode.ABORTED, "lease_expired", StreamEndAction.RECONNECT),
        (grpc.StatusCode.ABORTED, "protocol_error", StreamEndAction.RECONNECT),
        (grpc.StatusCode.ABORTED, "", StreamEndAction.RECONNECT),
        (grpc.StatusCode.ABORTED, None, StreamEndAction.RECONNECT),
        (grpc.StatusCode.UNAVAILABLE, "connect failed", StreamEndAction.RECONNECT),
        (grpc.StatusCode.DEADLINE_EXCEEDED, "", StreamEndAction.RECONNECT),
        (grpc.StatusCode.CANCELLED, "", StreamEndAction.RECONNECT),
        (grpc.StatusCode.UNKNOWN, "", StreamEndAction.RECONNECT),
        (grpc.StatusCode.INTERNAL, "", StreamEndAction.RECONNECT),
        (
            grpc.StatusCode.FAILED_PRECONDITION,
            "expected Hello",
            StreamEndAction.RECONNECT,
        ),
        (grpc.StatusCode.INVALID_ARGUMENT, "bad node_id", StreamEndAction.FATAL_CONFIG),
        (grpc.StatusCode.PERMISSION_DENIED, "revoked", StreamEndAction.FATAL_CONFIG),
        (None, "", StreamEndAction.RECONNECT),
    ],
)
def test_stream_end_action(
    code: grpc.StatusCode | None, details: str | None, action: StreamEndAction
) -> None:
    assert stream_end_action(code, details) is action


def test_superseded_detail_is_only_for_aborted() -> None:
    # "superseded" as a detail on any other status is not the fencing signal.
    assert (
        stream_end_action(grpc.StatusCode.OK, "superseded") is StreamEndAction.RECONNECT
    )
    assert (
        stream_end_action(grpc.StatusCode.UNAVAILABLE, "superseded")
        is StreamEndAction.RECONNECT
    )


# --------------------------------------------------------------- reject table


def test_stale_epoch_retries_are_bounded() -> None:
    for n in range(MAX_STALE_EPOCH_RETRIES):
        assert (
            hello_reject_action(RejectCode.REJECT_CODE_STALE_EPOCH, n)
            is HelloRejectAction.RETRY_NOW
        )
    assert (
        hello_reject_action(RejectCode.REJECT_CODE_STALE_EPOCH, MAX_STALE_EPOCH_RETRIES)
        is HelloRejectAction.BACKOFF
    )
    assert (
        hello_reject_action(RejectCode.REJECT_CODE_STALE_EPOCH, 99)
        is HelloRejectAction.BACKOFF
    )


@pytest.mark.parametrize(
    ("code", "action"),
    [
        (RejectCode.REJECT_CODE_PROTOCOL_VERSION, HelloRejectAction.FATAL_CONFIG),
        (RejectCode.REJECT_CODE_INTERNAL, HelloRejectAction.BACKOFF),
        (RejectCode.REJECT_CODE_DUPLICATE_SESSION, HelloRejectAction.BACKOFF),
        (RejectCode.REJECT_CODE_UNSPECIFIED, HelloRejectAction.BACKOFF),
    ],
)
def test_reject_actions(
    code: agent_pb2.HelloAck.RejectCode.ValueType, action: HelloRejectAction
) -> None:
    assert hello_reject_action(code, 0) is action
