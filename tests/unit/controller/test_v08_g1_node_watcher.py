"""V0.8 G1 deliverable 3 (D172): ``node.status`` / ``node.metrics`` from the
session manager's in-memory view - only changes are published, metrics at
most once per ``metrics_interval_s`` per node and only for a new sample."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from meshforge.controller.events.bus import EventBus, EventType
from meshforge.controller.events.watcher import NodeWatcher
from meshforge.controller.liveness import NodeState
from meshforge.controller.resources import GpuSample, LiveSample

pytestmark = pytest.mark.unit


class _Manager:
    def __init__(self) -> None:
        self.live: list[Any] = []
        self.linger: list[Any] = []

    def snapshot(self) -> list[Any]:
        return list(self.live)

    def lingering(self) -> list[Any]:
        return list(self.linger)


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
        cpu_percent=12.345,
        ram_used_bytes=1,
        ram_total_bytes=2,
        gpus=(
            GpuSample(index=0, util_percent=5.0, vram_used_bytes=3, vram_total_bytes=4),
        ),
    )


def _session(node: str, state: NodeState = NodeState.ONLINE, **kw: Any) -> Any:
    return SimpleNamespace(
        node_id=node,
        state=state,
        ended=kw.get("ended", False),
        lingering=kw.get("lingering", False),
    )


def _setup() -> tuple[NodeWatcher, EventBus, _Manager, _Resources, list[float]]:
    bus, manager, resources = EventBus(), _Manager(), _Resources()
    now = [0.0]
    watcher = NodeWatcher(
        bus,
        manager,  # type: ignore[arg-type]
        resources,  # type: ignore[arg-type]
        clock=lambda: now[0],
        metrics_interval_s=1.0,
    )
    return watcher, bus, manager, resources, now


def _events(bus: EventBus, type_: EventType) -> list[dict[str, Any]]:
    return [dict(e.data) for e in bus._ring if e.type is type_]


def test_status_is_published_only_on_change() -> None:
    watcher, bus, manager, _, _ = _setup()
    manager.live = [_session("n1")]
    watcher.tick()
    watcher.tick()
    assert _events(bus, EventType.NODE_STATUS) == [
        {"node_id": "n1", "state": "online", "connected": True}
    ]
    manager.live = [_session("n1", NodeState.SUSPECT)]
    watcher.tick()
    manager.live, manager.linger = (
        [],
        [_session("n1", NodeState.SUSPECT, lingering=True)],
    )
    watcher.tick()
    manager.linger = []
    watcher.tick()
    watcher.tick()
    assert _events(bus, EventType.NODE_STATUS)[1:] == [
        {"node_id": "n1", "state": "suspect", "connected": True},
        {"node_id": "n1", "state": "suspect", "connected": False},
        {"node_id": "n1", "state": None, "connected": False},
    ]


def test_metrics_at_most_once_a_second_and_only_new_samples() -> None:
    watcher, bus, manager, resources, now = _setup()
    manager.live = [_session("n1")]
    resources.samples["n1"] = _sample(1)
    watcher.tick()
    watcher.tick()  # same sample: nothing
    resources.samples["n1"] = _sample(2)
    now[0] = 0.5
    watcher.tick()  # new sample but < 1 s: nothing
    now[0] = 1.2
    watcher.tick()
    metrics = _events(bus, EventType.NODE_METRICS)
    assert len(metrics) == 2
    assert metrics[0]["cpu_percent"] == 12.35 and metrics[0]["gpus"][0]["index"] == 0
    # A lingering (disconnected) node gets no metrics.
    manager.live, manager.linger = [], [_session("n1", lingering=True)]
    resources.samples["n1"] = _sample(3)
    now[0] = 5.0
    watcher.tick()
    assert len(_events(bus, EventType.NODE_METRICS)) == 2
