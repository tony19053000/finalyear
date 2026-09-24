"""Pure change tracking and detection-latency maths (no I/O, no clock).

A :class:`Timeline` receives one :class:`NodeView` per poll, stamped with the
monotonic time the answer arrived, and keeps only the points where something the
operator cares about changed (``state``, ``connected``, ``session_epoch``,
``last_session_id``) plus the moments the controller stopped / started answering.
Conditions are functions of a view, so they can only become true at a change
point: :meth:`Timeline.first_edge` walks the change points, never every poll.

**Latency convention.** ``edge - mark``: ``mark`` is when the operator pressed
Enter, ``edge`` is when the first poll *answer* showing the new state arrived.
That is an upper bound on the controller's own detection time by at most one poll
interval plus one request (both recorded in the result's ``params``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

#: V0.3 success criterion 2 (roadmap): OFFLINE within 8 s, ONLINE within 10 s.
BOUNDS_S: Final[Mapping[str, float]] = {"offline": 8.0, "online": 10.0}
EXPECTABLE: Final = ("suspect", "offline", "online")
TRACKED: Final = ("state", "connected", "session_epoch", "last_session_id")


@dataclass(frozen=True, slots=True)
class NodeView:
    """What one ``GET /v1/nodes/{id}`` said about a node (the fields we track)."""

    node_id: str
    hostname: str
    state: str
    connected: bool
    session_epoch: int | None
    last_session_id: str | None

    @classmethod
    def from_json(cls, doc: Mapping[str, Any]) -> NodeView:
        epoch = doc.get("session_epoch")
        return cls(
            node_id=str(doc.get("node_id", "")),
            hostname=str(doc.get("hostname", "")),
            state=str(doc.get("state", "")),
            connected=doc.get("connected") is True,
            session_epoch=epoch
            if isinstance(epoch, int) and not isinstance(epoch, bool)
            else None,
            last_session_id=_opt_str(doc.get("last_session_id")),
        )

    def key(self) -> tuple[object, ...]:
        return tuple(getattr(self, name) for name in TRACKED)


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


type Condition = Callable[[NodeView], bool]


def condition(state: str) -> Condition:
    """When a node counts as having reached ``state``.

    ``online`` also needs ``connected``: a row reading ``online`` whose stream is
    gone is a write that has not landed yet (ARCHITECTURE §20, D16), not a node
    the controller can use.
    """
    if state not in EXPECTABLE:
        raise ValueError(f"cannot expect state {state!r}; choose from {EXPECTABLE}")
    if state == "online":
        return lambda v: v.state == "online" and v.connected
    return lambda v: v.state == state


@dataclass(frozen=True, slots=True)
class Point:
    """A change point: the first poll answer (or failure) that differed."""

    t: float
    wall: str
    view: NodeView | None  # None = the controller did not answer this poll


@dataclass(frozen=True, slots=True)
class Change:
    t: float
    wall: str
    field: str
    old: object
    new: object

    def to_json(self, origin: float) -> dict[str, Any]:
        return {
            "t_s": round(self.t - origin, 3),
            "wall": self.wall,
            "field": self.field,
            "old": self.old,
            "new": self.new,
        }


class Timeline:
    """Change points of one node, in arrival order."""

    def __init__(self) -> None:
        self.points: list[Point] = []
        self.changes: list[Change] = []

    @property
    def last(self) -> Point | None:
        return self.points[-1] if self.points else None

    def observe(self, t: float, wall: str, view: NodeView | None) -> list[Change]:
        """Record one poll result; return what changed (empty if nothing did)."""
        last = self.last
        if last is not None and t < last.t:
            raise ValueError("observations must arrive in monotonic order")
        if last is not None and _same(last.view, view):
            return []
        new = _changes(
            t, wall, None if last is None else last.view, view, first=last is None
        )
        self.points.append(Point(t, wall, view))
        self.changes.extend(new)
        return new

    def first_edge(self, cond: Condition, *, after: float) -> float | None:
        """Time of the first change point at or after ``after`` where ``cond``
        becomes true (it was false - or unknown - at the point before)."""
        previous = False
        for point in self.points:
            now = point.view is not None and cond(point.view)
            if point.t >= after and now and not previous:
                return point.t
            previous = now
        return None

    def holds(self, cond: Condition) -> bool:
        last = self.last
        return last is not None and last.view is not None and cond(last.view)

    def changes_between(self, start: float, end: float) -> list[Change]:
        return [c for c in self.changes if start <= c.t <= end]


def _same(a: NodeView | None, b: NodeView | None) -> bool:
    if a is None or b is None:
        return a is b
    return a.key() == b.key()


def _changes(
    t: float, wall: str, old: NodeView | None, new: NodeView | None, *, first: bool
) -> list[Change]:
    if new is None:
        return [Change(t, wall, "reachable", True, False)]
    out: list[Change] = []
    if old is None and not first:
        out.append(Change(t, wall, "reachable", False, True))
    for name in TRACKED:
        before = None if old is None else getattr(old, name)
        after = getattr(new, name)
        if old is None or before != after:
            out.append(Change(t, wall, name, before, after))
    return out


def latency(mark: float, edge: float) -> float:
    """Seconds from the operator's mark to the detecting poll answer (3 decimals)."""
    return round(edge - mark, 3)


def within_bound(state: str, seconds: float | None) -> bool | None:
    """``None`` when criterion 2 sets no bound for ``state`` or nothing was measured."""
    bound = BOUNDS_S.get(state)
    if bound is None or seconds is None:
        return None
    return seconds <= bound


def restarted(sequence_before: int | None, new_sequences: list[int]) -> bool | None:
    """Did the agent *process* restart across a reconnect?

    ``ResourceUpdate.sequence`` is per agent process and never reset across
    reconnects (``agent/session/client.py``); a restarted process starts again at 1.
    So: the lowest sequence the controller received on the new session is at or
    below the highest one before the disconnect -> a new process. ``None`` when
    either side is unknown (no live sample before, or none arrived after).

    ``session_epoch`` is **not** usable for this: the agent bumps it on every
    connection *attempt* (ADR-0002 Updates §1), so it changes on every reconnect
    of the same process too.
    """
    if sequence_before is None or not new_sequences:
        return None
    return min(new_sequences) <= sequence_before
