"""V0.8 G1 tester r1 (D172): ``node.metrics`` is <= 1 Hz per node whatever the
sample rate, ``node.status`` is only changes, and a flapping node cannot make
the watcher grow without bound."""

# ruff: noqa: SIM113

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.controller.events.bus import EventBus, EventType
from meshforge.controller.events.watcher import NodeWatcher
from meshforge.controller.liveness import NodeState
from meshforge.controller.resources import LiveSample

pytestmark = pytest.mark.unit


class _Manager:
    def __init__(self) -> None:
        self.live: list[Any] = []

    def snapshot(self) -> list[Any]:
        return list(self.live)

    def lingering(self) -> list[Any]:
        return []


class _Resources:
    def __init__(self) -> None:
        self.samples: dict[str, LiveSample] = {}

    def latest(self, node_id: str) -> LiveSample | None:
        return self.samples.get(node_id)


def _sample(seq: int) -> LiveSample:
    return LiveSample(
        session_epoch=1,
        sequence=seq,
        observed_at=dt.datetime(2026, 10, 15, tzinfo=dt.UTC),
        received_at=float(seq),
        cpu_percent=1.0,
        ram_used_bytes=1,
        ram_total_bytes=2,
        gpus=(),
    )


def _live(node: str) -> Any:
    return SimpleNamespace(
        node_id=node, state=NodeState.ONLINE, ended=False, lingering=False
    )


def test_metrics_never_exceed_one_per_second_per_node_at_any_sample_rate() -> None:
    bus, manager, resources = EventBus(ring_size=100_000), _Manager(), _Resources()
    now = [0.0]
    watcher = NodeWatcher(
        bus,
        manager,
        resources,
        clock=lambda: now[0],
        metrics_interval_s=1.0,  # type: ignore[arg-type]
    )
    nodes = [f"n{i}" for i in range(20)]
    manager.live = [_live(n) for n in nodes]
    seq = 0
    for step in range(400):  # 20 s of 50 ms ticks, a NEW sample every tick
        now[0] = step * 0.05
        seq += 1
        for n in nodes:
            resources.samples[n] = _sample(seq)
        watcher.tick()
    per_node: dict[str, int] = {}
    for e in bus._ring:
        if e.type is EventType.NODE_METRICS:
            per_node[e.data["node_id"]] = per_node.get(e.data["node_id"], 0) + 1
    assert set(per_node) == set(nodes)
    assert all(count <= 21 for count in per_node.values()), per_node  # 20 s -> <= 21
    assert all(count >= 19 for count in per_node.values()), per_node  # and not starved


def test_a_flapping_node_status_is_every_change_but_nothing_else() -> None:
    bus, manager, resources = EventBus(), _Manager(), _Resources()
    watcher = NodeWatcher(bus, manager, resources, clock=lambda: 0.0)  # type: ignore[arg-type]
    for i in range(50):
        manager.live = [_live("n")] if i % 2 == 0 else []
        watcher.tick()
        watcher.tick()  # a repeated tick adds nothing
    kinds = [e.data["connected"] for e in bus._ring if e.type is EventType.NODE_STATUS]
    assert kinds == [i % 2 == 0 for i in range(50)]


def test_a_thousand_nodes_appearing_and_vanishing_leave_no_state_behind() -> None:
    bus, manager, resources = EventBus(ring_size=10_000), _Manager(), _Resources()
    watcher = NodeWatcher(bus, manager, resources, clock=lambda: 0.0)  # type: ignore[arg-type]
    for i in range(1000):
        manager.live = [_live(f"n{i}")]
        resources.samples[f"n{i}"] = _sample(i + 1)
        watcher.tick()
    manager.live = []
    watcher.tick()
    assert not watcher._status and not watcher._metrics_at and not watcher._metrics_seen


def test_a_crash_in_one_tick_does_not_poison_the_next() -> None:
    bus, manager, resources = EventBus(), _Manager(), _Resources()
    watcher = NodeWatcher(bus, manager, resources, clock=lambda: 0.0)  # type: ignore[arg-type]
    manager.live = [_live("n")]

    def boom(_node: str) -> None:
        raise RuntimeError("tracker exploded")

    resources.latest = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        watcher.tick()
    resources.latest = lambda _n: None  # type: ignore[method-assign,assignment]
    watcher.tick()  # recovers; status already published once, not twice
    statuses = [e for e in bus._ring if e.type is EventType.NODE_STATUS]
    assert len(statuses) == 1
