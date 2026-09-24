"""Pure change tracking + latency maths of the V0.3 lab kit (``lab/_fabric/timeline.py``)."""

from __future__ import annotations

import pytest

from _fabric.timeline import (
    NodeView,
    Timeline,
    condition,
    latency,
    restarted,
    within_bound,
)


def view(state: str = "online", *, connected: bool = True, epoch: int = 1) -> NodeView:
    return NodeView("node_x", "lap", state, connected, epoch, f"s{epoch}")


def test_only_changes_are_kept_and_stamped_with_the_poll_time() -> None:
    tl = Timeline()
    assert [c.field for c in tl.observe(1.0, "w1", view())] == list(
        ("state", "connected", "session_epoch", "last_session_id")
    )
    assert tl.observe(1.1, "w2", view()) == []  # nothing changed: no point
    changes = tl.observe(1.2, "w3", view("suspect", connected=False))
    assert {(c.field, c.old, c.new, c.t) for c in changes} == {
        ("state", "online", "suspect", 1.2),
        ("connected", True, False, 1.2),
    }
    assert len(tl.points) == 2


def test_unreachable_polls_are_change_points_too() -> None:
    tl = Timeline()
    tl.observe(1.0, "w", view())
    lost = tl.observe(2.0, "w", None)
    assert [(c.field, c.new) for c in lost] == [("reachable", False)]
    assert tl.observe(2.1, "w", None) == []
    back = tl.observe(3.0, "w", view())
    assert ("reachable", True) in [(c.field, c.new) for c in back]


def test_time_must_not_run_backwards() -> None:
    tl = Timeline()
    tl.observe(2.0, "w", view())
    with pytest.raises(ValueError, match="monotonic"):
        tl.observe(1.0, "w", view("offline"))


def test_first_edge_is_the_change_point_where_the_condition_becomes_true() -> None:
    tl = Timeline()
    tl.observe(1.0, "w", view())
    tl.observe(2.0, "w", view("suspect", connected=False))
    tl.observe(7.5, "w", view("offline", connected=False))
    tl.observe(9.0, "w", view("online", connected=False))  # row written, stream not yet
    tl.observe(9.4, "w", view("online", connected=True, epoch=2))
    assert tl.first_edge(condition("offline"), after=1.5) == 7.5
    assert tl.first_edge(condition("suspect"), after=0.0) == 2.0
    # online needs connected: the 9.0 row does not count
    assert tl.first_edge(condition("online"), after=7.5) == 9.4
    # an edge before `after` is not found again
    assert tl.first_edge(condition("suspect"), after=2.5) is None


def test_a_state_that_already_held_at_the_start_is_not_an_edge() -> None:
    tl = Timeline()
    tl.observe(1.0, "w", view("offline", connected=False))
    assert tl.first_edge(condition("offline"), after=0.0) == 1.0  # first point: was unknown
    assert tl.first_edge(condition("offline"), after=1.5) is None


def test_latency_and_bounds() -> None:
    assert latency(10.0, 16.25) == 6.25
    assert latency(10.0, 9.5) == -0.5
    assert within_bound("offline", 8.0) is True
    assert within_bound("offline", 8.001) is False
    assert within_bound("online", 10.0) is True
    assert within_bound("suspect", 1.0) is None
    assert within_bound("offline", None) is None


def test_unknown_state_is_refused() -> None:
    with pytest.raises(ValueError, match="cannot expect"):
        condition("draining")


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (120, [121, 122], False),  # same process: the counter continued
        (120, [1, 2], True),  # new process: the counter restarted at 1
        (120, [120], True),  # at or below the old maximum = a new counter
        (None, [5], None),  # no sample before: unknown
        (120, [], None),  # no sample after: unknown
    ],
)
def test_restart_is_read_from_the_per_process_sequence(
    before: int | None, after: list[int], expected: bool | None
) -> None:
    assert restarted(before, after) is expected
