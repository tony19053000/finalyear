"""Fabricated registry/memory records for the V0.3 G5 ``/v1`` tests.

Imported as ``mf_api_records`` (``tests/conftest.py`` puts ``tests/_support`` on
``sys.path``) by both the shaping tests and the router tests - never
``from conftest import``, and never across test modules: with prepend import mode
``tests`` is not a package.

Everything here is invented. Nothing reflects the machine the tests run on, so the
same assertions hold on a CPU-only Windows laptop.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from meshforge.common.ids import new_node_id, new_session_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import NodeRecord, SessionRecord, SnapshotRecord
from meshforge.controller.resources import LiveRing, LiveSample
from meshforge.controller.session.clocks import NodeClock
from meshforge.controller.session.manager import LiveSession

__all__ = [
    "T0",
    "live_session",
    "node_record",
    "ring_of",
    "sample",
    "session_record",
    "snapshot_record",
]

T0 = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.UTC)


class _FakeLiveSession:
    """Only the attributes ``shape`` reads off a live session.

    A real :class:`LiveSession` needs a lease tracker, an outbound queue and a
    write chain; none of that is part of the shaping contract.
    """

    def __init__(
        self,
        *,
        session_id: str,
        peer_address: str = "127.0.0.1:50000",
        last_rtt_ms: float | None = 4.5,
        rtt_ewma_ms: float | None = 5.5,
        peer_endpoint: str = "",
    ) -> None:
        self.session_id = session_id
        self.peer_endpoint = peer_endpoint
        self.peer_address = peer_address
        self.last_rtt_ms = last_rtt_ms
        self.rtt_ewma_ms = rtt_ewma_ms
        self.policy = None  # V0.7 G5: no advertised owner policy
        self.policy_refused = None
        self.clock = NodeClock()  # V0.9 G1 (D213): no exchange yet


def live_session(**kwargs: Any) -> LiveSession:
    return cast("LiveSession", _FakeLiveSession(**kwargs))


def node_record(**overrides: Any) -> NodeRecord:
    base: dict[str, Any] = {
        "node_id": str(new_node_id()),
        "hostname": "fabricated-host",
        "os": "linux",
        "platform": "x86_64",
        "hardware": {"cpu_threads": 8},
        "state": NodeState.ONLINE,
        "first_seen_at": T0,
        "last_seen_at": T0 + dt.timedelta(minutes=1),
        "last_session_id": None,
        "session_epoch": 3,
    }
    base.update(overrides)
    return NodeRecord(**base)


def session_record(node_id: str, **overrides: Any) -> SessionRecord:
    base: dict[str, Any] = {
        "session_id": str(new_session_id()),
        "node_id": node_id,
        "session_epoch": 3,
        "started_at": T0,
        "ended_at": None,
        "end_reason": None,
        "peer_address": "127.0.0.1:50001",
        "agent_version": "0.1.0.dev0",
        "protocol_version": 1,
        "last_rtt_ms": 7.0,
        "rtt_ewma_ms": 8.0,
    }
    base.update(overrides)
    return SessionRecord(**base)


def snapshot_record(node_id: str, **overrides: Any) -> SnapshotRecord:
    base: dict[str, Any] = {
        "node_id": node_id,
        "bucket_start": T0,
        "samples": 30,
        "cpu_percent_mean": 1.0,
        "cpu_percent_max": 2.0,
        "ram_used_bytes_mean": 3,
        "ram_used_bytes_max": 4,
        "ram_total_bytes": 8,
        "gpu": {},
        "disks": {},
    }
    base.update(overrides)
    return SnapshotRecord(**base)


def sample(**overrides: Any) -> LiveSample:
    base: dict[str, Any] = {
        "session_epoch": 3,
        "sequence": 11,
        "observed_at": T0,
        "received_at": 100.0,
        "cpu_percent": 12.5,
        "ram_used_bytes": 2_000,
        "ram_total_bytes": 8_000,
        "gpus": (),
        "disks": (),
    }
    base.update(overrides)
    return LiveSample(**base)


def ring_of(*samples: LiveSample, capacity: int = 100) -> LiveRing:
    ring = LiveRing(capacity=capacity)
    for item in samples:
        ring.append(item)
    return ring
