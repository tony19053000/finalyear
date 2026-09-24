"""``fabric_check watch`` against a real stub HTTP controller with an injected clock:
transition timestamping, latency from the operator's Enter, restart detection
(sequence reset, NOT epoch change), and the typed failures."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from _fabric.api import FabricApi
from _fabric.testing import (
    FakeClock,
    ScriptedOperator,
    StubController,
    node_doc,
    step,
)
from _fabric.watch import (
    MARK_LATE,
    NODE_ID_CHANGED,
    NOT_DETECTED,
    NOT_READY,
    WatchConfig,
    run_watch,
)

NODE = "node_01JAAAAAAAAAAAAAAAAAAAAAAA"


def _setup(stub: StubController) -> None:
    stub.nodes[NODE] = node_doc(NODE, "lap2", epoch=5, session_id="s5")
    stub.add_session("s5", NODE, epoch=5)
    stub.add_samples(NODE, 5, range(1, 121))


def _cable_pull(
    stub: StubController,
    clock: FakeClock,
    *,
    new_sequences: list[int],
    reason: str = "lease_expired",
    offline_after: float = 6.0,
    online_after: float = 0.3,
) -> list[Any]:
    """Enter at +0.5 s after each prompt; the node reacts ``*_after`` s later."""

    def pull() -> float:
        mark = clock.now + 0.5
        clock.at(
            mark + 1.0, lambda: stub.set_node(NODE, state="suspect", connected=False)
        )

        def gone() -> None:
            stub.set_node(NODE, state="offline")
            stub.end_session("s5", ended_at="2026-09-24T10:00:07+00:00", reason=reason)

        clock.at(mark + offline_after, gone)
        return 0.5

    def plug() -> float:
        mark = clock.now + 0.5

        def back() -> None:
            stub.set_node(
                NODE,
                state="online",
                connected=True,
                session_epoch=6,
                last_session_id="s6",
            )
            stub.add_session("s6", NODE, epoch=6)
            stub.add_samples(NODE, 6, new_sequences)

        clock.at(mark + online_after, back)
        return 0.5

    return [pull, plug]


def _run(stub: StubController, script: list[Any], clock: FakeClock, **cfg: Any) -> Any:
    op = ScriptedOperator(clock, script)
    config = WatchConfig(node_id=NODE, expect=("offline", "online"), reps=1, **cfg)
    return run_watch(FabricApi(stub.url), clock, op, config), op


def test_cable_pull_latencies_are_measured_from_enter(
    stub_controller: StubController,
) -> None:
    _setup(stub_controller)
    clock = FakeClock()
    result, op = _run(
        stub_controller,
        _cable_pull(stub_controller, clock, new_sequences=[121, 122]),
        clock,
    )
    assert result.errors == []
    (sample,) = result.samples
    # the change lands between two 100 ms polls: the measured value is at most one poll late
    assert 6.0 <= sample["offline_s"] <= 6.0 + 0.1 + 1e-6
    assert 0.3 <= sample["online_s"] <= 0.3 + 0.1 + 1e-6
    assert sample["offline_within_bound"] is True
    assert sample["online_within_bound"] is True
    assert sample["end_reason"] == "lease_expired"
    assert (sample["epoch_before"], sample["epoch_after"]) == (5, 6)
    # the epoch changed, yet the agent process did NOT restart: the sequence continued
    assert sample["restarted"] is False
    assert sample["first_new_sequence"] == 121
    fields = [(c["field"], c["new"]) for c in sample["changes"]]
    assert fields.index(("state", "suspect")) < fields.index(("state", "offline"))
    assert fields.index(("state", "offline")) < len(fields) - 1 - fields[::-1].index(
        ("state", "online")
    )
    assert len(op.prompts) == 2
    assert "PULL" in op.prompts[0] and "lap2" in op.prompts[0]


def test_a_sequence_reset_is_reported_as_an_agent_restart(
    stub_controller: StubController,
) -> None:
    _setup(stub_controller)
    clock = FakeClock()
    script = _cable_pull(
        stub_controller, clock, new_sequences=[1, 2], reason="stream_lost"
    )
    result, _ = _run(stub_controller, script, clock)
    (sample,) = result.samples
    assert sample["restarted"] is True
    assert sample["end_reason"] == "stream_lost"


def test_a_bound_exceeded_is_recorded_not_hidden(
    stub_controller: StubController,
) -> None:
    _setup(stub_controller)
    clock = FakeClock()
    script = _cable_pull(stub_controller, clock, new_sequences=[121], offline_after=9.0)
    result, _ = _run(stub_controller, script, clock)
    (sample,) = result.samples
    assert sample["offline_s"] > 8.0
    assert sample["offline_within_bound"] is False


def test_enter_pressed_after_the_change_does_not_count(
    stub_controller: StubController,
) -> None:
    _setup(stub_controller)
    clock = FakeClock()

    def late() -> float:
        clock.after(
            0.2,
            lambda: stub_controller.set_node(NODE, state="offline", connected=False),
        )
        return 2.0  # Enter 1.8 s after the node was already offline

    result, _ = _run(stub_controller, [late], clock)
    (error,) = result.errors
    assert error["failure_type"] == MARK_LATE
    assert "offline_s" not in result.samples[0]


def test_a_state_never_reached_times_out_as_not_detected(
    stub_controller: StubController,
) -> None:
    _setup(stub_controller)
    clock = FakeClock()
    result, op = _run(stub_controller, [lambda: 0.1], clock, detect_timeout_s=3.0)
    (error,) = result.errors
    assert error["failure_type"] == NOT_DETECTED
    assert result.samples[0]["offline_within_bound"] is False
    assert clock.now - 1000.0 < 10.0  # gave up at the timeout, not later
    assert any("restore the node" in s for s in op.said)


def test_a_node_back_under_a_new_node_id_is_named(
    stub_controller: StubController,
) -> None:
    _setup(stub_controller)
    clock = FakeClock()

    def pull() -> float:
        clock.after(
            1.0,
            lambda: stub_controller.set_node(NODE, state="offline", connected=False),
        )
        return 0.5

    def plug() -> float:
        def new_identity() -> None:
            other = "node_01JBBBBBBBBBBBBBBBBBBBBBBB"
            with stub_controller.lock:
                stub_controller.nodes[other] = node_doc(other, "lap2", epoch=1)

        clock.after(1.0, new_identity)
        return 0.5

    result, _ = _run(stub_controller, [pull, plug], clock, detect_timeout_s=5.0)
    (error,) = result.errors
    assert error["failure_type"] == NODE_ID_CHANGED
    assert "node_01JBBBBBBBBBBBBBBBBBBBBBBB" in error["message"]


def test_a_node_that_never_comes_online_aborts_the_run(
    stub_controller: StubController,
) -> None:
    _setup(stub_controller)
    stub_controller.set_node(NODE, state="offline", connected=False)
    clock = FakeClock()
    op = ScriptedOperator(clock, [])
    cfg = WatchConfig(
        node_id=NODE, expect=("offline", "online"), reps=3, ready_timeout_s=2.0
    )
    result = run_watch(FabricApi(stub_controller.url), clock, op, cfg)
    assert result.aborted
    assert result.errors[0]["failure_type"] == NOT_READY
    assert result.errors[0]["fatal"] is True
    assert op.prompts == []


def test_the_controller_going_away_mid_step_is_on_the_timeline(
    stub_controller: StubController,
) -> None:
    """The stub's listening socket is really closed for 2 s during the pull."""
    _setup(stub_controller)
    clock = FakeClock()

    def pull() -> float:
        clock.after(1.0, stub_controller.stop)
        clock.after(3.0, stub_controller.start)
        clock.after(
            4.0,
            lambda: stub_controller.set_node(NODE, state="offline", connected=False),
        )
        return 0.5

    def plug() -> float:
        clock.after(
            0.5, lambda: stub_controller.set_node(NODE, state="online", connected=True)
        )
        return 0.2

    result, _ = _run(stub_controller, [pull, plug], clock)
    (sample,) = result.samples
    reach = [
        (c["field"], c["new"]) for c in sample["changes"] if c["field"] == "reachable"
    ]
    assert reach == [("reachable", False), ("reachable", True)]
    assert 3.5 <= sample["offline_s"] <= 3.6 + 1e-6


def test_several_repetitions_wait_for_the_node_between_them(
    stub_controller: StubController,
) -> None:
    _setup(stub_controller)
    clock = FakeClock()

    def go(state: str, *, connected: bool) -> Callable[[], None]:
        return lambda: stub_controller.set_node(NODE, state=state, connected=connected)

    script: list[Callable[[], float | None]] = []
    for _ in range(3):
        script += [
            step(clock, [(2.0, go("offline", connected=False))], 0.1),
            step(clock, [(1.0, go("online", connected=True))], 0.1),
        ]
    op = ScriptedOperator(clock, script)
    cfg = WatchConfig(
        node_id=NODE, expect=("offline", "online"), reps=3, sample_wait_s=0.5
    )
    result = run_watch(FabricApi(stub_controller.url), clock, op, cfg)
    assert [round(s["offline_s"], 1) for s in result.samples] == [1.9, 1.9, 1.9]
    assert [s["rep"] for s in result.samples] == [0, 1, 2]
    # no live sample on a new epoch arrived: restart is unknown, not "no"
    assert all(s["restarted"] is None for s in result.samples)


@pytest.mark.parametrize(
    ("expect", "match"),
    [
        ((), "at least one"),
        (("offline", "offline"), "repeat"),
        (("gone",), "cannot expect"),
    ],
)
def test_bad_expect_lists_are_refused(expect: tuple[str, ...], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        WatchConfig(node_id=NODE, expect=expect)
